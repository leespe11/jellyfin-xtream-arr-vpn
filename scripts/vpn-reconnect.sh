#!/usr/bin/env bash
# Moves the VPN to another server without restarting any container. Asks gluetun's
# control server to stop and start the tunnel (it picks a random server each time, within
# SERVER_REGIONS / SERVER_HOSTNAMES), tries again until the public IP actually changes,
# then has qBittorrent announce its new IP to the trackers. Needs qBittorrent bound to
# tun0 (see README) and gluetun's HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE line.
# Usage: vpn-reconnect.sh
set -uo pipefail
cd "$(dirname "$0")/.."
source scripts/lib.sh

# One move at a time: both watchdogs (and you, by hand) can call this.
exec 9>/tmp/vpn-reconnect.lock
flock -n 9 || { echo "another VPN move is already running"; exit 1; }

API=http://localhost:8000/v1
# gluetun has no curl; qBittorrent shares its network, so the requests go from there.
ctl() { docker exec qbittorrent curl -fsS -m 15 "$@"; }
# The public IP (with the city when gluetun knows it). gluetun looks the IP up through
# outside services that sometimes all fail ("all fetchers failed") although the tunnel
# works; then an IP echo service is asked through the tunnel directly (qBittorrent
# shares it), so a working connection isn't mistaken for none.
where() {
  local ip url
  ip=$(ctl "$API/publicip/ip" 2>/dev/null | jq -r 'select((.public_ip // "") != "") | "\(.public_ip) (\(.city))"')
  [ -n "$ip" ] && { echo "$ip"; return; }
  for url in https://api.ipify.org https://icanhazip.com https://ifconfig.me/ip; do
    ip=$(docker exec qbittorrent curl -fsS -m 8 "$url" 2>/dev/null | tr -d '[:space:]')
    [[ $ip =~ ^[0-9a-fA-F.:]+$ ]] && { echo "$ip"; return; }
  done
}
vpn() { ctl -X PUT -H 'Content-Type: application/json' -d "{\"status\":\"$1\"}" "$API/vpn/status" >/dev/null; }

before=$(where); after=""
for try in 1 2 3 4 5; do
  vpn stopped; vpn running
  for _ in $(seq 10); do          # up to ~30 s for gluetun to report the new IP
    sleep 3; after=$(where)
    # Compare the addresses only: one of them may come without a city.
    [ -n "$after" ] && [ "${after%% *}" != "${before%% *}" ] && break 2
  done
  echo "try $try: still on ${after:-no connection}, trying again"
done
if [ -z "$after" ] || [ "${after%% *}" = "${before%% *}" ]; then
  echo "VPN did not move off ${before:-unknown}"; exit 1
fi

qbit_login && curl -fsS -b "$WORK/qbit-cookie" -d hashes=all "$QBIT_URL/api/v2/torrents/reannounce" \
  || echo "could not tell qBittorrent to reannounce"
echo "VPN moved: ${before:-unknown} -> $after"
