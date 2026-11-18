#!/usr/bin/env bash
# Keeps the Windscribe forwarded port in step across machines sharing one account.
#   ROLE=primary    renews the port every ~6 days and applies it here
#   ROLE=secondary  reads the account's current port from Windscribe (no renewal) and applies it
# Only runs with VPN_SERVICE_PROVIDER=windscribe and PORT_REFRESH=yes in .env; otherwise
# TORRENT_PORT stays as set.
# Usage: port-sync.sh [--force]    (--force makes the primary renew now)
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/lib.sh

provider=$(env_val VPN_SERVICE_PROVIDER)
refresh=$(env_val PORT_REFRESH)
if [ "$provider" != windscribe ] || [ "$refresh" != yes ]; then
  echo "$(date) port refresh is off (VPN_SERVICE_PROVIDER=$provider, PORT_REFRESH=$refresh); port stays $(env_val TORRENT_PORT)"
  exit 0
fi

WS_IMAGE=dhruvinsh/ws-ephemeral:latest
mkdir -p ./config/ws-ephemeral

# docker --env-file reads values literally, so write the unquoted values out.
{
  echo "WS_USERNAME=$(env_val WS_USERNAME)"
  echo "WS_PASSWORD=$(env_val WS_PASSWORD)"
  [ -z "$(env_val WS_TOTP)" ] || echo "WS_TOTP=$(env_val WS_TOTP)"
  echo "QBIT_USERNAME=$(env_val QBIT_USERNAME)"
  echo "QBIT_PASSWORD=$(env_val QBIT_PASSWORD)"
} > "$WORK/ws.env"

case "$(env_val ROLE)" in
  primary)
    STAMP=./config/ws-ephemeral-last-run

    # Cron calls this daily; only rotate once ~6 days have passed (ports last 7).
    if [ "${1:-}" != "--force" ] && [ -f "$STAMP" ] \
       && [ -z "$(find "$STAMP" -mmin +$((6*24*60 - 60)))" ]; then
      exit 0
    fi
    echo "$(date) renewing Windscribe port (current: $(env_val TORRENT_PORT))"

    docker run --rm --network host \
      --env-file "$WORK/ws.env" \
      -e QBIT_HOST=http://localhost -e QBIT_PORT=8080 \
      -e QBIT_PRIVATE_TRACKER=true \
      -e WS_COOKIE_PATH=/cookie -e TZ="$(env_val TZ)" \
      -v "$PWD/config/ws-ephemeral:/cookie" \
      "$WS_IMAGE" ws-ephemeral run

    # ws-ephemeral pushed the new port into qBittorrent; read it back.
    qbit_login
    PORT=$(qbit_port)
    touch "$STAMP"
    ;;

  secondary)
    # Reads the port with ws-ephemeral's own Windscribe client, without renewing it.
    PORT=$(docker run --rm --network host \
      --env-file "$WORK/ws.env" \
      -v "$PWD/config/ws-ephemeral:/cookie" \
      --entrypoint python "$WS_IMAGE" -c '
import os
from pathlib import Path
from ws_ephemeral.windscribe import Windscribe
with Windscribe(os.environ["WS_USERNAME"], os.environ["WS_PASSWORD"],
                os.environ.get("WS_TOTP") or None,
                cookie_file=Path("/cookie/cookie.pkl")) as ws:
    print(ws.get_ephm_port() or "")
')
    qbit_login
    ;;

  *)
    echo "ROLE in .env must be primary or secondary"; exit 1
    ;;
esac

[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "could not read the Windscribe port"; exit 1; }
[ "$(qbit_port)" = "$PORT" ] || qbit_set_port "$PORT"
if [ "$PORT" = "$(env_val TORRENT_PORT)" ]; then
  echo "$(date) port unchanged ($PORT)"
else
  apply_port "$PORT"
fi
