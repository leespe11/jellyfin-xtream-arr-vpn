#!/usr/bin/env bash
# Run at boot by cron (@reboot), and by nightly.sh on nights it doesn't reboot.
# The containers sharing gluetun's network lose it when gluetun restarts or is
# recreated (after a reboot if they started before gluetun, or after port-sync.sh
# recreates gluetun). This waits for the VPN, recreates only those containers, and
# stops qBittorrent if its traffic isn't going through the VPN.
set -uo pipefail
cd "$(dirname "$0")/.."
echo; echo "=========== $(date) after-boot ==========="
DEPENDENTS="qbittorrent jellyfin livetv-proxy dispatcharr"   # network_mode: service:gluetun

health() { docker inspect -f '{{.State.Health.Status}}' gluetun 2>/dev/null; }
for _ in $(seq 60); do [ "$(health)" = healthy ] && break; sleep 5; done   # up to 5 min
if [ "$(health)" != healthy ]; then
  echo "gluetun never became healthy; stopping qbittorrent"
  docker stop qbittorrent; exit 1
fi

# A container is cut off when it isn't running, was joined to an older gluetun
# container (gluetun was recreated), or started before the current gluetun did.
since() { date -d "$1" +%s%N; }
read -r gluetun_id gluetun_started < <(docker inspect -f '{{.Id}} {{.State.StartedAt}}' gluetun)
stale=""
for c in $DEPENDENTS; do
  running="" started="" mode=""
  read -r running started mode < <(docker inspect \
    -f '{{.State.Running}} {{.State.StartedAt}} {{.HostConfig.NetworkMode}}' "$c" 2>/dev/null)
  if [ "$running" != true ] || [ "$mode" != "container:$gluetun_id" ] \
     || [ "$(since "$started")" -lt "$(since "$gluetun_started")" ]; then
    stale+=" $c"
  fi
done

if [ -n "$stale" ]; then
  echo "reattaching to gluetun's network:$stale"
  # --no-deps: never recreate gluetun from here, which would cut off the others.
  docker compose up -d --no-deps --force-recreate $stale
  sleep 30
else
  echo "all containers are on gluetun's network; nothing to restart"
fi

host_ip=$(curl -s -m 10 https://api.ipify.org)
qb_ip=$(docker exec qbittorrent curl -s -m 10 https://api.ipify.org)
echo "host IP: $host_ip   qbittorrent IP: $qb_ip"
if [ -z "$qb_ip" ] || [ "$qb_ip" = "$host_ip" ]; then
  echo "qbittorrent is NOT behind the VPN; stopping it"
  docker stop qbittorrent; exit 1
fi
echo "OK: qbittorrent is behind the VPN"
