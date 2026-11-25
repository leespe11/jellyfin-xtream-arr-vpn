#!/usr/bin/env bash
# Creates or completes .env, the one file with every login and key this machine uses.
#   - Asks for what only you know: VPN, folders, the admin login. Each question comes up
#     only while its answer is missing.
#   - Generates the keys and passwords the apps use with each other (Sonarr, Radarr,
#     Prowlarr, Bazarr and Seerr API keys, Dispatcharr's Xtream login). On a machine that
#     already runs the stack it takes them from the apps' own settings instead.
#   - Writes .env in the order and with the explanations of .env.example, keeping every
#     value already there, so it's safe to run again after editing .env by hand.
# Then run scripts/bootstrap.sh to set up and link the apps.
# Usage (as root, from the stack folder): scripts/generate-env.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { : > .env; echo "creating .env"; }
chmod 600 .env
source scripts/lib.sh

# Writes NAME='value' into .env, replacing the line if NAME is there already.
set_val() {
  local line="$1='$2'"
  if grep -q "^$1=" .env; then
    K=$1 L=$line awk 'index($0, ENVIRON["K"] "=") == 1 { print ENVIRON["L"]; next } { print }' .env > "$WORK/env"
    cat "$WORK/env" > .env          # cat keeps .env's owner and mode
  else
    printf '%s\n' "$line" >> .env
  fi
}
has() { grep -q "^$1=" .env; }       # answered already (maybe with an empty value)

# Tidies what was typed and checks it: prints the value to keep, or explains and fails.
# A whole config line pasted ("PrivateKey = abc...=") is cut down to its value.
check() {  # kind value
  local v=$2
  v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
  [[ $v =~ ^[A-Za-z]+[[:space:]]*=[[:space:]]*(.+)$ ]] && v=${BASH_REMATCH[1]}
  case $1 in
    wgkey)   [[ $v =~ ^[A-Za-z0-9+/]{43}=$ ]] \
               || { echo "  that's not a WireGuard key (44 characters ending in =)" >&2; return 1; } ;;
    address) v=${v// /}
             [[ $v =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}(,.*)?$ ]] \
               || { echo "  that's not an address like 10.x.x.x/32" >&2; return 1; } ;;
    totp)    v=${v// /}; v=${v^^}
             [[ $v =~ ^[A-Z2-7]{16,}=*$ ]] \
               || { echo "  that's not a 2FA secret key (16+ letters A-Z and digits 2-7, not the 6-digit code)" >&2; return 1; } ;;
    port)    [[ $v =~ ^[0-9]{1,5}$ ]] && [ "$v" -le 65535 ] \
               || { echo "  that's not a port number" >&2; return 1; } ;;
    yesno)   case ${v,,} in y|yes) v=yes ;; n|no) v=no ;; *) echo "  yes or no" >&2; return 1 ;; esac ;;
    role)    case ${v,,} in p|primary) v=primary ;; s|secondary) v=secondary ;;
               *) echo "  primary or secondary" >&2; return 1 ;; esac ;;
    dir)     v=${v%/}; [[ $v == /?* ]] || { echo "  a full path starting with / (not / itself)" >&2; return 1; } ;;
    subdir)  v=${v%/}; v=${v#./}
             [[ -n $v && $v != /* && /$v/ != */../* ]] \
               || { echo "  just the name of a folder inside the media folder, e.g. Movies" >&2; return 1; } ;;
  esac
  case $v in *"'"*) echo "  can't contain a single quote (')" >&2; return 1 ;; esac
  printf '%s' "$v"
}

read_value() {  # prompt secret -> the line typed, in $REPLY
  if [ -n "$2" ]; then read -rsp "$1: "; echo; else read -rp "$1: "; fi
}

# Asks while NAME is empty.
ask() {  # name prompt [secret] [kind]
  local v
  [ -z "$(env_val "$1")" ] || return 0
  [ -t 0 ] || { echo "$1 is empty in .env and there's no terminal to ask on"; exit 1; }
  while true; do
    read_value "$2" "${3:-}"
    [ -n "$REPLY" ] || { echo "  needed, try again"; continue; }
    v=$(check "${4:-}" "$REPLY") && break
  done
  set_val "$1" "$v"
  [ -z "${3:-}" ] || echo "  saved (${#v} characters)"   # hidden input: show that it arrived
}

# For answers that may be empty: asked while NAME has no line in .env (Enter keeps the
# default shown, which may be empty). To be asked again, delete the line.
ask_once() {  # name prompt [default] [secret] [kind]
  local v
  ! has "$1" || return 0
  if [ ! -t 0 ]; then set_val "$1" "${3:-}"; return 0; fi
  while true; do
    read_value "$2${3:+ [$3]}" "${4:-}"
    [ -n "$REPLY" ] || { v=${3:-}; break; }
    v=$(check "${5:-}" "$REPLY") && break
  done
  set_val "$1" "$v"
  if [ -n "${4:-}" ]; then
    [ -n "$v" ] && echo "  saved (${#v} characters)" || echo "  left empty"
  fi
}

once() { has "$1" || set_val "$1" "$2"; }                         # a value, unless set
default() { [ -n "$(env_val "$1")" ] || set_val "$1" "$2"; }       # a value while empty
explain() { has "$1" || { echo; cat; }; }                         # text before a question

# What a running machine already uses (empty if that app hasn't been set up here).
xml_tag() { [ -f "$2" ] && grep -oP "(?<=<$1>)[^<]+" "$2" | head -1 || true; }
existing_key() {
  case $1 in
    SONARR_API_KEY)   xml_tag ApiKey config/sonarr/config.xml ;;
    RADARR_API_KEY)   xml_tag ApiKey config/radarr/config.xml ;;
    PROWLARR_API_KEY) xml_tag ApiKey config/prowlarr/config.xml ;;
    BAZARR_API_KEY)
      [ -f config/bazarr/config/config.yaml ] && awk '/^auth:/{a=1; next} /^[^ ]/{a=0}
        a && /apikey:/{gsub(/'\''/, "", $2); print $2; exit}' config/bazarr/config/config.yaml || true ;;
    SEERR_API_KEY) [ -f config/seerr/settings.json ] && jq -r '.main.apiKey // empty' config/seerr/settings.json || true ;;
    DISPATCHARR_XC_USER)     xml_tag Username config/jellyfin/plugins/configurations/Jellyfin.Xtream.Library.xml ;;
    DISPATCHARR_XC_PASSWORD) xml_tag Password config/jellyfin/plugins/configurations/Jellyfin.Xtream.Library.xml ;;
  esac
}

# ---- This machine ----------------------------------------------------------------------
host_tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || true)
ask_once TZ "Timezone" "${host_tz:-America/Toronto}"
default SERVER_NAME jellyfin
# The media belongs to your own login, not root: the user who ran sudo, else whoever
# owns the stack folder, else the first regular user (UID 1000 and up), else 1000.
owner=
for u in "${SUDO_USER:-}" "$(stat -c %U . 2>/dev/null)" "$(logname 2>/dev/null)" \
         "$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 { print $1; exit }')"; do
  [ -n "$u" ] && [ "$u" != root ] && id -u "$u" >/dev/null 2>&1 && { owner=$u; break; }
done
once PUID "$( [ -n "$owner" ] && id -u "$owner" || echo 1000)"
once PGID "$( [ -n "$owner" ] && id -g "$owner" || echo 1000)"
[ "$(env_val PUID)" != 0 ] || echo "  note: PUID in .env is 0, so the media files will belong to root"

# ---- VPN ---------------------------------------------------------------------------------
ask_once VPN_SERVICE_PROVIDER "VPN provider, as gluetun names it (windscribe, protonvpn, mullvad, airvpn...)" windscribe
provider=$(env_val VPN_SERVICE_PROVIDER)
explain WIREGUARD_PRIVATE_KEY <<'EOF'
The next answers come from a WireGuard config made for THIS machine (on Windscribe:
the WireGuard config generator). Every machine needs its own: two machines on one
config knock each other offline. Pasting the whole line ("PrivateKey = ...") is fine.
EOF
# What each provider needs, per gluetun's wiki (setup/providers):
#   windscribe, airvpn  private key, address, preshared key; servers by region; the
#                       forwarded port comes from your account
#   protonvpn           private key only; servers by country or region
#   mullvad             private key and address; servers by country; no port forwarding
# Any other provider: everything is asked; what isn't needed can be left empty.
ask WIREGUARD_PRIVATE_KEY "WireGuard private key (PrivateKey)" secret wgkey
case $provider in
  protonvpn) once WIREGUARD_ADDRESSES "" ;;                  # gluetun knows it
  *)         ask WIREGUARD_ADDRESSES "WireGuard address (Address), e.g. 10.x.x.x/32" "" address ;;
esac
case $provider in
  # Their servers pass no traffic without it, and gluetun can't tell you why.
  windscribe|airvpn) ask WIREGUARD_PRESHARED_KEY "WireGuard preshared key (PresharedKey)" secret wgkey ;;
  protonvpn|mullvad) once WIREGUARD_PRESHARED_KEY "" ;;
  *) ask_once WIREGUARD_PRESHARED_KEY "WireGuard preshared key (PresharedKey; Enter if your config has none)" "" secret wgkey ;;
esac
case $provider in
  mullvad)
    once VPN_SERVER_REGIONS ""
    ask_once VPN_SERVER_COUNTRIES "VPN server countries, comma-separated, e.g. Canada (Enter for any)" ;;
  protonvpn)
    ask_once VPN_SERVER_COUNTRIES "VPN server countries, comma-separated, e.g. Canada (Enter for any)"
    ask_once VPN_SERVER_REGIONS "VPN server regions, comma-separated (Enter for any)" ;;
  windscribe)
    once VPN_SERVER_COUNTRIES ""
    ask_once VPN_SERVER_REGIONS "VPN server regions, comma-separated, e.g. Canada East (Enter for any server)" ;;
  *)
    once VPN_SERVER_COUNTRIES ""
    ask_once VPN_SERVER_REGIONS "VPN server regions, comma-separated, as your provider names them (Enter for any server)" ;;
esac

# ---- Port forwarding -------------------------------------------------------------------
if [ "$provider" = windscribe ]; then
  explain PORT_REFRESH <<'EOF'
Windscribe forwards one port to qBittorrent. Without a static IP it's an "ephemeral"
port that expires after 7 days; the nightly job can renew it, which needs your
Windscribe login. With a static IP the port is permanent and nothing needs renewing.
EOF
  ask_once PORT_REFRESH "Renew the forwarded port every week? (yes/no)" yes "" yesno
else
  once PORT_REFRESH no                     # only Windscribe's ports get renewed
fi
if [ "$(env_val PORT_REFRESH)" = yes ]; then
  explain ROLE <<'EOF'
Machines on one Windscribe account share the forwarded port. One of them renews it
(primary); the others follow it (secondary). With one machine, it's the primary.
EOF
  ask_once ROLE "This machine's role (primary/secondary)" primary "" role
  ask WS_USERNAME "Windscribe username"
  ask WS_PASSWORD "Windscribe password" secret
  # The 2FA secret (the key shown when 2FA was set up, not a 6-digit code), so the
  # renewal can sign in by itself.
  ask_once WS_TOTP "Windscribe 2FA secret key (only if your account has 2FA turned on, not the 6-digit code; otherwise press Enter)" "" secret totp
else
  once ROLE primary
fi
case $provider in
  mullvad)    # no port forwarding: torrents still work, they just reach fewer peers
    once TORRENT_PORT "" ;;
  protonvpn)  # ProtonVPN hands out a random port that changes; this stack doesn't follow it yet
    ask_once TORRENT_PORT "Forwarded port for qBittorrent (Enter for none; ProtonVPN's changes, so usually none)" "" "" port ;;
  *)
    ask TORRENT_PORT "Port your VPN forwards to qBittorrent (your VPN account's port forwarding page)" "" port ;;
esac

# ---- Folders -------------------------------------------------------------------------------
explain DATA_ROOT <<'EOF'
Your media goes in one folder, ideally on your big drive (mount it first). Movies, series
and downloads are subfolders of it, so finished downloads are hard-linked into place
instead of copied. Missing folders are created.
EOF
ask_once DATA_ROOT "Media folder" /mnt/storage "" dir
root=$(env_val DATA_ROOT)
# Asked as a name inside DATA_ROOT, saved as the full path.
subfolder() {  # name prompt default
  has "$1" && return 0
  ask_once "$1" "$2, a folder inside $root" "$3" "" subdir
  set_val "$1" "$root/$(env_val "$1")"
}
subfolder MOVIES_DIR "Movies" Movies
subfolder SERIES_DIR "Series" Series
subfolder TORRENTS_DIR "Downloads (torrents)" torrents
for d in "$(env_val MOVIES_DIR)" "$(env_val SERIES_DIR)" "$(env_val TORRENTS_DIR)/incomplete"; do
  [ -d "$d" ] || { mkdir -p "$d" && echo "  created $d"; }
done
explain JELLYFIN_BULK_DIR <<'EOF'
Jellyfin's artwork and the IPTV provider's movie/series (STRM) files grow to tens of GB.
By default they stay in ./config/jellyfin with the rest of Jellyfin. If this machine's
system drive is small, give a folder on the big drive instead (e.g. /mnt/storage/jellyfin).
EOF
if [ -n "$(ls -A config/jellyfin/metadata config/jellyfin/xtream-library 2>/dev/null)" ]; then
  has JELLYFIN_BULK_DIR || echo "(This machine has them in ./config/jellyfin already. To move them, stop the stack and move metadata/ and xtream-library/ to the new folder first.)"
fi
ask_once JELLYFIN_BULK_DIR "Folder for them (Enter keeps them in ./config/jellyfin)" "" "" dir

# ---- Logins --------------------------------------------------------------------------------
explain ADMIN_USER <<'EOF'
One admin login for the web pages: Sonarr, Radarr, Prowlarr, Bazarr, Dispatcharr, and
to start with Jellyfin and qBittorrent (each can be changed later in .env).
EOF
ask ADMIN_USER "Admin username"
ask ADMIN_PASSWORD "Admin password" secret
default JELLYFIN_ADMIN_USER "$(env_val ADMIN_USER)"
default JELLYFIN_ADMIN_PASSWORD "$(env_val ADMIN_PASSWORD)"
default DISPATCHARR_ADMIN_USER "$(env_val ADMIN_USER)"
default DISPATCHARR_ADMIN_PASSWORD "$(env_val ADMIN_PASSWORD)"
default QBIT_USERNAME "$(env_val ADMIN_USER)"
default QBIT_PASSWORD "$(env_val ADMIN_PASSWORD)"

# ---- Keys the apps use with each other -----------------------------------------------------
for name in SONARR_API_KEY RADARR_API_KEY PROWLARR_API_KEY BAZARR_API_KEY SEERR_API_KEY \
            DISPATCHARR_XC_USER DISPATCHARR_XC_PASSWORD; do
  [ -z "$(env_val "$name")" ] || continue
  value=$(existing_key "$name")
  if [ -n "$value" ]; then
    set_val "$name" "$value"; echo "  $name taken from this machine's settings"
    continue
  fi
  case $name in
    SEERR_API_KEY)           value=$(random_string 48) ;;
    DISPATCHARR_XC_USER)     value=streamer ;;
    DISPATCHARR_XC_PASSWORD) value=$(random_string 24) ;;
    *)                       value=$(random_hex 32) ;;
  esac
  set_val "$name" "$value"
done

# ---- Write .env in .env.example's order ---------------------------------------------------
# Every line of .env.example, with this machine's value where .env has one; then anything
# else .env holds (older or hand-added settings) at the end.
awk '
  FILENAME == ARGV[1] { if (match($0, /^[A-Za-z_][A-Za-z0-9_]*=/)) { k = substr($0, 1, RLENGTH - 1)
                if (!(k in have)) { have[k] = $0; order[++n] = k } } next }
  { if (match($0, /^[A-Za-z_][A-Za-z0-9_]*=/)) { k = substr($0, 1, RLENGTH - 1); used[k] = 1
      print ((k in have) ? have[k] : $0) } else print }
  END { extra = 0
        for (i = 1; i <= n; i++) if (!(order[i] in used)) {
          if (!extra++) print "\n# ---- Other settings (not in .env.example) ------------------------------------"
          print have[order[i]] } }
' .env .env.example > "$WORK/env.new"
cat "$WORK/env.new" > .env

echo
echo "Done: .env is complete (nano .env to look it over or change a setting)."
echo "Next: scripts/bootstrap.sh"
