#!/usr/bin/env bash
# Run by cron every minute. When Dispatcharr's log shows the IPTV provider refusing a
# stream with "403 Forbidden" (it blocks some VPN addresses), moves the VPN to another
# server with scripts/vpn-reconnect.sh; no containers are restarted. Only reads the log,
# so it never contacts the provider itself.
# Waits 10 minutes between moves. After 3 moves within 2 hours it stops and reports to
# Discord instead, since then it's probably not the VPN address (provider down, or a
# problem with the account).
# Prints (to /var/log/nightly.log via cron) only when it acts.
set -uo pipefail
cd "$(dirname "$0")/.."
source scripts/lib.sh

STATE=/tmp/livetv-watchdog    # emptied by each reboot
COOLDOWN=600                  # at least 10 minutes between VPN moves
WINDOW=7200                   # ...and at most MAX_MOVES of them in 2 hours
MAX_MOVES=3
mkdir -p "$STATE"

notify() {  # title, text
  local hook name; hook=$(env_val DISCORD_WEBHOOK); [ -n "$hook" ] || return 0
  name=$(env_val SERVER_NAME); name=${name:-$(hostname)}
  jq -n --arg t "$name: $1" --arg d "$2" \
    '{username:"Media Server", embeds:[{title:$t, description:$d, color:15105570, timestamp:(now|todate)}]}' \
  | curl -fsS -m 15 -H 'Content-Type: application/json' -d @- "$hook" >/dev/null || true
}

# Leave it alone while the nightly job runs or the stack is still coming up; the log
# lines from that time are checked on the next run.
pgrep -f 'scripts/(nightly|after-boot)\.sh' >/dev/null && exit 0
[ "$(docker inspect -f '{{.State.Health.Status}}' gluetun 2>/dev/null)" = healthy ] || exit 0

# Only the log lines written since the last check.
now=$(date +%s)
since=$(cat "$STATE/last-check" 2>/dev/null || echo $((now - 60)))
echo "$now" > "$STATE/last-check"
hits=$(docker logs --since "$since" dispatcharr 2>&1 | grep -c 'HTTP error 403')
[ "$hits" -gt 0 ] || exit 0

last=$(stat -c %Y "$STATE/last-move" 2>/dev/null || echo 0)
[ $((now - last)) -ge "$COOLDOWN" ] || exit 0

moves=$(awk -v t=$((now - WINDOW)) '$1 > t' "$STATE/moves" 2>/dev/null | wc -l)
if [ "$moves" -ge "$MAX_MOVES" ]; then
  if [ ! -f "$STATE/gave-up" ]; then
    touch "$STATE/gave-up"
    echo "$(date) livetv-watchdog: provider still returns 403 after $MAX_MOVES VPN moves in 2 hours; not moving again for now"
    notify "⚠️ Live TV still blocked" "The IPTV provider kept refusing streams (403 Forbidden) after $MAX_MOVES VPN moves in 2 hours, so it's probably not the VPN address. Check the provider and the account."
  fi
  exit 0
fi
rm -f "$STATE/gave-up"
echo "$now" >> "$STATE/moves"; touch "$STATE/last-move"

echo "$(date) livetv-watchdog: provider returned 403 Forbidden ($hits log lines); moving the VPN"
if out=$(scripts/vpn-reconnect.sh 2>&1); then
  echo "$out"; notify "📺 moved VPN server" "The IPTV provider refused a stream (403 Forbidden)."$'\n'"$(tail -1 <<<"$out")"
else
  echo "$out"; notify "⚠️ VPN move failed" "The IPTV provider refused a stream (403 Forbidden)."$'\n'"$out"
fi
