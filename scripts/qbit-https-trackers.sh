#!/bin/sh
# Run by qBittorrent when a torrent is added (inside the qbittorrent container, which
# has curl but no bash or jq). Switches the torrent's http:// tracker addresses to
# https://: through some VPN servers the tracker cuts off plain-HTTP announces
# ("End of file"), while HTTPS ones work. Hosts without working HTTPS stay on http.
# qBittorrent setting (Options -> Downloads -> Run on torrent added):
#   sh /scripts/qbit-https-trackers.sh "%K"
# Logs to /config/qbit-https-trackers.log (config/qbittorrent/ on the host).
ID=$1
API=http://localhost:8080/api/v2
LOG=/config/qbit-https-trackers.log
[ -n "$ID" ] || exit 0
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
log() { echo "$(date '+%F %T') ${ID%"${ID#????????}"} $*" >> "$LOG"; }

curl -fsS -c "$JAR" --data-urlencode "username=$QBIT_USERNAME" \
  --data-urlencode "password=$QBIT_PASSWORD" "$API/auth/login" >/dev/null \
  || { log "login failed (QBIT_USERNAME/QBIT_PASSWORD in .env?)"; exit 1; }

n=0
for url in $(curl -fsS -b "$JAR" "$API/torrents/trackers?hash=$ID" \
             | grep -o '"url":"http://[^"]*"' | cut -d'"' -f4); do
  new=$(echo "https://${url#http://}" | sed 's#^\(https://[^/]*\):80/#\1/#')   # drop an explicit :80
  # Keep http for a host with no working HTTPS (e.g. no certificate for its name, so the
  # TLS handshake fails). Any HTTP answer, even an error page, means HTTPS works there.
  host=$(echo "$new" | cut -d/ -f3)
  if ! curl -sS -o /dev/null -m 15 "https://$host/" 2>/dev/null; then
    log "kept http for $host (HTTPS handshake failed)"; continue
  fi
  curl -fsS -b "$JAR" --data-urlencode "hash=$ID" --data-urlencode "urls=$new" \
    "$API/torrents/addTrackers" \
  && curl -fsS -b "$JAR" --data-urlencode "hash=$ID" --data-urlencode "urls=$url" \
    "$API/torrents/removeTrackers" \
  && n=$((n + 1))
done
[ "$n" = 0 ] && exit 0
curl -fsS -b "$JAR" -d "hashes=$ID" "$API/torrents/reannounce"
log "switched $n tracker(s) to https"
