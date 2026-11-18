"""livetv-proxy: tidies Dispatcharr's channel list for Jellyfin.

/LiveTv.m3u  Dispatcharr's playlist. Each channel name has the STRIP_PREFIXES tags
             removed from its start and the STRIP_SUFFIXES tags from its end, then
             channels are sorted (SORT_BY, SORT_ORDER; by default A-Z, TSN 2 before
             TSN 10) and renumbered 1, 2, 3...
             A name starting with a number ("60 MINUTES") gets a no-break space after
             it, so Jellyfin doesn't take the number for a channel number and drop it.
/Epg.xml     Dispatcharr's guide with the same tags removed from the channel names
             and from every programme title (Dispatcharr's 24/7 dummy programmes
             are titled with the channel name). Where a programme's description
             mentions its channel's full original name ("...on US: FAMILY GUY 4K"),
             that is swapped for the cleaned name. Nothing else is changed or added.
/stream/<id> A channel's video, from Dispatcharr's /proxy/ts/stream/<id>, passed through
             FFmpeg (video and audio copied, not re-encoded) so its timestamps only ever
             move forward. When the provider drops a stream, Dispatcharr reconnects, but
             the provider restarts it a few seconds back in time; Jellyfin keeps the
             original timestamps (-copyts), so its player freezes or loops the audio.
             FFmpeg evens out such jumps. Before FFmpeg, the replayed part is cut out:
             every keyframe sent is remembered (a hash of its picture data), and after a
             reconnect everything is dropped until a keyframe that hasn't been sent yet,
             so the viewer skips ahead a moment instead of watching the replay. If
             Dispatcharr gives up on the channel, it's asked for it again (a fresh
             provider connection) for up to RESTART_FOR seconds while the player waits.
             The playlist's stream addresses point here when TIMESTAMP_FIX is on and
             FFmpeg is installed; otherwise at Dispatcharr.

Environment:
  SOURCE_M3U      Dispatcharr M3U link   (default http://localhost:9191/output/m3u/default)
  SOURCE_EPG      Dispatcharr EPG link   (default http://localhost:9191/output/epg/default)
  STRIP_PREFIXES  comma-separated tags to remove from the start of names, e.g.
                  CA EN:,24/7:,24/7,AT&T   (case-insensitive, repeated: "CA EN: 24/7: X" -> "X")
  STRIP_SUFFIXES  same, for the end of names, e.g.  FHD,HD,(CA)   ("TSN 1 (CA) FHD" -> "TSN 1")
  SORT_BY         name (default): by cleaned channel name; group: by Dispatcharr channel
                  group, then name; dispatcharr: keep Dispatcharr's own channel order
  SORT_ORDER      asc (default) or desc
  TIMESTAMP_FIX   1 (default) to send streams through /stream/<id>, 0 for straight to Dispatcharr
  RESTART_FOR     seconds to keep asking Dispatcharr for a channel it gave up on (default 60)
  PORT            default 8097

Nothing is stored on disk.
Tuner URL: http://localhost:8097/LiveTv.m3u   Guide URL: http://localhost:8097/Epg.xml
"""
import collections
import hashlib
import html
import http.client
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

SOURCE_M3U = os.environ.get("SOURCE_M3U", "http://localhost:9191/output/m3u")
SOURCE_EPG = os.environ.get("SOURCE_EPG", "http://localhost:9191/output/epg")
PORT = int(os.environ.get("PORT", "8097"))
DISPATCHARR = "{0.scheme}://{0.netloc}".format(urlsplit(SOURCE_M3U))
TIMESTAMP_FIX = os.environ.get("TIMESTAMP_FIX", "1") == "1" and shutil.which("ffmpeg") is not None
RESTART_FOR = int(os.environ.get("RESTART_FOR", "60"))
SORT_BY = os.environ.get("SORT_BY", "name").strip().lower() or "name"
SORT_DESC = os.environ.get("SORT_ORDER", "asc").strip().lower() == "desc"
GROUP_TITLE = re.compile(r'\sgroup-title="([^"]*)"')
CHNO_VALUE = re.compile(r'\stvg-chno="([^"]*)"')


def tag_list(var):
    """'CA EN:,24/7:,24/7' -> ['ca en:', '24/7:', '24/7'], longest first so '24/7:' beats '24/7'."""
    return sorted({t.strip().lower() for t in os.environ.get(var, "").split(",") if t.strip()},
                  key=len, reverse=True)


PREFIXES = tag_list("STRIP_PREFIXES")
SUFFIXES = tag_list("STRIP_SUFFIXES")
SEPARATORS = " :|-"  # cleaned off whatever is left next to a removed tag

TVG_NAME = re.compile(r'(\stvg-name=")([^"]*)(")')
TVG_CHNO = re.compile(r'\stvg-chno="[^"]*"')
EXTINF = re.compile(r"^(#EXTINF:-?\d+)")
GUIDE_NAME = re.compile(r"(<(display-name|title)\b[^>]*>)([^<]*)(</\2>)")  # channel names + programme titles
CHANNEL = re.compile(r'<channel\b[^>]*\bid="([^"]+)"[^>]*>\s*<display-name[^>]*>([^<]*)</display-name>')
PROGRAMME = re.compile(r'(<programme\b[^>]*\bchannel="([^"]+)"[^>]*>)(.*?)(</programme>)', re.S)
DESC = re.compile(r"(<desc\b[^>]*>)([^<]*)(</desc>)")
LEADING_NUMBER = re.compile(r"^([+-]?[\d.,]+) ")  # "60 MINUTES", "84. VOX"
STREAM_URL = re.compile(r"^\s*https?://[^/]+/proxy/ts/stream/([A-Za-z0-9-]+)\s*$")  # Dispatcharr's
STREAM_PATH = re.compile(r"^/stream/([A-Za-z0-9-]+)$")


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def clean_name(name):
    """'CA EN: 24/7: TSN 1 (CA) FHD' -> 'TSN 1' (keeps the original if nothing would be left)."""
    original = name = name.strip()
    while True:
        low = name.lower()
        if p := next((p for p in PREFIXES if low.startswith(p)), None):
            name = name[len(p):].lstrip(SEPARATORS)
        elif s := next((s for s in SUFFIXES if low.endswith(s)), None):
            name = name[:-len(s)].rstrip(SEPARATORS)
        else:
            return name or original


def keep_leading_number(name):
    """Jellyfin treats a number before the first space of an M3U channel name as a
    channel number and drops it ('60 MINUTES' -> 'MINUTES'). A no-break space after
    the number looks the same on screen but isn't a space to Jellyfin."""
    return LEADING_NUMBER.sub("\\1\u00a0", name, count=1)


def sort_key(name):
    """Alphabetical, ignoring case, with numbers compared as numbers (TSN 2 before TSN 10)."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name.lower())]


def split_extinf(line):
    """'#EXTINF:-1 tvg-name="A, B",A, B' -> ('#EXTINF:-1 tvg-name="A, B"', 'A, B')"""
    quoted = False
    for i, c in enumerate(line):
        if c == '"':
            quoted = not quoted
        elif c == "," and not quoted:
            return line[:i], line[i + 1:]
    return line, ""


def stream_url(line):
    """Dispatcharr's stream address -> this proxy's /stream/<id> (when the fix is on)."""
    m = STREAM_URL.match(line)
    return f"http://localhost:{PORT}/stream/{m.group(1)}" if TIMESTAMP_FIX and m else line


def fix_playlist(m3u):
    header, channels = [], []
    for line in m3u.splitlines():
        if line.startswith("#EXTINF"):
            channels.append([line])
        elif channels:
            channels[-1].append(line)
        elif line.strip():
            header.append(line)

    entries = []
    for position, (extinf, *rest) in enumerate(channels):
        chno = CHNO_VALUE.search(extinf)
        attrs, name = split_extinf(TVG_CHNO.sub("", extinf))
        name = clean_name(name)
        attrs = TVG_NAME.sub(lambda m: m.group(1) + clean_name(m.group(2)) + m.group(3), attrs)
        if SORT_BY == "dispatcharr":
            try:
                key = (float(chno.group(1)), position)
            except (AttributeError, ValueError):
                key = (float("inf"), position)
        elif SORT_BY == "group":
            group = GROUP_TITLE.search(attrs)
            key = (sort_key(group.group(1) if group else ""), sort_key(name))
        else:
            key = sort_key(name)
        entries.append((key, f"{attrs},{keep_leading_number(name)}",
                        [stream_url(l) for l in rest if l.strip()]))

    # Stable: channels with the same name keep Dispatcharr's order.
    entries.sort(key=lambda e: e[0], reverse=SORT_DESC)
    out = list(header)
    for n, (_, extinf, rest) in enumerate(entries, 1):
        out.append(EXTINF.sub(rf'\1 tvg-chno="{n}"', extinf, count=1))
        out.extend(rest)
    return "\n".join(out) + "\n", len(entries)


def fix_guide(epg):
    # Channel id -> (original name, cleaned name), for channels whose name changes.
    renames = {}
    for cid, raw in CHANNEL.findall(epg):
        old = html.unescape(raw).strip()
        if (new := clean_name(old)) != old:
            renames[cid] = (old, new)

    def fix_programme(m):
        if m.group(2) not in renames:
            return m.group(0)
        old, new = renames[m.group(2)]
        body = DESC.sub(lambda d: d.group(1) + escape(html.unescape(d.group(2)).replace(old, new)) + d.group(3),
                        m.group(3))
        return m.group(1) + body + m.group(4)

    epg = PROGRAMME.sub(fix_programme, epg)
    return GUIDE_NAME.sub(
        lambda m: m.group(1) + escape(clean_name(html.unescape(m.group(3)))) + m.group(4), epg)


# --- MPEG-TS replay skipping -------------------------------------------------------
TS = 188
VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24}   # MPEG-1/2, MPEG-4, H.264, HEVC
HOLD_LIMIT = 16 << 20      # stop looking for a keyframe after holding back this much
MAX_SKIP = 40              # most keyframes skipped per reconnect (a replay is ~10; a
                           # still image repeats the same keyframe and mustn't stall)
IDR = re.compile(rb"\x00\x00\x01(?:[\x25\x45\x65]|[\x26\x28]\x01)")  # H.264 / HEVC IDR


def ts_payload(pkt):
    afc = (pkt[3] >> 4) & 3
    if not afc & 1:
        return b""
    off = 5 + pkt[4] if afc & 2 else 4
    return pkt[off:] if off < TS else b""


def ts_flags(pkt):
    """(random_access_indicator, discontinuity_indicator) of a TS packet."""
    if (pkt[3] >> 4) & 2 and pkt[4] > 0:
        return bool(pkt[5] & 0x40), bool(pkt[5] & 0x80)
    return False, False


def pes_time(p):
    """DTS (or PTS when there's no DTS) of a PES header, 90 kHz; None if absent."""
    if len(p) < 14 or p[:3] != b"\x00\x00\x01" or not p[7] & 0x80:
        return None
    o = 14 if (p[7] & 0xC0) == 0xC0 and len(p) >= 19 else 9
    b = p[o:o + 5]
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def pes_body(p):
    return p[9 + p[8]:] if len(p) >= 9 and p[:3] == b"\x00\x00\x01" else p


class ReplaySkipper:
    """Cuts the replayed part out of a channel's MPEG-TS stream after a reconnect.

    In normal play every packet passes, and a hash of each video keyframe's whole
    picture is remembered (the start alone isn't enough: static areas such as black
    bars make the first part of different keyframes identical). After a reconnect (Dispatcharr's discontinuity marker, a jump
    in the video's timestamps, or a new connection to Dispatcharr) packets are held
    back until the next keyframe. A keyframe that was already sent means a replay: it
    and everything held are dropped, and the next keyframe is checked. The first
    keyframe not sent before is where playback carries on, so the viewer jumps ahead
    a moment (past the rest of the last picture group) instead of seeing the replay."""

    def __init__(self, log):
        self.log = log
        self.buf = b""
        self.pmt_pids, self.video_pid = set(), None
        self.sent = collections.deque(maxlen=900)   # recent keyframe hashes (~15+ min)
        self.checking, self.held, self.skipped = False, [], 0
        self.key = self.key_at = None
        self.last_time = {}                         # PID -> last PES timestamp

    def new_connection(self):
        self.buf = b""
        self._start_check()

    def feed(self, data):
        buf, out, i = self.buf + data, [], 0
        while len(buf) - i >= TS:
            if buf[i] != 0x47:          # lost sync: skip to the next packet start
                j = buf.find(b"\x47", i + 1)
                i = j if j >= 0 else len(buf)
                continue
            self._packet(buf[i:i + TS], out)
            i += TS
        self.buf = buf[i:]
        return b"".join(out)

    def _start_check(self):
        if not self.checking:
            self.checking, self.held, self.skipped = True, [], 0
            self.key = None

    def _jumped(self, pid, t, disc):
        """A reconnect shows on any stream first (often audio, a moment before the
        video): Dispatcharr's discontinuity marker, or a jump in that stream's time."""
        last = self.last_time.get(pid)
        if t is not None:
            self.last_time[pid] = t
        if disc:
            return True
        if t is None or last is None:
            return False
        d = (t - last) % (1 << 33)
        if d >= 1 << 32:
            d -= 1 << 33
        return d < -45000 or d > 900000            # back over 0.5 s, or ahead over 10 s

    def _packet(self, pkt, out):
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        start = bool(pkt[1] & 0x40)
        if start and pid == 0:
            self._pat(ts_payload(pkt))
        elif start and pid in self.pmt_pids:
            self._pmt(ts_payload(pkt))
        if pid == self.video_pid:
            payload = ts_payload(pkt)
            if start:
                self._close_key(out)
                rai, disc = ts_flags(pkt)
                if self._jumped(pid, pes_time(payload), disc) and not self.checking:
                    self._start_check()
                body = pes_body(payload)
                if rai or IDR.search(body):
                    self.key, self.key_at = hashlib.sha1(body), len(self.held)
            elif self.key is not None:
                self.key.update(payload)
        elif pid != 0 and pid not in self.pmt_pids and pid != 0x1FFF:   # audio etc.
            disc = ts_flags(pkt)[1]
            t = pes_time(ts_payload(pkt)) if start else None
            if (start or disc) and self._jumped(pid, t, disc) and not self.checking:
                self._start_check()
        (self.held if self.checking else out).append(pkt)
        if self.checking and len(self.held) * TS > HOLD_LIMIT:
            self.log(f"no keyframe found after a reconnect; passing {len(self.held) * TS >> 20} MB through")
            out.extend(self.held)
            self.checking, self.held = False, []

    def _close_key(self, out):
        """Called when the keyframe's picture is complete (the next video packet starts)."""
        if self.key is None:
            return
        h = self.key.digest()
        at, self.key = self.key_at, None
        if not self.checking:
            self.sent.append(h)
        elif h in self.sent and self.skipped < MAX_SKIP:   # replayed picture: drop it all
            self.skipped += 1
            self.held = []
        else:                                      # new picture: carry on from here
            out.extend(self.held[at:])
            if self.skipped:
                self.log(f"skipped a replay ({self.skipped} keyframes)")
            self.checking, self.held = False, []
            self.sent.append(h)

    def _pat(self, p):
        if not p or len(p) <= p[0] + 8:
            return
        s = p[1 + p[0]:]
        if s[0] != 0:
            return
        end = min(3 + (((s[1] & 0x0F) << 8) | s[2]) - 4, len(s))
        self.pmt_pids = {((s[i + 2] & 0x1F) << 8) | s[i + 3]
                         for i in range(8, end - 3, 4) if (s[i] << 8 | s[i + 1]) != 0}

    def _pmt(self, p):
        if not p or len(p) <= p[0] + 12:
            return
        s = p[1 + p[0]:]
        if s[0] != 2:
            return
        end = min(3 + (((s[1] & 0x0F) << 8) | s[2]) - 4, len(s))
        i = 12 + (((s[10] & 0x0F) << 8) | s[11])
        while i + 5 <= end:
            if s[i] in VIDEO_TYPES:
                self.video_pid = ((s[i + 1] & 0x1F) << 8) | s[i + 2]
                return
            i += 5 + (((s[i + 3] & 0x0F) << 8) | s[i + 4])


# --- /stream/<id> ------------------------------------------------------------------
def feed_ffmpeg(channel_id, proc, stop, state):
    """Reads the channel from Dispatcharr, cuts out replays, and writes it to FFmpeg.
    When Dispatcharr ends the stream (it gave up after its own reconnects), asks for
    the channel again (a fresh provider connection) until RESTART_FOR seconds pass
    without any new picture (replayed data doesn't count)."""
    def log(msg):
        print(f"stream {channel_id}: {msg}", flush=True)

    skipper = ReplaySkipper(log)
    host = urlsplit(DISPATCHARR)
    last_data, connections = time.time(), 0
    try:
        while not stop.is_set():
            why = "Dispatcharr ended the stream"
            conn = http.client.HTTPConnection(host.hostname, host.port or 80, timeout=20)
            try:
                conn.connect()
                state["sock"] = conn.sock               # so the player side can cut it
                conn.request("GET", f"/proxy/ts/stream/{channel_id}")
                resp = conn.getresponse()
                if resp.status != 200:
                    raise OSError(f"Dispatcharr answered HTTP {resp.status}")
                connections += 1
                if connections > 1:
                    log("got the channel from Dispatcharr again")
                skipper.new_connection()
                while chunk := resp.read1(65536):
                    if data := skipper.feed(chunk):
                        last_data = time.time()             # new picture, not a replay
                        try:
                            proc.stdin.write(data)
                            proc.stdin.flush()
                        except (BrokenPipeError, OSError):
                            return                      # FFmpeg is gone: player left
            except (OSError, http.client.HTTPException) as e:
                why = str(e) or e.__class__.__name__
            finally:
                conn.close()
            if stop.is_set():
                return
            if time.time() - last_data > RESTART_FOR:
                log(f"no data for {RESTART_FOR} s, giving up")
                return
            log(f"{why}; asking Dispatcharr for the channel again")
            stop.wait(3)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def relay_stream(handler, channel_id):
    """Streams the channel to the player through FFmpeg until either side hangs up.
    No -copyts: for MPEG-TS input FFmpeg shifts the timestamps after any backward
    jump (and any forward jump over 10 s) so the output keeps counting forward."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning",
           "-fflags", "+genpts+discardcorrupt",
           "-dts_delta_threshold", "1",                    # close forward gaps over 1 s too (default 10)
           "-analyzeduration", "1000000", "-probesize", "2000000",  # start within ~1 s
           "-f", "mpegts", "-i", "pipe:0",
           "-map", "0", "-c", "copy",
           "-mpegts_flags", "+resend_headers", "-f", "mpegts", "pipe:1"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    stop, state = threading.Event(), {}
    threading.Thread(target=feed_ffmpeg, args=(channel_id, proc, stop, state), daemon=True).start()
    handler.send_response(200)
    handler.send_header("Content-Type", "video/mp2t")
    handler.end_headers()
    print(f"stream {channel_id}: started", flush=True)
    reason = "stream ended"
    try:
        while chunk := proc.stdout.read1(65536):
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        reason = "player disconnected"
    finally:
        stop.set()
        if sock := state.get("sock"):                   # free the provider connection now
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        proc.kill()
        proc.wait()
    print(f"stream {channel_id}: stopped ({reason})", flush=True)


class Handler(BaseHTTPRequestHandler):
    timeout = 60   # a player that stops reading for 60 s is treated as gone
    def do_HEAD(self):  # Jellyfin checks the stream address before opening it
        if STREAM_PATH.match(self.path.split("?")[0]):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
        else:
            self.send_error(404)

    def do_GET(self):
        path = self.path.split("?")[0]
        if m := STREAM_PATH.match(path):
            relay_stream(self, m.group(1))
            return
        try:
            if path == "/LiveTv.m3u":
                body, count = fix_playlist(fetch(SOURCE_M3U))
                ctype = "audio/x-mpegurl; charset=utf-8"
                print(f"playlist: {count} channels", flush=True)
            elif path == "/Epg.xml":
                body = fix_guide(fetch(SOURCE_EPG))
                ctype = "application/xml; charset=utf-8"
                print("guide: served", flush=True)
            else:
                self.send_error(404)
                return
        except Exception as e:  # Dispatcharr down or restarting
            print(f"fetch failed for {path}: {e}", flush=True)
            self.send_error(502, "could not fetch from Dispatcharr")
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # one summary line per request is enough
        pass


if __name__ == "__main__":
    print(f"livetv-proxy on :{PORT}, source {SOURCE_M3U}, prefixes {PREFIXES}, suffixes {SUFFIXES}", flush=True)
    if TIMESTAMP_FIX:
        print("timestamp fix on: streams go through /stream/<id>", flush=True)
    elif os.environ.get("TIMESTAMP_FIX", "1") == "1":
        print("timestamp fix OFF: ffmpeg not found, streams go straight to Dispatcharr", flush=True)
    else:
        print("timestamp fix off (TIMESTAMP_FIX=0): streams go straight to Dispatcharr", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
