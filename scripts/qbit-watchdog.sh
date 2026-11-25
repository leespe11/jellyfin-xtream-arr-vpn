#!/usr/bin/env bash
# Run by cron every 5 minutes. Moves the VPN to another server (scripts/vpn-reconnect.sh,
# no containers restarted) when the torrents show the signs of a server that blocks
# torrent traffic:
#   - none of them can reach its tracker, or
#   - a download has seeders in the swarm but hasn't connected to any of them.
# It must see the problem on two checks in a row, waits 30 minutes between moves, and
# after 3 moves that didn't help it stops and reports to Discord instead.
# Prints (to /var/log/nightly.log via cron) only when it acts.
set -uo pipefail
cd "$(dirname "$0")/.."
source scripts/lib.sh

STATE=/tmp/qbit-watchdog       # emptied by each reboot; cleared too whenever torrents are healthy
MIN_AGE=600                    # ignore torrents added in the last 10 minutes
COOLDOWN=1800                  # at least 30 minutes between VPN moves
MAX_MOVES=3
mkdir -p "$STATE"

notify() {  # title, text
  local hook name; hook=$(env_val DISCORD_WEBHOOK); [ -n "$hook" ] || return 0
  name=$(env_val SERVER_NAME); name=${name:-$(hostname)}
  jq -n --arg t "$name: $1" --arg d "$2" \
    '{username:"Media Server", embeds:[{title:$t, description:$d, color:15105570, timestamp:(now|todate)}]}' \
  | curl -fsS -m 15 -H 'Content-Type: application/json' -d @- "$hook" >/dev/null || true
}

# Leave it alone while the nightly job runs or the stack is still coming up.
pgrep -f 'scripts/(nightly|after-boot)\.sh' >/dev/null && exit 0
[ "$(docker inspect -f '{{.State.Health.Status}}' gluetun 2>/dev/null)" = healthy ] || exit 0
qbit_login 2>/dev/null || exit 0
torrents=$(curl -fsS -b "$WORK/qbit-cookie" "$QBIT_URL/api/v2/torrents/info") || exit 0

# .tracker is the tracker qBittorrent is currently using; it's empty when none works.
reason=$(jq -r --argjson age "$MIN_AGE" '
  [.[] | select((now - .added_on) > $age and .trackers_count > 0
                and (.state | test("^(stalled|uploading|downloading|forced|meta)")))] as $active
  | [$active[] | select(.state == "stalledDL" and .num_complete >= 3 and .num_seeds == 0)] as $stuck
  | if ($active | length) > 0 and all($active[]; .tracker == "") then
      "none of the \($active | length) torrents can reach their tracker"
    elif ($stuck | length) > 0 then
      "\($stuck | length) download(s) have seeders but connect to none (e.g. \($stuck[0].name), 0 of \($stuck[0].num_complete))"
    else empty end' <<<"$torrents")

if [ -z "$reason" ]; then
  rm -f "$STATE"/{strikes,moves,gave-up}; exit 0
fi
strikes=$(( $(cat "$STATE/strikes" 2>/dev/null || echo 0) + 1 )); echo "$strikes" > "$STATE/strikes"
[ "$strikes" -ge 2 ] || exit 0
if [ -f "$STATE/last-move" ] && \
   [ $(( $(date +%s) - $(stat -c %Y "$STATE/last-move") )) -lt "$COOLDOWN" ]; then
  exit 0
fi

moves=$(( $(cat "$STATE/moves" 2>/dev/null || echo 0) + 1 ))
if [ "$moves" -gt "$MAX_MOVES" ]; then
  if [ ! -f "$STATE/gave-up" ]; then
    touch "$STATE/gave-up"
    echo "$(date) qbit-watchdog: still failing after $MAX_MOVES VPN moves, giving up: $reason"
    notify "⚠️ torrents still stuck" "Moved the VPN $MAX_MOVES times and it didn't help, so it's probably not the server. $reason"
  fi
  exit 0
fi
echo "$moves" > "$STATE/moves"; touch "$STATE/last-move"; rm -f "$STATE/strikes"

echo "$(date) qbit-watchdog: $reason; moving the VPN (move $moves of $MAX_MOVES)"
if out=$(scripts/vpn-reconnect.sh 2>&1); then
  echo "$out"; notify "🔄 moved VPN server" "$reason"$'\n'"$(tail -1 <<<"$out")"
else
  echo "$out"; notify "⚠️ VPN move failed" "$reason"$'\n'"$out"
fi
