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
             every picture sent is remembered (a hash of its data), and after a
             reconnect everything is held back while the replay arrives. Pictures
             already sent are dropped, and playback carries on with the first picture
             that wasn't sent, so the viewer sees a short pause but misses nothing.
             Audio is cut by its timestamps instead (Dispatcharr re-encodes it, so a
             replay's audio has other bytes): that picture gives the offset between the
             old and new connection's clocks, audio up to the end of the last audio sent
             is dropped (to the nearest AAC frame), and the rest follows on from it with
             no hole or overlap, so sound and picture can't drift apart. Only when the
             provider's replay doesn't reach back far enough (a real gap) does it carry
             on from the next keyframe instead. The proxy then moves the new
             connection's timestamps to follow on from the last picture sent, so FFmpeg
             never sees a jump.
             The channel goes on to FFmpeg at playback speed, with a cushion of whatever
             arrived early: the provider sends its last ~30 s at once on every
             (re)connect, so the cushion fills at once, and the player plays on from it
             while Dispatcharr reconnects instead of waiting. Playback starts once
             BUFFER seconds are queued (at once with such a burst). If
             Dispatcharr gives up on the channel, it's asked for it again (a fresh
             provider connection) for up to RESTART_FOR seconds while the player waits;
             if Dispatcharr keeps refusing it (stopped in its web UI) for REFUSED_FOR
             seconds, the proxy gives up.
             When a player stops or changes channel, Jellyfin hangs up on the proxy at
             once and the stream ends here within a second or two. But Jellyfin sometimes
             keeps a channel open after the player has gone (it still counts a viewer
             that's gone, so it keeps reading). The proxy's only client is Jellyfin, so
             it can't see the player leave; instead, every CHECK_EVERY seconds while a
             stream runs it asks Jellyfin which channels are being played. One nobody
             has played for IDLE_AFTER seconds is closed in Jellyfin, and cut here if
             Jellyfin keeps reading 10 s later; that frees the provider connection.
             A new stream gets IDLE_AFTER seconds before it has to show up as played.
             This is skipped while Jellyfin is recording, and whenever Jellyfin can't
             be asked.
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
  BUFFER          seconds of cushion to build before a channel starts playing (default 8;
                  0 = no cushion, send everything as it arrives)
  RESTART_FOR     seconds to keep asking Dispatcharr for a channel it gave up on (default 60)
  REFUSED_FOR     seconds of Dispatcharr refusing a channel (HTTP 503) before giving up (default 10)
  IDLE_AFTER      seconds a channel nobody is playing in Jellyfin stays open (default 20; 0 = off)
  CHECK_EVERY     seconds between asking Jellyfin, while a stream runs (default 5)
  JELLYFIN_URL    default http://localhost:8096
  JELLYFIN_API_KEY, or JELLYFIN_USER and JELLYFIN_PASSWORD (an admin): to ask Jellyfin
                  what's being played. Without them IDLE_AFTER does nothing.
  PORT            default 8097

Nothing is stored on disk.
Tuner URL: http://localhost:8097/LiveTv.m3u   Guide URL: http://localhost:8097/Epg.xml
"""
import collections
import hashlib
import html
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

SOURCE_M3U = os.environ.get("SOURCE_M3U", "http://localhost:9191/output/m3u")
SOURCE_EPG = os.environ.get("SOURCE_EPG", "http://localhost:9191/output/epg")
PORT = int(os.environ.get("PORT", "8097"))
DISPATCHARR = "{0.scheme}://{0.netloc}".format(urlsplit(SOURCE_M3U))
TIMESTAMP_FIX = os.environ.get("TIMESTAMP_FIX", "1") == "1" and shutil.which("ffmpeg") is not None
RESTART_FOR = int(os.environ.get("RESTART_FOR", "60"))
REFUSED_FOR = int(os.environ.get("REFUSED_FOR", "10"))
IDLE_AFTER = int(os.environ.get("IDLE_AFTER", "20"))
CHECK_EVERY = max(1, int(os.environ.get("CHECK_EVERY", "5")))
BUFFER = max(0.0, float(os.environ.get("BUFFER", "8") or 0))
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://localhost:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()
JELLYFIN_USER = os.environ.get("JELLYFIN_USER", "").strip()
JELLYFIN_PASSWORD = os.environ.get("JELLYFIN_PASSWORD", "")
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
    return proxy_url(m.group(1)) if TIMESTAMP_FIX and m else line


def proxy_url(channel_id):
    return f"http://localhost:{PORT}/stream/{channel_id}"


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
HOLD_LIMIT = 16 << 20      # stop holding back after a reconnect beyond this much
MAX_SKIP = 900             # most pictures dropped per reconnect: 15-37 s (a replay is ~10 s;
                           # a looping slate repeats the same pictures and mustn't stall)
SENT_FRAMES = 18000        # pictures remembered (~10 min at 30 fps)
IDR = re.compile(rb"\x00\x00\x01(?:[\x25\x45\x65]|[\x26\x28]\x01)")  # H.264 / HEVC IDR


def ts_pid(pkt):
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


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
    """A PES without its header (the header holds timestamps, which differ in a replay)."""
    return p[9 + p[8]:] if len(p) >= 9 and p[:3] == b"\x00\x00\x01" else p


WRAP = 1 << 33              # MPEG timestamps (90 kHz) wrap around at 2^33


def later(a, b):
    """Is timestamp a after b (allowing for the wrap-around)?"""
    d = (a - b) % WRAP
    return 0 < d < WRAP // 2


def ticks_between(a, b):
    """b - a in 90 kHz ticks, negative when b is before a (allowing for the wrap-around)."""
    d = (b - a) % WRAP
    return d - WRAP if d >= WRAP // 2 else d


NO_PES_HEADER = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF}   # PES stream ids without timestamps


def _shift_time(b, o, shift):
    t = ((b[o] >> 1) & 7) << 30 | b[o + 1] << 22 | (b[o + 2] >> 1) << 15 | b[o + 3] << 7 | b[o + 4] >> 1
    t = (t - shift) % WRAP
    b[o] = (b[o] & 0xF0) | ((t >> 29) & 0x0E) | 1
    b[o + 1] = (t >> 22) & 0xFF
    b[o + 2] = ((t >> 14) & 0xFE) | 1
    b[o + 3] = (t >> 7) & 0xFF
    b[o + 4] = ((t << 1) & 0xFE) | 1


def retime(pkt, shift):
    """A TS packet with its PCR and PES timestamps moved back by shift ticks."""
    afc = (pkt[3] >> 4) & 3
    b, off = None, 4
    if afc & 2:
        n = pkt[4]
        if n >= 7 and pkt[5] & 0x10:                # PCR
            b = bytearray(pkt)
            base = (b[6] << 25 | b[7] << 17 | b[8] << 9 | b[9] << 1 | b[10] >> 7) - shift
            base %= WRAP
            b[6:10] = (base >> 1).to_bytes(4, "big")
            b[10] = (base & 1) << 7 | (b[10] & 0x7F)
        off = 5 + n
    if pkt[1] & 0x40 and afc & 1 and off + 14 <= TS and pkt[off:off + 3] == b"\x00\x00\x01" \
            and pkt[off + 3] not in NO_PES_HEADER:
        flags = pkt[off + 7] >> 6
        if flags & 2:
            b = b or bytearray(pkt)
            _shift_time(b, off + 9, shift)          # PTS
            if flags == 3 and off + 19 <= TS:
                _shift_time(b, off + 14, shift)     # DTS
    return bytes(b) if b else pkt


ADTS_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


def adts_frames(data):
    """([(offset, length)] of the whole AAC (ADTS) frames data starts with, sample rate);
    ([], None) when it isn't ADTS."""
    i, res, rate = 0, [], None
    while i + 7 <= len(data) and data[i] == 0xFF and data[i + 1] & 0xF6 == 0xF0:
        n = ((data[i + 3] & 3) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        idx = (data[i + 2] >> 2) & 15
        if n < 7 or i + n > len(data) or idx >= len(ADTS_RATES):
            break
        rate = ADTS_RATES[idx]
        res.append((i, n))
        i += n
    return res, rate


def pts_field(t):
    return bytes([0x21 | ((t >> 29) & 0x0E), (t >> 22) & 0xFF, 0x01 | ((t >> 14) & 0xFE),
                  (t >> 7) & 0xFF, 0x01 | ((t << 1) & 0xFE)])


def packetize(pid, pes):
    """A PES split into TS packets (continuity counters are set when sent)."""
    out, first = [], True
    while pes or first:
        chunk, pes = pes[:184], pes[184:]
        head = bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF])
        stuff = 184 - len(chunk)
        if not stuff:
            out.append(head + b"\x10" + chunk)
        else:
            af = b"\x00" if stuff == 1 else bytes([stuff - 1, 0]) + b"\xff" * (stuff - 2)
            out.append(head + b"\x30" + af + chunk)
        first = False
    return out


class ReplaySkipper:
    """Cuts the replayed part out of a channel's MPEG-TS stream after a reconnect.

    In normal play every packet passes, and a hash of every picture (video PES,
    without its header, since a replay has new timestamps) is remembered. After a
    reconnect (Dispatcharr's discontinuity marker, a jump in a stream's timestamps, or
    a new connection to Dispatcharr) packets are held back. Pictures that were already
    sent are the replay and are dropped, each one lining the replay up with what was
    sent. Once the replay has caught up, the next picture is the first new one and
    playback carries on from it: its reference pictures are the ones the player just
    showed, so it decodes cleanly and nothing is skipped.

    Audio can't be matched the same way: Dispatcharr re-encodes it on every connection,
    so a replayed audio frame has different bytes. Instead the picture playback carries
    on with gives the offset between the old connection's clock and the new one, and
    audio is kept from the AAC frame (~23 ms) nearest to where the last audio sent ends;
    earlier audio is the replay and is dropped (cut out of its PES when needed). That
    frame is then placed exactly where the last audio sent ends: players play audio
    sample after sample and keep the picture in step with it, so even small holes or
    overlaps in the audio's timestamps would add up over many reconnects and the sound
    would drift away from the picture. Placed this way it is never more than half a
    frame off, and that doesn't add up.

    If the replay doesn't reach back to what was sent (the provider skipped ahead),
    playback can only carry on from a keyframe, as there's nothing to decode from;
    audio then starts with that picture.

    Every connection has its own clock. Rather than leave the jumps for FFmpeg to even
    out (it keeps one correction for all streams, and when audio and video jump at
    different places in the stream it can end up with the sound minutes away from the
    picture), the timestamps sent are moved onto one continuous clock: the picture
    playback carries on with gets the time right after the last picture sent, and all
    audio and video of the new connection moves with it."""

    def __init__(self, log):
        self.log = log
        self.buf = b""
        self.pmt_pids, self.video_pid = set(), None
        self.frames = collections.deque()   # hashes of recent pictures sent, oldest first
        self.where = {}                     # hash -> number of the latest picture sent with it
        self.count = 0                      # number the next picture sent gets
        self.last_dts, self.frame_ticks = None, 3754    # last picture sent; picture length
        self.video = None                   # picture being read: [sha1, held index, keyframe?, DTS]
        self.au = {}                        # PID -> audio (and other non-video) state, see _au
        self.last_time = {}                 # PID -> last PES timestamp (reconnect detection)
        self.checking = False
        self.held = []
        self.expect = None                  # number of the sent picture the replay is up to
        self.skipped = 0
        self.shift = 0                      # input clock minus output clock (ticks)
        self.out_dts = None                 # output time of the first picture in feed()'s result
        self.cc = {}                        # PID -> continuity counter of the next packet sent

    def new_connection(self):
        """The picture being read when the old connection ended went out, whole or cut
        short: count it as sent. A whole one then matches its copy in the replay; a cut
        one doesn't, and the replay's whole copy follows it."""
        self.buf = b""
        if self.video and not self.checking:
            self._remember_picture(self.video[0].digest(), self.video[3])
        self.video = None
        for pid in self.au:                         # the rest of any PES isn't coming
            self._audio_done(pid, None)
        self._start_check()

    def feed(self, data):
        buf, out, i = self.buf + data, [], 0
        self.out_dts = None
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
        if self.checking:
            return
        self.checking, self.held = True, []
        self.expect, self.skipped = None, 0
        if self.video:
            self.video[1] = None            # begun before the hold: part of it went out already

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
        d = (t - last) % WRAP
        if d >= WRAP // 2:
            d -= WRAP
        return d < -45000 or d > 900000            # back over 0.5 s, or ahead over 10 s

    def _packet(self, pkt, out):
        pid = ts_pid(pkt)
        start = bool(pkt[1] & 0x40)
        if start and pid == 0:
            self._pat(ts_payload(pkt))
        elif start and pid in self.pmt_pids:
            self._pmt(ts_payload(pkt))
        if pid == 0 or pid == 0x1FFF or pid in self.pmt_pids:
            if self.checking:
                self.held.append(pkt)
            else:
                self._send(pkt, out)
        elif pid == self.video_pid:
            self._video(pkt, start, out)
        else:
            self._audio(pid, pkt, start, out)
        if self.checking and len(self.held) * TS > HOLD_LIMIT:
            self.log(f"no place to carry on found after a reconnect; passing {len(self.held) * TS >> 20} MB through")
            self._release(0, None, None, out)

    def _video(self, pkt, start, out):
        payload = ts_payload(pkt)
        if start:
            if v := self.video:
                self.video = None
                self._picture(v[0].digest(), v[2], v[1], v[3], out)
            rai, disc = ts_flags(pkt)
            t = pes_time(payload)
            if self._jumped(self.video_pid, t, disc):
                self._start_check()
            body = pes_body(payload)
            self.video = [hashlib.sha1(body), len(self.held) if self.checking else None,
                          bool(rai or IDR.search(body)), t]
        else:
            if ts_flags(pkt)[1] and self._jumped(self.video_pid, None, True):
                self._start_check()
            if self.video:
                self.video[0].update(payload)
        if self.checking:
            self.held.append(pkt)
        else:
            self._send(pkt, out)

    def _send(self, pkt, out, shift=None):
        """Sends a packet, its timestamps moved onto the output's continuous clock and
        its continuity counter following on from the last packet sent on its PID:
        FFmpeg takes a gap in the counter (where the replay was cut out) for lost data
        and throws the picture away, and the rest of its group of pictures with it."""
        pid = ts_pid(pkt)
        if pid == 0x1FFF:
            out.append(pkt)
            return
        shift = self.shift if shift is None else shift
        if shift and pid not in self.pmt_pids and pid != 0:
            pkt = retime(pkt, shift)
        if pkt[3] & 0x10:                           # has a payload: the counter goes up
            n = self.cc.get(pid, pkt[3] & 15)
            self.cc[pid] = (n + 1) & 15
        else:                                       # adaptation field only: it stays
            n = (self.cc.get(pid, (pkt[3] + 1) & 15) - 1) & 15
        if pkt[3] & 15 != n:
            pkt = pkt[:3] + bytes([(pkt[3] & 0xF0) | n]) + pkt[4:]
        if self.out_dts is None and pkt[1] & 0x40 and pid == self.video_pid:
            self.out_dts = pes_time(ts_payload(pkt))
        out.append(pkt)

    def _audio(self, pid, pkt, start, out):
        """Audio (and subtitles etc.): held while checking, otherwise sent on."""
        disc = ts_flags(pkt)[1]
        if start:
            if self._jumped(pid, pes_time(ts_payload(pkt)), disc):
                self._start_check()
        elif disc and self._jumped(pid, None, True):
            self._start_check()
        if self.checking:
            self.held.append(pkt)
        else:
            self._audio_out(pid, pkt, start, out)

    def _au(self, pid):
        """cur: the PES being read ({t, head, data, pkts, sent, out_t}); shift: this PID's
        input minus output clock; end: output time where the last audio sent ends;
        decide: after a reconnect, the input time the next audio sent should start at
        (the PES is held until it is complete and then cut there); ticks: PES spacing."""
        if pid not in self.au:
            self.au[pid] = {"cur": None, "shift": self.shift, "end": None, "decide": None,
                            "ticks": 0, "last_t": None}
        return self.au[pid]

    def _audio_out(self, pid, pkt, start, out):
        a = self._au(pid)
        payload = ts_payload(pkt)
        if start:
            self._audio_done(pid, out)
            t = pes_time(payload)
            if t is not None:
                if a["last_t"] is not None and 0 < (t - a["last_t"]) % WRAP <= 45000:
                    a["ticks"] = (t - a["last_t"]) % WRAP
                a["last_t"] = t
            hl = 9 + payload[8] if len(payload) >= 9 and payload[:3] == b"\x00\x00\x01" else 0
            sent = a["decide"] is None
            a["cur"] = {"t": t, "head": payload[:hl], "data": bytearray(payload[hl:]), "pkts": [],
                        "sent": sent, "out_t": None if t is None or not sent else (t - a["shift"]) % WRAP}
            if sent:
                self._send(pkt, out, a["shift"])
            else:
                a["cur"]["pkts"].append(pkt)
            return
        c = a["cur"]
        if c is None:
            return                                  # the rest of a PES from a connection that ended
        c["data"] += payload
        if c["sent"]:
            self._send(pkt, out, a["shift"])
        else:
            c["pkts"].append(pkt)

    def _end_of(self, a, c):
        """Output time where the sent PES c ends (its whole AAC frames)."""
        if c["out_t"] is None:
            return None
        frames, rate = adts_frames(c["data"])
        ticks = round(len(frames) * 1024 * 90000 / rate) if frames else a["ticks"]
        return (c["out_t"] + ticks) % WRAP

    def _audio_done(self, pid, out):
        """The PES being read is complete (out=None: it never will be)."""
        a = self.au[pid]
        c, a["cur"] = a["cur"], None
        if c is None:
            return
        if not c["sent"] and (out is None or not self._decide(a, pid, c, out)):
            return                                  # replayed audio: dropped
        if (e := self._end_of(a, c)) is not None:
            a["end"] = e

    def _decide(self, a, pid, c, out):
        """After a reconnect: sends the part of PES c from the frame nearest the start
        floor on, placed where the last audio sent ends. False: all of it is replayed."""
        t, floor = c["t"], a["decide"]
        if t is None:
            return False
        frames, rate = adts_frames(c["data"])
        if frames:
            step = 1024 * 90000 / rate
            units = [((t + round(i * step)) % WRAP, off) for i, (off, _) in enumerate(frames)]
        else:
            step, units = a["ticks"], [(t, 0)]
        for ut, off in units:
            if later((ut + int(step // 2)) % WRAP, floor):
                break
        else:
            return False
        true_out = (ut - self.shift) % WRAP         # where it belongs next to the picture
        if a["end"] is None or ticks_between(a["end"], true_out) > 18000:
            a["shift"] = self.shift                 # audio is missing (over 0.2 s): leave the gap
        else:
            a["shift"] = (ut - a["end"]) % WRAP     # follows the last audio sent exactly
        a["decide"] = None
        if off:
            flags = c["head"][6] if len(c["head"]) > 6 else 0x80
            pes = c["head"][:4] + b"\x00\x00" + bytes([flags, 0x80, 5]) + pts_field(ut) + bytes(c["data"][off:])
            if len(pes) - 6 <= 0xFFFF:
                pes = pes[:4] + (len(pes) - 6).to_bytes(2, "big") + pes[6:]
            pkts = packetize(pid, pes)
            del c["data"][:off]
        else:
            pkts = c["pkts"]
        for pkt in pkts:
            self._send(pkt, out, a["shift"])
        c.update(t=ut, sent=True, pkts=[], out_t=(ut - a["shift"]) % WRAP)
        return True

    def _picture(self, h, key, at, dts, out):
        """A complete picture: in normal play remember it; after a reconnect decide
        whether it's the replay (drop it) or where playback carries on."""
        if not self.checking or at is None:
            self._remember_picture(h, dts)
            return
        n = self.where.get(h)
        known = n is not None and n >= self.count - len(self.frames)
        if self.expect == self.count:               # the replay has caught up: this is new
            self._carry_on(at, h, dts, self._offset(dts, 1), out, "right after the last picture sent")
        elif known and self.skipped < MAX_SKIP:     # sent already: part of the replay
            self.expect, self.skipped = n + 1, self.skipped + 1
        elif self.expect == self.count - 1:         # the last picture sent was cut short
            self._carry_on(at, h, dts, self._offset(dts, 0), out, "with the picture the drop cut short")
        elif key or self.skipped >= MAX_SKIP:
            how = "at a keyframe (the replay didn't reach back far enough)" if not known \
                else "after too long a replay (a still image?)"
            self._carry_on(at, h, dts, None, out, how)
        else:                                       # new, but can't be decoded from here
            self.skipped += 1

    def _offset(self, dts, after):
        """New clock minus old clock, from the picture playback carries on with: it is
        the last picture sent (after=0) or the one after it (after=1)."""
        if dts is None or self.last_dts is None:
            return None
        return (dts - self.last_dts - after * self.frame_ticks) % WRAP

    def _carry_on(self, at, h, dts, offset, out, how):
        if self.count and (self.skipped or "keyframe" in how):
            self.log(f"after a reconnect: dropped {self.skipped} replayed pictures; carrying on {how}")
        self._release(at, dts, offset, out)
        self._remember_picture(h, dts)

    def _release(self, at, dts, offset, out):
        """Ends the hold: sends the held pictures from index `at` on, and the held audio
        that isn't replayed. Audio starts where the last audio sent ends, on the new
        clock (offset); without an offset, no earlier than the picture at `at` (DTS dts)."""
        step = offset if offset is not None else self._offset(dts, 1)
        if step is not None:                        # the new picture follows the last one sent
            self.shift = (self.shift + step) % WRAP
        psi = {0, 0x1FFF} | self.pmt_pids
        pids = (set(self.au) | {ts_pid(p) for p in self.held}) - psi - {self.video_pid}
        for pid in pids:
            a = self._au(pid)
            c = a["cur"]
            if c and c["sent"] and (e := self._end_of(a, c)) is not None:
                a["end"] = e
            floor = None
            if offset is not None or dts is not None:
                if a["end"] is not None:
                    floor = (a["end"] + self.shift) % WRAP
                if offset is None and dts is not None and (floor is None or later(dts, floor)):
                    floor = dts
            a["decide"] = floor
            if floor is None:
                a["shift"] = self.shift
        held, self.checking, self.held = self.held, False, []
        for i, pkt in enumerate(held):
            pid = ts_pid(pkt)
            if pid in psi or pid == self.video_pid:
                if i >= at:                         # earlier: replayed or undecodable pictures
                    self._send(pkt, out)
            else:
                self._audio_out(pid, pkt, bool(pkt[1] & 0x40), out)

    def _remember_picture(self, h, dts):
        if dts is not None and self.last_dts is not None:
            d = (dts - self.last_dts) % WRAP
            if 0 < d <= 9000:                       # a plausible picture length (10-90 fps)
                self.frame_ticks = d
        self.last_dts = dts
        self.where[h] = self.count
        self.frames.append(h)
        self.count += 1
        if len(self.frames) > SENT_FRAMES:
            old, number = self.frames.popleft(), self.count - len(self.frames) - 1
            if self.where.get(old) == number:
                del self.where[old]

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


# --- Jellyfin: which channels are being played -------------------------------------
def jellyfin_id(text):
    """Jellyfin's id for a string: the MD5 of its UTF-16 text, written as a .NET Guid.
    Its media source id for an M3U channel is this of the channel's stream address."""
    return uuid.UUID(bytes_le=hashlib.md5(text.encode("utf-16-le")).digest()).hex


# Jellyfin's id for an open live stream: these two, then the channel's media source id.
LIVE_STREAM_PREFIX = (jellyfin_id("Jellyfin.LiveTv.LiveTvMediaSourceProvider") + "_"
                      + jellyfin_id("Jellyfin.LiveTv.DefaultLiveTvService") + "_")


class Jellyfin:
    AUTH = 'MediaBrowser Client="livetv-proxy", Device="livetv-proxy", DeviceId="livetv-proxy", Version="1.0"'

    def __init__(self):
        self.token = JELLYFIN_API_KEY or None
        self.can_login = bool(JELLYFIN_USER) and not JELLYFIN_API_KEY

    @property
    def usable(self):
        return bool(self.token or self.can_login)

    def _call(self, method, path, body=None):
        auth = self.AUTH + (f', Token="{self.token}"' if self.token else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(JELLYFIN_URL + path, data=data, method=method,
                                     headers={"Authorization": auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def call(self, method, path):
        """Signed in (logging in first when there's no key); signs in again once on 401."""
        for attempt in (1, 2):
            if not self.token and self.can_login:
                self.token = self._call("POST", "/Users/AuthenticateByName",
                                        {"Username": JELLYFIN_USER, "Pw": JELLYFIN_PASSWORD})["AccessToken"]
            try:
                return self._call(method, path)
            except urllib.error.HTTPError as e:
                if e.code != 401 or not self.can_login or attempt == 2:
                    raise
                self.token = None

    def watching(self, known):
        """(media source ids of the channels being played, None) or, when that can't be
        told and nothing should be cut, (None, why): Jellyfin is recording, or a channel
        is being played whose stream can't be identified."""
        sessions = self.call("GET", "/Sessions")
        if (self.call("GET", "/LiveTv/Recordings?IsInProgress=true&Limit=1") or {}).get("TotalRecordCount"):
            return None, "Jellyfin is recording"
        ids = set()
        for s in sessions or []:
            item = s.get("NowPlayingItem")
            if not item or item.get("Type") != "TvChannel":
                continue
            ps = s.get("PlayState") or {}
            live, source = ps.get("LiveStreamId") or "", ps.get("MediaSourceId") or ""
            if live.startswith(LIVE_STREAM_PREFIX):     # an M3U channel's open stream
                ids.add(live[len(LIVE_STREAM_PREFIX):])
            elif source in known:
                ids.add(source)
            else:
                return None, (f"{s.get('Client', 'a player')} plays a channel without saying "
                              f"which stream (LiveStreamId {live!r})")
        return ids, None

    def close(self, source_id):
        self.call("POST", f"/LiveStreams/Close?liveStreamId={LIVE_STREAM_PREFIX}{source_id}")


RELAYS = set()              # streams being relayed
RELAYS_LOCK = threading.Lock()


class Relay:
    def __init__(self, channel_id, proc, stop):
        self.channel_id, self.proc, self.stop = channel_id, proc, stop
        self.source_id = jellyfin_id(proxy_url(channel_id))
        self.watched_at, self.close_asked, self.cut = time.time(), None, False

    def cut_off(self):
        self.cut = True
        self.stop.set()
        self.proc.kill()            # the player's side sees the end of the stream


def watch_viewers():
    """Every CHECK_EVERY s while a stream runs: a channel nobody has played in Jellyfin
    for IDLE_AFTER seconds is closed in Jellyfin (it still counts a viewer that's gone)
    and, if Jellyfin keeps reading anyway, cut here 10 s later."""
    jf, problem = Jellyfin(), None
    while True:
        time.sleep(CHECK_EVERY)
        with RELAYS_LOCK:
            relays = list(RELAYS)
        if not relays:
            continue
        try:
            watching, why = jf.watching({r.source_id for r in relays})
        except Exception as e:      # Jellyfin restarting, wrong login...: cut nothing
            watching, why = None, f"can't ask Jellyfin ({e})"
        if why != problem:
            print(f"idle check: {why}; leaving streams alone" if why else "idle check: working again",
                  flush=True)
            problem = why
        now = time.time()
        for r in relays:
            if watching is None or r.source_id in watching:
                r.watched_at, r.close_asked = now, None
            elif now - r.watched_at < IDLE_AFTER:
                continue
            elif r.close_asked is None:
                print(f"stream {r.channel_id}: nobody has played it in Jellyfin for "
                      f"{int(now - r.watched_at)} s; asking Jellyfin to close it", flush=True)
                r.close_asked = now
                try:
                    jf.close(r.source_id)
                except Exception as e:
                    print(f"stream {r.channel_id}: Jellyfin didn't close it ({e})", flush=True)
            elif now - r.close_asked >= 10:
                print(f"stream {r.channel_id}: Jellyfin still reading it; cutting it off", flush=True)
                r.cut_off()


# --- /stream/<id> ------------------------------------------------------------------
class Refused(OSError):
    pass


START_BURST = 2             # seconds sent at once when playback starts (FFmpeg probes it)
MAX_CUSHION = 60            # seconds of cushion kept at most; beyond it, sent at once
QUEUE_LIMIT = 96 << 20      # bytes of cushion kept at most


class Pacer:
    """Passes a channel on to FFmpeg at playback speed, going by its picture times, and
    keeps whatever arrives early as a cushion. The provider drops the connection every
    10-20 s and Dispatcharr takes a few seconds to reconnect, but on reconnecting the
    provider sends its last ~30 s at once (the replay, plus everything missed while it
    was gone). Without a cushion the player runs dry during every reconnect and then
    gets that burst; with one it plays on through the reconnect from the cushion, and
    the burst fills it up again.

    Playback starts once BUFFER seconds are queued, which is at once when the provider
    sends such a burst on connecting, or after BUFFER seconds otherwise."""

    def __init__(self, stdin, stop, log):
        self.stdin, self.stop, self.log = stdin, stop, log
        self.queue = collections.deque()    # (data, output time of its first picture or None)
        self.size = 0
        self.first = self.newest = None     # times of the first and newest picture queued
        self.cond = threading.Condition()
        self.ended = self.gone = False

    def put(self, data, dts):
        with self.cond:
            self.queue.append((data, dts))
            self.size += len(data)
            if dts is not None:
                self.newest = dts
                if self.first is None:
                    self.first = dts
            self.cond.notify()

    def end(self):
        """No more data is coming: send what's queued, then close FFmpeg's input."""
        with self.cond:
            self.ended = True
            self.cond.notify()

    def _cushion(self, sent):
        """Seconds of pictures queued after the one at time sent."""
        if sent is None or self.newest is None:
            return 0.0
        return ticks_between(sent, self.newest) / 90000

    def run(self):
        anchor = None               # (wall clock, picture time) that playback is paced from
        sent = None                 # time of the last picture sent
        waiting_since = None        # the cushion ran out at this wall clock time
        opened = time.monotonic()
        try:
            while True:
                with self.cond:
                    while not self.queue and not self.ended and not self.stop.is_set():
                        if anchor and waiting_since is None:
                            waiting_since = time.monotonic()
                        self.cond.wait(1)
                    if self.stop.is_set() or not self.queue:
                        return
                    if anchor is None and BUFFER > 0 and not self.ended \
                            and self._cushion(self.first) < BUFFER and time.monotonic() - opened < BUFFER:
                        self.cond.wait(0.2)         # building the cushion before playback starts
                        continue
                    data, dts = self.queue.popleft()
                    self.size -= len(data)
                    cushion = self._cushion(dts if dts is not None else sent)
                    full = cushion > MAX_CUSHION or self.size > QUEUE_LIMIT
                if waiting_since is not None:
                    gap = time.monotonic() - waiting_since
                    if gap >= 1:
                        self.log(f"the cushion ran out; the player waited {gap:.0f} s for Dispatcharr")
                    waiting_since = None
                if dts is not None and BUFFER > 0:
                    now = time.monotonic()
                    if anchor is None:
                        self.log(f"playing with a {cushion:.0f} s cushion")
                        anchor = (now - START_BURST, dts)
                    due = anchor[0] + ticks_between(anchor[1], dts) / 90000
                    if due < now - START_BURST - 1 or due > now + 5 or full:
                        anchor, due = (now, dts), now   # after running dry, a jump, or too much queued
                    if due > now and self.stop.wait(due - now):
                        return
                    sent = dts
                self.stdin.write(data)
                self.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.gone = True                        # FFmpeg is gone: player left
        finally:
            try:
                self.stdin.close()
            except OSError:
                pass


def feed_ffmpeg(channel_id, proc, stop, state):
    """Reads the channel from Dispatcharr, cuts out replays, and writes it to FFmpeg.
    When Dispatcharr ends the stream (it gave up after its own reconnects), asks for
    the channel again (a fresh provider connection) until RESTART_FOR seconds pass
    without any new picture (replayed data doesn't count). Gives up sooner when
    Dispatcharr refuses the channel: at once if it doesn't know it (404), after
    REFUSED_FOR seconds of 503 (the channel was stopped in Dispatcharr's web UI)."""
    def log(msg):
        print(f"stream {channel_id}: {msg}", flush=True)

    skipper = ReplaySkipper(log)
    pacer = Pacer(proc.stdin, stop, log)
    threading.Thread(target=pacer.run, daemon=True).start()
    host = urlsplit(DISPATCHARR)
    last_data, connections, refused_since = time.time(), 0, None
    try:
        while not stop.is_set():
            why = "Dispatcharr ended the stream"
            conn = http.client.HTTPConnection(host.hostname, host.port or 80, timeout=20)
            try:
                conn.connect()
                state["sock"] = conn.sock               # so the player side can cut it
                conn.request("GET", f"/proxy/ts/stream/{channel_id}")
                resp = conn.getresponse()
                if resp.status == 404:
                    log("Dispatcharr doesn't know this channel (HTTP 404); giving up")
                    return
                if resp.status != 200:
                    raise Refused(f"Dispatcharr answered HTTP {resp.status}")
                refused_since = None
                connections += 1
                if connections > 1:
                    log("got the channel from Dispatcharr again")
                skipper.new_connection()
                while chunk := resp.read1(65536):
                    if data := skipper.feed(chunk):
                        last_data = time.time()             # new picture, not a replay
                        pacer.put(data, skipper.out_dts)
                    if pacer.gone:
                        return                          # FFmpeg is gone: player left
            except Refused as e:
                why = str(e)
                refused_since = refused_since or time.time()
            except (OSError, http.client.HTTPException) as e:
                why = str(e) or e.__class__.__name__
            finally:
                conn.close()
            if stop.is_set():
                return
            if refused_since and time.time() - refused_since >= REFUSED_FOR:
                log(f"{why} for {REFUSED_FOR} s (stopped in Dispatcharr?); giving up")
                return
            if time.time() - last_data > RESTART_FOR:
                log(f"no data for {RESTART_FOR} s, giving up")
                return
            log(f"{why}; asking Dispatcharr for the channel again")
            stop.wait(1)
    finally:
        pacer.end()


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
    relay = Relay(channel_id, proc, stop)
    with RELAYS_LOCK:
        RELAYS.add(relay)
    threading.Thread(target=feed_ffmpeg, args=(channel_id, proc, stop, state), daemon=True).start()
    handler.send_response(200)
    handler.send_header("Content-Type", "video/mp2t")
    handler.end_headers()
    print(f"stream {channel_id}: started", flush=True)
    reason = "stream ended"
    try:
        while chunk := proc.stdout.read1(65536):
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
        reason = "player disconnected"
    finally:
        with RELAYS_LOCK:
            RELAYS.discard(relay)
        stop.set()
        if sock := state.get("sock"):                   # free the provider connection now
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        proc.kill()
        proc.wait()
    if relay.cut:
        reason = "nobody watching in Jellyfin"
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
        if IDLE_AFTER <= 0:
            print("idle check off (IDLE_AFTER=0)", flush=True)
        elif not Jellyfin().usable:
            print("idle check off: no JELLYFIN_API_KEY or JELLYFIN_USER", flush=True)
        else:
            print(f"idle check on: channels nobody plays in Jellyfin for {IDLE_AFTER} s are closed", flush=True)
            threading.Thread(target=watch_viewers, daemon=True).start()
    elif os.environ.get("TIMESTAMP_FIX", "1") == "1":
        print("timestamp fix OFF: ffmpeg not found, streams go straight to Dispatcharr", flush=True)
    else:
        print("timestamp fix off (TIMESTAMP_FIX=0): streams go straight to Dispatcharr", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
