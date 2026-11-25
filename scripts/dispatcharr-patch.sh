#!/bin/bash
# Start-up wrapper for the Dispatcharr container (its entrypoint in docker-compose.yml).
# When the provider drops a stream, Dispatcharr reconnects 3 times within ~2.5 seconds,
# but the provider (one connection per account) can refuse new connections for a few
# seconds after a drop, so all 3 fail and Jellyfin goes back to the guide. Dispatcharr
# also only forgets failures after 30 minutes without one, so routine drops add up.
# These values are fixed in Dispatcharr's code, not settings: this edits them, then
# starts Dispatcharr as usual. If an update changes those lines, Dispatcharr starts
# unpatched and this says so in its log:  docker logs dispatcharr 2>&1 | grep patch
# To undo: remove this script's entrypoint and volume lines from docker-compose.yml,
# then `docker compose up -d dispatcharr`.
MAX_RETRIES=6       # attempts before giving up on a stream            (Dispatcharr: 3)
RETRY_STEP=1        # wait after the nth failure = n x this, seconds   (Dispatcharr: 0.25)
RETRY_MAX_WAIT=5    # ...but at most this many seconds                 (Dispatcharr: 3)
RETRY_WINDOW=10     # forget failures after this long without one, s   (Dispatcharr: 1800)
                    # (failed attempts come <=5 s apart, so they still add up; a drop
                    # after 10+ s of playback starts the count again)
# With these: waits of 1, 2, 3, 4, 5 s, about 15 s of retrying before giving up.
# Shorter waits (0.5-2 s, tried 2026-11-15) didn't help: the provider serves ~10 s
# chunks and a new connection starts at the beginning of the current one, so a quick
# reconnect often gets a replay of what was just played (a freeze while livetv-proxy
# skips it) instead of the next chunk.

CONFIG=/app/apps/proxy/config.py
MANAGER=/app/apps/proxy/live_proxy/input/manager.py

say() { echo "[dispatcharr-patch] $*"; }

edit() {  # file, what, extended regex, replacement, expected number of lines
  local n; n=$(grep -cE "$3" "$1" 2>/dev/null)
  if [ "${n:-0}" != "$5" ]; then
    say "WARNING: expected $5 line(s) for '$2' in $1, found ${n:-0}; left unchanged"
    return
  fi
  sed -i -E "s/$3/$4/" "$1" && say "$2"
  # Drop the compiled copy so Python reads the edited file.
  rm -f "$(dirname "$1")/__pycache__/$(basename "$1" .py)".*.pyc
}

# Patterns match any current value, so a restart (files already edited) works too.
edit "$CONFIG" "MAX_RETRIES = $MAX_RETRIES" \
  '^([[:space:]]*MAX_RETRIES = )[0-9]+' "\\1$MAX_RETRIES" 1
edit "$CONFIG" "RETRY_WINDOW_SECONDS = $RETRY_WINDOW" \
  '^([[:space:]]*RETRY_WINDOW_SECONDS = )[0-9]+' "\\1$RETRY_WINDOW" 1
edit "$MANAGER" "reconnect wait = min($RETRY_STEP x failures, $RETRY_MAX_WAIT) s" \
  'timeout = min\([0-9.]+ \* failures, [0-9.]+\)' "timeout = min($RETRY_STEP * failures, $RETRY_MAX_WAIT)" 2

exec /app/docker/entrypoint.sh "$@"
