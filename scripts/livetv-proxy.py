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
             every picture and audio frame sent is remembered (a hash of its data), and
             after a reconnect everything is held back while the replay arrives. Pictures
             and audio already sent are dropped, and playback carries on with the first
             picture that wasn't sent, so the viewer sees a short pause but misses
             nothing. Only when the provider's replay doesn't reach back far enough
             (a real gap) does it carry on from the next keyframe instead. If
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
SENT_OTHER = 40000         # audio (and other) frames remembered (~10 min)
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


class ReplaySkipper:
    """Cuts the replayed part out of a channel's MPEG-TS stream after a reconnect.

    In normal play every packet passes, and a hash of every picture (video PES) and
    every audio frame (other PES) sent is remembered, without the PES headers since a
    replay has new timestamps. After a reconnect (Dispatcharr's discontinuity marker, a
    jump in a stream's timestamps, or a new connection to Dispatcharr) packets are held
    back. Pictures that were already sent are the replay and are dropped, each one
    lining the replay up with what was sent. Once the replay has caught up, the next
    picture is the first new one and playback carries on from it: its reference
    pictures are the ones the player just showed, so it decodes cleanly and nothing is
    skipped. Held audio already sent is dropped and new audio kept, wherever it sits.
    If the replay doesn't reach back to what was sent (the provider skipped ahead),
    playback can only carry on from a keyframe, as there's nothing to decode from."""

    def __init__(self, log):
        self.log = log
        self.buf = b""
        self.pmt_pids, self.video_pid = set(), None
        self.frames = collections.deque()   # hashes of recent pictures sent, oldest first
        self.where = {}                     # hash -> number of the latest picture sent with it
        self.count = 0                      # number the next picture sent gets
        self.other = collections.deque()    # hashes of recent audio frames sent
        self.other_seen = collections.Counter()
        self.pes = {}       # PID -> [sha1, held indexes (None: keep), is keyframe, first held index]
        self.last_time = {}                 # PID -> last PES timestamp
        self.checking = False
        self.held, self.drop = [], set()
        self.expect = None                  # number of the sent picture the replay is up to
        self.skipped = self.dropped_audio = 0

    def new_connection(self):
        """The frames being read when the old connection ended went out, whole or cut
        short: count them as sent. A whole one then matches its copy in the replay; a
        cut one doesn't, and the replay's whole copy follows it."""
        self.buf = b""
        if not self.checking:
            for pid, st in self.pes.items():
                h = st[0].digest()
                self._remember_picture(h) if pid == self.video_pid else self._remember_other(h)
        self.pes.clear()
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
        if self.checking:
            return
        self.checking, self.held, self.drop = True, [], set()
        self.expect, self.skipped, self.dropped_audio = None, 0, 0
        for st in self.pes.values():    # begun before the hold: part of it went out already
            st[1], st[3] = None, None

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
        pid = ts_pid(pkt)
        start = bool(pkt[1] & 0x40)
        if start and pid == 0:
            self._pat(ts_payload(pkt))
        elif start and pid in self.pmt_pids:
            self._pmt(ts_payload(pkt))
        if pid != 0 and pid != 0x1FFF and pid not in self.pmt_pids:     # video, audio etc.
            payload = ts_payload(pkt)
            rai, disc = ts_flags(pkt)
            if start:
                self._end_pes(pid, out)
                if self._jumped(pid, pes_time(payload), disc):
                    self._start_check()
                body = pes_body(payload)
                key = pid == self.video_pid and bool(rai or IDR.search(body))
                at = len(self.held) if self.checking else None
                self.pes[pid] = [hashlib.sha1(body), [] if self.checking else None, key, at]
            else:
                if disc and self._jumped(pid, None, disc):
                    self._start_check()
                if st := self.pes.get(pid):
                    st[0].update(payload)
            if self.checking and (st := self.pes.get(pid)) and st[1] is not None:
                st[1].append(len(self.held))
        (self.held if self.checking else out).append(pkt)
        if self.checking and len(self.held) * TS > HOLD_LIMIT:
            self.log(f"no place to carry on found after a reconnect; passing {len(self.held) * TS >> 20} MB through")
            self._release(0, out)

    def _end_pes(self, pid, out):
        st = self.pes.pop(pid, None)
        if st is None:
            return
        h = st[0].digest()
        if pid == self.video_pid:
            self._picture(h, st[2], st[3], out)
        elif self.checking and st[1] is not None and self.other_seen[h]:
            self.drop.update(st[1])                 # audio the player already has
            self.dropped_audio += 1
        else:
            self._remember_other(h)

    def _picture(self, h, key, at, out):
        """A complete picture: in normal play remember it; after a reconnect decide
        whether it's the replay (drop it) or where playback carries on."""
        if not self.checking or at is None:
            self._remember_picture(h)
            return
        n = self.where.get(h)
        known = n is not None and n >= self.count - len(self.frames)
        if self.expect == self.count:               # the replay has caught up: this is new
            self._carry_on(at, h, out, "right after the last picture sent")
        elif known and self.skipped < MAX_SKIP:     # sent already: part of the replay
            self.expect, self.skipped = n + 1, self.skipped + 1
        elif self.expect == self.count - 1:         # the last picture sent was cut short
            self._carry_on(at, h, out, "with the picture the drop cut short")
        elif key or self.skipped >= MAX_SKIP:
            how = "at a keyframe (the replay didn't reach back far enough)" if not known \
                else "after too long a replay (a still image?)"
            self._carry_on(at, h, out, how)
        else:                                       # new, but can't be decoded from here
            self.skipped += 1

    def _carry_on(self, at, h, out, how):
        if self.count and (self.skipped or self.dropped_audio or "keyframe" in how):
            self.log(f"after a reconnect: dropped {self.skipped} pictures, {self.dropped_audio} audio "
                     f"frames; carrying on {how}")
        self._release(at, out)
        self._remember_picture(h)

    def _release(self, at, out):
        """Ends the hold: sends what was held from index `at` on, plus held audio from
        before it that the player doesn't have yet."""
        for i, pkt in enumerate(self.held):
            if i in self.drop:
                continue
            if i >= at:
                out.append(pkt)
            else:
                pid = ts_pid(pkt)
                if pid != self.video_pid and pid not in (0, 0x1FFF) and pid not in self.pmt_pids:
                    out.append(pkt)
        self.checking, self.held, self.drop = False, [], set()
        for st in self.pes.values():
            st[1], st[3] = None, None

    def _remember_picture(self, h):
        self.where[h] = self.count
        self.frames.append(h)
        self.count += 1
        if len(self.frames) > SENT_FRAMES:
            old, number = self.frames.popleft(), self.count - len(self.frames) - 1
            if self.where.get(old) == number:
                del self.where[old]

    def _remember_other(self, h):
        self.other.append(h)
        self.other_seen[h] += 1
        if len(self.other) > SENT_OTHER:
            old = self.other.popleft()
            self.other_seen[old] -= 1
            if not self.other_seen[old]:
                del self.other_seen[old]

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
                        try:
                            proc.stdin.write(data)
                            proc.stdin.flush()
                        except (BrokenPipeError, OSError):
                            return                      # FFmpeg is gone: player left
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
