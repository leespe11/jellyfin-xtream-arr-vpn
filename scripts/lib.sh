# Shared helpers for the stack's scripts. Source from the repo root.

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
QBIT_URL=http://localhost:8080

# Reads a value from .env literally (no shell expansion), dropping surrounding quotes.
env_val() {
  local v; v=$(grep -m1 "^$1=" .env | cut -d= -f2- || true)
  v=${v#[\'\"]}; v=${v%[\'\"]}
  printf '%s' "$v"
}

qbit_login() {
  curl -fsS -c "$WORK/qbit-cookie" --data-urlencode "username=$(env_val QBIT_USERNAME)" \
    --data-urlencode "password=$(env_val QBIT_PASSWORD)" "$QBIT_URL/api/v2/auth/login" >/dev/null
}

qbit_port() {
  curl -fsS -b "$WORK/qbit-cookie" "$QBIT_URL/api/v2/app/preferences" | grep -oP '"listen_port":\K[0-9]+'
}

qbit_set_port() {
  curl -fsS -b "$WORK/qbit-cookie" --data-urlencode "json={\"listen_port\":$1}" \
    "$QBIT_URL/api/v2/app/setPreferences" >/dev/null
}

# Saves the port to .env and recreates everything sharing gluetun's network,
# so gluetun opens the new port and Jellyfin/qBittorrent reattach to it.
apply_port() {
  sed -i "s/^TORRENT_PORT=.*/TORRENT_PORT=$1/" .env
  docker compose up -d --force-recreate gluetun qbittorrent jellyfin
  echo "$(date) now using port $1"
}

# ---- Used by generate-env.sh and bootstrap.sh ---------------------------------------

# Where Jellyfin's artwork (metadata/) and the provider's STRM files (xtream-library/)
# are: JELLYFIN_BULK_DIR from .env, or inside config/jellyfin.
jellyfin_bulk_dir() { local d; d=$(env_val JELLYFIN_BULK_DIR); printf '%s' "${d:-config/jellyfin}"; }

# Reads MOVIES_DIR, SERIES_DIR, TORRENTS_DIR and DATA_ROOT (their folder, mounted as
# /data so downloads can be hard-linked into the media folders) from .env, and sets
# MOVIES_PATH, SERIES_PATH, TORRENTS_PATH (the same folders inside the containers).
media_dirs() {
  MOVIES_DIR=$(env_val MOVIES_DIR); SERIES_DIR=$(env_val SERIES_DIR)
  TORRENTS_DIR=$(env_val TORRENTS_DIR); DATA_ROOT=$(env_val DATA_ROOT)
  MOVIES_PATH=/data${MOVIES_DIR#"$DATA_ROOT"}
  SERIES_PATH=/data${SERIES_DIR#"$DATA_ROOT"}
  TORRENTS_PATH=/data${TORRENTS_DIR#"$DATA_ROOT"}
}

# Letters and digits only, so the result is safe in URLs, XML, YAML and in Dispatcharr's
# XC password rules.
random_string() {  # [length]
  local s=""
  while [ ${#s} -lt "${1:-32}" ]; do s+=$(head -c 64 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9'); done
  printf '%s' "${s:0:${1:-32}}"
}
random_hex() {  # [length]
  local s=""
  while [ ${#s} -lt "${1:-32}" ]; do s+=$(head -c 64 /dev/urandom | LC_ALL=C tr -dc 'a-f0-9'); done
  printf '%s' "${s:0:${1:-32}}"
}

# Prints a template with each {{NAME}} filled in: from the shell variable NAME when it's
# set (values bootstrap.sh works out as it goes), otherwise from NAME in .env. Values are
# escaped for the file type (xml, json, or yaml inside single quotes). An empty value is
# an error, except for {{NAME:-}}, which may stay empty.
render() {  # template [xml|json|yaml|raw]
  local text name val kind=${2:-raw} optional
  shopt -u patsub_replacement 2>/dev/null || true   # bash 5.2: & in a replacement means "the match"
  text=$(<"$1")
  for name in $(grep -oE '\{\{[A-Z0-9_]+(:-)?\}\}' "$1" | sort -u | tr -d '{}'); do
    optional=false; [ "${name%:-}" = "$name" ] || { optional=true; name=${name%:-}; }
    val=${!name-}; [ -n "$val" ] || val=$(env_val "$name")
    if [ -z "$val" ] && ! $optional; then echo "render: $name is empty (needed by $1)" >&2; return 1; fi
    case $kind in
      xml)  val=${val//&/&amp;}; val=${val//</&lt;}; val=${val//>/&gt;} ;;
      json) val=${val//'\'/'\\'}; val=${val//'"'/'\"'} ;;
      yaml) val=${val//"'"/"''"} ;;
    esac
    $optional && text=${text//"{{$name:-}}"/$val} || text=${text//"{{$name}}"/$val}
  done
  printf '%s\n' "$text"
}

# Waits until a URL answers with any status below 500, for up to [seconds] (default 300).
wait_for() {  # url [seconds]
  local i
  for ((i = 0; i < ${2:-300}; i += 3)); do
    curl -s -o /dev/null -m 5 -w '%{http_code}' "$1" 2>/dev/null | grep -qE '^[1-4]' && return 0
    sleep 3
  done
  echo "gave up waiting for $1" >&2
  return 1
}
