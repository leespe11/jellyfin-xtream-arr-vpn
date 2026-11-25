#!/usr/bin/env bash
# Sets up this machine's apps from .env (scripts/generate-env.sh) and templates/, and
# links them together:
#   Dispatcharr    admin login, and the user the Xtream Library plugin signs in with
#   Jellyfin       admin, settings, Live TV, plugins and their settings, libraries
#   qBittorrent    settings, categories, web login
#   Sonarr/Radarr  login, root folder, qBittorrent, Jellyfin updates, x265 custom format,
#                  size limits, quality profiles, naming and media management
#   Prowlarr       login, Sonarr and Radarr
#   Bazarr         settings, Sonarr/Radarr/Jellyfin links, English profile
#   Seerr          admin (your Jellyfin admin), libraries, Radarr/Sonarr, 6 am full scan
#   cron           the nightly job, artwork shrinking, after-boot and the two watchdogs
# Safe to run again: settings files are only created when missing (an app's own file is
# never overwritten), API steps skip what's already there, and existing logins are left
# alone. Your accounts stay manual: the Prowlarr indexer, Dispatcharr's IPTV provider.
# Usage (as root, from the stack folder): scripts/bootstrap.sh
set -uo pipefail
cd "$(dirname "$0")/.."
source scripts/lib.sh
T=templates
PROBLEMS=()
step()    { echo; echo "--- $* ---"; }
problem() { echo "PROBLEM: $*"; PROBLEMS+=("$*"); }
say()     { echo "  $*"; }

# ---------------------------------------------------------------------------------
step "Checks"
for cmd in docker jq curl python3 md5sum; do
  command -v "$cmd" >/dev/null || { echo "$cmd is not installed (see README, Requirements)"; exit 1; }
done
missing=()
for v in PUID PGID DATA_ROOT MOVIES_DIR SERIES_DIR TORRENTS_DIR ADMIN_USER ADMIN_PASSWORD \
         JELLYFIN_ADMIN_USER JELLYFIN_ADMIN_PASSWORD DISPATCHARR_ADMIN_USER DISPATCHARR_ADMIN_PASSWORD \
         QBIT_USERNAME QBIT_PASSWORD SONARR_API_KEY RADARR_API_KEY PROWLARR_API_KEY BAZARR_API_KEY \
         SEERR_API_KEY DISPATCHARR_XC_USER DISPATCHARR_XC_PASSWORD QUALITY_PROFILE QUALITIES X265_SCORE; do
  [ -n "$(env_val "$v")" ] || missing+=("$v")
done
[ ${#missing[@]} -eq 0 ] || { echo "empty in .env: ${missing[*]} (fill them in; generate-env.sh fills in logins, keys and folders)"; exit 1; }

ADMIN_USER=$(env_val ADMIN_USER); ADMIN_PASSWORD=$(env_val ADMIN_PASSWORD)
D_USER=$(env_val DISPATCHARR_ADMIN_USER); D_PASS=$(env_val DISPATCHARR_ADMIN_PASSWORD)
PUID=$(env_val PUID); PGID=$(env_val PGID)
media_dirs
for d in "$MOVIES_DIR" "$SERIES_DIR" "$TORRENTS_DIR"; do
  case $d in "$DATA_ROOT"/?*) ;; *)
    echo "$d isn't inside DATA_ROOT ($DATA_ROOT): the movies, series and downloads folders"
    echo "must be subfolders of it. Fix them in .env, or delete those lines and run scripts/generate-env.sh."
    exit 1 ;;
  esac
done
JELLYFIN_BULK=$(jellyfin_bulk_dir)
SERVER_NAME=$(env_val SERVER_NAME); SERVER_NAME=${SERVER_NAME:-jellyfin}
# Quality profiles, each "name|qualities|x265 score"; the first is Seerr's default, and an
# empty QUALITY_PROFILE_4K means no 4K profile.
QUALITY_PROFILE=$(env_val QUALITY_PROFILE)
QUALITY_PROFILE_4K=$(env_val QUALITY_PROFILE_4K)
PROFILES=("$QUALITY_PROFILE|$(env_val QUALITIES)|$(env_val X265_SCORE)")
[ -z "$QUALITY_PROFILE_4K" ] || [ "$QUALITY_PROFILE_4K" = "$QUALITY_PROFILE" ] \
  || PROFILES+=("$QUALITY_PROFILE_4K|$(env_val QUALITIES_4K)|$(env_val X265_SCORE_4K)")
for p in "${PROFILES[@]}"; do
  [[ ${p##*|} =~ ^-?[0-9]+$ ]] || { echo "X265_SCORE and X265_SCORE_4K in .env must be whole numbers"; exit 1; }
done
say "ok"

# ---------------------------------------------------------------------------------
step "Folders"
# The media and download folders (and qBittorrent's incomplete folder). Only these
# folders are chowned, never what's already in them.
mkdir -p "$MOVIES_DIR" "$SERIES_DIR" "$TORRENTS_DIR/incomplete"
chown "$PUID:$PGID" "$MOVIES_DIR" "$SERIES_DIR" "$TORRENTS_DIR" "$TORRENTS_DIR/incomplete"
say "movies $MOVIES_DIR, series $SERIES_DIR, downloads $TORRENTS_DIR (as $MOVIES_PATH, $SERIES_PATH, $TORRENTS_PATH in the apps)"
mkdir -p config/{sonarr,radarr,prowlarr,bazarr/config,qbittorrent/qBittorrent,seerr,dispatcharr} \
         config/jellyfin-cache config/jellyfin/{config,plugins/configurations} \
         "$JELLYFIN_BULK"/{metadata,xtream-library/Movies,xtream-library/Series}
say "ok"

# ---------------------------------------------------------------------------------
step "Settings files (created only when missing)"
KEPT=0
seed() {  # template destination format [owner]
  if [ -e "$2" ]; then KEPT=$((KEPT + 1)); return 0; fi
  if render "$1" "$3" > "$2.new"; then
    mv "$2.new" "$2"; say "created $2"
    [ -z "${4:-}" ] || chown "$4" "$2"
  else
    rm -f "$2.new"; problem "could not create $2"
  fi
}
seed $T/sonarr/config.xml   config/sonarr/config.xml   xml "$PUID:$PGID"
seed $T/radarr/config.xml   config/radarr/config.xml   xml "$PUID:$PGID"
seed $T/prowlarr/config.xml config/prowlarr/config.xml xml "$PUID:$PGID"

QCONF=config/qbittorrent/qBittorrent/qBittorrent.conf
if [ ! -e "$QCONF" ]; then
  # qBittorrent keeps only a salted PBKDF2-SHA512 hash of its web password.
  QBIT_PASSWORD_PBKDF2=$(python3 - "$(env_val QBIT_PASSWORD)" <<'EOF'
import base64, hashlib, os, sys
salt = os.urandom(16)
key = hashlib.pbkdf2_hmac('sha512', sys.argv[1].encode(), salt, 100000)
print('@ByteArray(%s:%s)' % (base64.b64encode(salt).decode(), base64.b64encode(key).decode()))
EOF
)
fi
seed $T/qbittorrent/qBittorrent.conf "$QCONF" raw "$PUID:$PGID"
seed $T/qbittorrent/categories.json config/qbittorrent/qBittorrent/categories.json raw "$PUID:$PGID"

seed $T/seerr/settings.json config/seerr/settings.json json
chown -R 1000:1000 config/seerr        # the Seerr image runs as its "node" user (uid 1000)

for f in $T/jellyfin/config/*.xml; do
  seed "$f" "config/jellyfin/config/${f##*/}" xml
done
for f in $T/jellyfin/plugins/*.xml; do
  seed "$f" "config/jellyfin/plugins/configurations/${f##*/}" xml
done
[ $KEPT -eq 0 ] || say "$KEPT already there, kept as they are"
# Bazarr's file needs Jellyfin's library ids and key, so it's created further down.

# ---------------------------------------------------------------------------------
step "Starting the stack"
services=$(docker compose config --services)
[ -e config/bazarr/config/config.yaml ] || services=$(grep -vx bazarr <<<"$services")
# shellcheck disable=SC2086
docker compose up -d $services || problem "docker compose up failed"

# ---------------------------------------------------------------------------------
step "Dispatcharr"
DAPI=http://localhost:9191/api/accounts
say "waiting for Dispatcharr (its first start takes a few minutes)"
if wait_for "$DAPI/initialize-superuser/" 600; then
  if [ "$(curl -fsS "$DAPI/initialize-superuser/" | jq -r .superuser_exists)" != true ]; then
    jq -n --arg u "$D_USER" --arg p "$D_PASS" '{username: $u, password: $p}' \
      | curl -fsS -H 'Content-Type: application/json' -d @- "$DAPI/initialize-superuser/" >/dev/null \
      && say "created admin $D_USER" || problem "could not create Dispatcharr's admin"
  fi
  DTOKEN=$(jq -n --arg u "$D_USER" --arg p "$D_PASS" '{username: $u, password: $p}' \
    | curl -fsS -H 'Content-Type: application/json' -d @- "$DAPI/token/" | jq -r '.access // empty')
  if [ -z "$DTOKEN" ]; then
    problem "Dispatcharr login as $D_USER failed: set DISPATCHARR_ADMIN_USER and DISPATCHARR_ADMIN_PASSWORD in .env to its admin login"
  else
    XC_USER=$(env_val DISPATCHARR_XC_USER); XC_PASS=$(env_val DISPATCHARR_XC_PASSWORD)
    if curl -fsS -H "Authorization: Bearer $DTOKEN" "$DAPI/users/" \
        | jq -e --arg u "$XC_USER" '(.results? // .) | any(.[]; .username == $u)' >/dev/null; then
      say "user $XC_USER already there"
    else
      # Streamer level: it can only play channels and VOD, through the Xtream API.
      jq -n --arg u "$XC_USER" --arg p "$XC_PASS" '{username: $u, password: $p, user_level: 0,
            custom_properties: {xc_password: $p, vod_movies_enabled: true, vod_series_enabled: true}}' \
        | curl -fsS -H "Authorization: Bearer $DTOKEN" -H 'Content-Type: application/json' -d @- "$DAPI/users/" >/dev/null \
        && say "created user $XC_USER for the Xtream Library plugin" || problem "could not create Dispatcharr user $XC_USER"
    fi
  fi
else
  problem "Dispatcharr didn't start (docker logs dispatcharr)"
fi

# ---------------------------------------------------------------------------------
step "Jellyfin"
JF=http://localhost:8096
JAUTH='MediaBrowser Client="media-stack", Device="bootstrap", DeviceId="media-stack-bootstrap", Version="1.0"'
JTOKEN=""
jf() {  # method path [body]: Jellyfin API call, signed in once JTOKEN is set
  curl -fsS -X "$1" -H "Authorization: $JAUTH${JTOKEN:+, Token=\"$JTOKEN\"}" \
    -H 'Content-Type: application/json' ${3:+-d "$3"} "$JF$2"
}
jf_login() {
  local body
  body=$(jq -n --arg u "$(env_val JELLYFIN_ADMIN_USER)" --arg p "$(env_val JELLYFIN_ADMIN_PASSWORD)" '{Username: $u, Pw: $p}')
  JTOKEN=""
  JTOKEN=$(jf POST /Users/AuthenticateByName "$body" | jq -r '.AccessToken // empty')
  [ -n "$JTOKEN" ]
}
jf_key() {  # app name: prints the API key Jellyfin keeps for it, creating it if needed
  local k
  k=$(jf GET /Auth/Keys | jq -r --arg a "$1" '[.Items[] | select(.AppName == $a)][0].AccessToken // empty')
  if [ -z "$k" ]; then
    jf POST "/Auth/Keys?app=$1" >/dev/null
    k=$(jf GET /Auth/Keys | jq -r --arg a "$1" '[.Items[] | select(.AppName == $a)][0].AccessToken // empty')
  fi
  printf '%s' "$k"
}

JELLYFIN_OK=false
say "waiting for Jellyfin"
if ! wait_for "$JF/System/Info/Public" 600; then
  problem "Jellyfin didn't start (docker logs jellyfin)"
else
  if [ "$(curl -fsS "$JF/System/Info/Public" | jq -r .StartupWizardCompleted)" != true ]; then
    # The first-run wizard, done through its API: language, admin user, finish.
    # (The wizard also sets the server name: without one Jellyfin shows its container's id.)
    jf POST /Startup/Configuration "$(jq -n --arg n "$SERVER_NAME" '{ServerName: $n, UICulture: "en-US",
         MetadataCountryCode: "CA", PreferredMetadataLanguage: "en"}')" >/dev/null \
      && jf GET /Startup/User >/dev/null \
      && jf POST /Startup/User "$(jq -n --arg n "$(env_val JELLYFIN_ADMIN_USER)" \
           --arg p "$(env_val JELLYFIN_ADMIN_PASSWORD)" '{Name: $n, Password: $p}')" >/dev/null \
      && jf POST /Startup/Complete >/dev/null \
      && say "created admin $(env_val JELLYFIN_ADMIN_USER)" || problem "Jellyfin's first-run setup failed"
  fi
  if ! jf_login; then
    problem "Jellyfin login as $(env_val JELLYFIN_ADMIN_USER) failed (check JELLYFIN_ADMIN_* in .env)"
  else
    JELLYFIN_OK=true
    # A server that went through the wizard before it set the name.
    cfg=$(jf GET /System/Configuration)
    if [ -n "$cfg" ] && [ -z "$(jq -r '.ServerName // empty' <<<"$cfg")" ]; then
      jq --arg n "$SERVER_NAME" '.ServerName = $n' <<<"$cfg" | jf POST /System/Configuration @- >/dev/null         && say "server name set to $SERVER_NAME" || problem "could not set Jellyfin's server name"
    fi
    # Plugin repositories: Jellyfin replaces the list with only its own on a fresh
    # install, so the ones in templates/jellyfin/repositories.json are added here.
    repos=$(jf GET /Repositories)
    merged=$(jq -c --slurpfile want $T/jellyfin/repositories.json       '. as $have | $have + [$want[0][] | select(.Url as $u | ($have | map(.Url) | index($u)) == null)]' <<<"$repos")
    if [ -n "$merged" ] && [ "$(jq length <<<"$merged")" != "$(jq length <<<"$repos")" ]; then
      jf POST /Repositories "$merged" >/dev/null && say "added the plugin repositories"         || problem "could not add Jellyfin's plugin repositories"
    fi
    # Plugins. Their settings files are already in place, so each starts with your settings.
    installed=$(jf GET /Plugins | jq -r '.[].Id | gsub("-"; "")')
    added=0
    while IFS=$'\t' read -r name guid; do
      if grep -qx "${guid//-/}" <<<"$installed"; then
        say "plugin $name already installed"
      elif jf POST "/Packages/Installed/$(jq -rn --arg n "$name" '$n | @uri')?assemblyGuid=$guid" >/dev/null; then
        say "installed plugin $name"; added=$((added + 1))
      else
        problem "could not install Jellyfin plugin $name"
      fi
    done < <(jq -r '.[] | [.name, .guid] | @tsv' $T/jellyfin/plugins.json)
    if [ $added -gt 0 ]; then
      say "restarting Jellyfin to load the new plugins"
      docker compose restart jellyfin >/dev/null
      sleep 10
      wait_for "$JF/System/Info/Public" 600 && jf_login || { problem "Jellyfin didn't come back after the restart"; JELLYFIN_OK=false; }
    fi
  fi
fi

if $JELLYFIN_OK; then
  # Libraries: names, folders and options as in templates/jellyfin/libraries.json.
  existing=$(jf GET /Library/VirtualFolders | jq -r '.[].Name')
  created=0
  while IFS= read -r lib; do
    name=$(jq -r .Name <<<"$lib")
    if grep -qxF "$name" <<<"$existing"; then say "library $name already there"; continue; fi
    if jf POST "/Library/VirtualFolders?name=$(jq -rn --arg n "$name" '$n | @uri')&collectionType=$(jq -r .CollectionType <<<"$lib")&refreshLibrary=false" \
         "$(jq -c '{LibraryOptions}' <<<"$lib")" >/dev/null; then
      say "created library $name"; created=$((created + 1))
    else
      problem "could not create Jellyfin library $name"
    fi
  done < <(jq -c '.[]' $T/jellyfin/libraries.json)
  [ $created -eq 0 ] || jf POST /Library/Refresh >/dev/null || true

  # Keys for the apps that tell Jellyfin about new files (Seerr makes its own).
  JELLYFIN_KEY_SONARR=$(jf_key Sonarr)
  JELLYFIN_KEY_RADARR=$(jf_key Radarr)
  JELLYFIN_KEY_BAZARR=$(jf_key Bazarr)
  folders=$(jf GET /Library/VirtualFolders)
  lib_id() { jq -r --arg n "$1" '.[] | select(.Name == $n) | .ItemId' <<<"$folders"; }
  JF_ID_MOVIES=$(lib_id Movies); JF_ID_MOVIES_LOCAL=$(lib_id "Movies Local"); JF_ID_SHOWS_LOCAL=$(lib_id "Shows Local")
fi

# ---------------------------------------------------------------------------------
arr() {  # app method path [body, or @- for stdin]
  local port ver
  case $1 in sonarr) port=8989 ver=v3 ;; radarr) port=7878 ver=v3 ;; prowlarr) port=9696 ver=v1 ;; esac
  curl -fsS -X "$2" -H "X-Api-Key: $(env_val "${1^^}_API_KEY")" -H 'Content-Type: application/json' \
    ${4:+--data-binary "$4"} "http://localhost:$port/api/$ver/$3"
}
arr_login() {  # app: sets the web login, unless it has one
  local host
  host=$(arr "$1" GET config/host) || { problem "$1 API not reachable"; return 1; }
  if [ -n "$(jq -r '.username // empty' <<<"$host")" ]; then say "login already set"; return 0; fi
  jq --arg u "$ADMIN_USER" --arg p "$ADMIN_PASSWORD" '.authenticationMethod = "forms"
      | .authenticationRequired = "enabled" | .username = $u | .password = $p | .passwordConfirmation = $p' <<<"$host" \
    | arr "$1" PUT config/host @- >/dev/null && say "login set ($ADMIN_USER)" || problem "could not set $1's login"
}
arr_add() {  # app endpoint json: adds it unless one with the same name exists
  local name; name=$(jq -r .name <<<"$3")
  if arr "$1" GET "$2" | jq -e --arg n "$name" 'any(.[]; .name == $n)' >/dev/null; then
    say "$2 '$name' already there"
  elif arr "$1" POST "$2?forceSave=true" "$3" >/dev/null; then
    say "added $2 '$name'"
  else
    problem "could not add $1 $2 '$name'"
  fi
}

for app in sonarr radarr; do
  step "${app^}"
  port=$([ $app = sonarr ] && echo 8989 || echo 7878)
  wait_for "http://localhost:$port/ping" 300 || { problem "$app didn't start"; continue; }
  arr_login $app || continue
  if ! $JELLYFIN_OK; then problem "$app: skipped, Jellyfin isn't set up"; continue; fi
  S=$(render $T/$app/settings.json json) || { problem "$app: template"; continue; }

  have=$(arr $app GET rootfolder | jq -r '.[].path')
  while IFS= read -r p; do
    grep -qxF "$p" <<<"$have" && { say "root folder $p already there"; continue; }
    arr $app POST rootfolder "$(jq -n --arg p "$p" '{path: $p}')" >/dev/null \
      && say "added root folder $p" || problem "$app: could not add root folder $p"
  done < <(jq -r '.rootFolders[]' <<<"$S")

  arr_add $app downloadclient "$(jq -c .downloadClient <<<"$S")"
  arr_add $app notification "$(jq -c .notification <<<"$S")"
  # Custom formats follow templates/ every run (a changed pattern reaches existing machines).
  formats=$(arr $app GET customformat)
  while IFS= read -r cf; do
    name=$(jq -r .name <<<"$cf")
    id=$(jq -r --arg n "$name" '.[] | select(.name == $n) | .id' <<<"$formats" | head -1)
    if [ -n "$id" ]; then
      arr $app PUT "customformat/$id" "$(jq --argjson id "$id" '.id = $id' <<<"$cf")" >/dev/null         && say "customformat '$name' applied" || problem "$app: could not update customformat '$name'"
    else
      arr_add $app customformat "$cf"
    fi
  done < <(jq -c '.customFormats[]' <<<"$S")

  # Size limits per quality (MB per minute), from templates/. Applied every run.
  arr $app GET qualitydefinition \
    | jq --slurpfile want $T/$app/qualitydefinitions.json 'map(. as $d | $want[0][$d.quality.name] as $w
        | if $w then .minSize = $w.min | .preferredSize = $w.preferred | .maxSize = $w.max else . end)' \
    | arr $app PUT qualitydefinition/update @- >/dev/null \
    && say "size limits applied" || problem "$app: could not apply the size limits"

  # The PROFILES (QUALITY_PROFILE and QUALITY_PROFILE_4K in .env), rebuilt every run by
  # scripts/quality-profile.jq: the listed qualities as one group, x265 scored, upgrades
  # as in templates/. Every other profile is removed, unless something uses it.
  if schema=$(arr $app GET qualityprofile/schema) && cfs=$(arr $app GET customformat) \
     && profiles=$(arr $app GET qualityprofile); then
    known=$(jq -c '[.items[] | if .quality then .quality.name else .items[].quality.name end]' <<<"$schema")
    keep=()
    for p in "${PROFILES[@]}"; do
      IFS='|' read -r name quals x265 <<<"$p"
      keep+=("$name")
      q=$(jq -cn --arg q "$quals" '$q | split(",") | map(gsub("^ +| +$"; "")) | map(select(. != ""))')
      unknown=$(jq -rn --argjson q "$q" --argjson k "$known" '[$q[] | select(IN($k[]) | not)] | join(", ")')
      if [ -n "$unknown" ] || [ "$q" = "[]" ]; then
        problem "$app: profile $name: no quality named '$unknown' (check the QUALITIES settings in .env)"; continue
      fi
      body=$(jq --arg name "$name" --argjson q "$q" --argjson x265 "$x265" --argjson cfs "$cfs" \
               --argjson want "$(jq -c .qualityProfile <<<"$S")" -f scripts/quality-profile.jq <<<"$schema")
      id=$(jq -r --arg n "$name" '.[] | select(.name == $n) | .id' <<<"$profiles" | head -1)
      if [ -n "$id" ]; then
        arr $app PUT "qualityprofile/$id" "$(jq --argjson id "$id" '.id = $id' <<<"$body")" >/dev/null
      else
        arr $app POST qualityprofile "$body" >/dev/null
      fi && say "profile $name: $(jq -r 'join(", ")' <<<"$q") as one group, x265 $x265" \
         || problem "$app: could not save profile $name"
    done
    while IFS=$'\t' read -r id name; do
      printf '%s\n' "${keep[@]}" | grep -qxF "$name" && continue
      arr $app DELETE "qualityprofile/$id" >/dev/null 2>&1 \
        && say "removed profile $name" || say "kept profile $name (something uses it)"
    done < <(jq -r '.[] | "\(.id)\t\(.name)"' <<<"$profiles")
  else
    problem "$app: could not read its quality profiles"
  fi

  for section in naming mediamanagement downloadclient indexer; do
    jq -s '.[0] * .[1]' <(arr $app GET "config/$section") <(jq ".config.$section" <<<"$S") \
      | arr $app PUT "config/$section" @- >/dev/null \
      && say "$section settings applied" || problem "$app: could not apply $section settings"
  done
done

# ---------------------------------------------------------------------------------
step "Prowlarr"
if wait_for http://localhost:9696/ping 300; then
  arr_login prowlarr
  apps=$(render $T/prowlarr/applications.json json) || problem "prowlarr: template"
  while IFS= read -r a; do arr_add prowlarr applications "$a"; done < <(jq -c '.[]' <<<"${apps:-[]}")
  say "add your indexer by hand (Indexers -> Add); it syncs to Sonarr and Radarr"
else
  problem "prowlarr didn't start"
fi

# ---------------------------------------------------------------------------------
step "Bazarr"
BYAML=config/bazarr/config/config.yaml
if [ ! -e "$BYAML" ]; then
  if $JELLYFIN_OK; then
    BAZARR_PASSWORD_MD5=$(printf '%s' "$ADMIN_PASSWORD" | md5sum | cut -d' ' -f1)   # how Bazarr stores it
    seed $T/bazarr/config.yaml "$BYAML" yaml
    # OpenSubtitles.com only works with an account: without one, leave it off.
    [ -n "$(env_val OPENSUBTITLES_USERNAME)" ] && [ -n "$(env_val OPENSUBTITLES_PASSWORD)" ]       || sed -i '/^  - opensubtitlescom$/d' "$BYAML"
    chown -R "$PUID:$PGID" config/bazarr
    docker compose up -d bazarr >/dev/null || problem "could not start bazarr"
  else
    problem "bazarr: skipped, Jellyfin isn't set up"
  fi
fi
if [ -e "$BYAML" ] && wait_for http://localhost:6767/ 300; then
  sleep 5
  curl -fsS -H "X-API-KEY: $(env_val BAZARR_API_KEY)" --data-urlencode "languages-enabled=en" \
    --data-urlencode "languages-profiles=$(jq -c . $T/bazarr/languages-profiles.json)" \
    http://localhost:6767/api/system/settings >/dev/null \
    && say "English language profile set" || problem "bazarr: could not set the language profile"
fi

# ---------------------------------------------------------------------------------
step "Seerr"
SAPI=http://localhost:5055/api/v1
seerr() {  # method path [body]
  curl -fsS -X "$1" -H "X-Api-Key: $(env_val SEERR_API_KEY)" -H 'Content-Type: application/json' \
    ${3:+-d "$3"} "$SAPI$2"
}
say "waiting for Seerr"
if ! wait_for "$SAPI/status" 300; then
  problem "Seerr didn't start (docker logs seerr)"
elif ! $JELLYFIN_OK; then
  problem "seerr: skipped, Jellyfin isn't set up"
else
  if [ "$(curl -fsS "$SAPI/settings/public" | jq -r .mediaServerType)" != 2 ]; then
    # Signing in with the Jellyfin admin makes it Seerr's admin and links Jellyfin.
    email=$(env_val SEERR_ADMIN_EMAIL); email=${email:-$(env_val JELLYFIN_ADMIN_USER)}
    jq -n --arg u "$(env_val JELLYFIN_ADMIN_USER)" --arg p "$(env_val JELLYFIN_ADMIN_PASSWORD)" --arg e "$email" \
        '{username: $u, password: $p, email: $e, hostname: "gluetun", port: 8096, useSsl: false, urlBase: "", serverType: 2}' \
      | curl -fsS -H 'Content-Type: application/json' -d @- "$SAPI/auth/jellyfin" >/dev/null \
      && say "linked Jellyfin, admin $(env_val JELLYFIN_ADMIN_USER)" || problem "Seerr's Jellyfin sign-in failed"
  fi
  seerr POST /settings/jellyfin/library/sync >/dev/null || true
  seerr GET /settings/jellyfin/library | jq -r '.[] | select(.enabled | not) | .id' | while read -r lib; do
    seerr PUT "/settings/jellyfin/library/$lib" '{"enabled":true}' >/dev/null && say "enabled library $lib"
  done
  for app in radarr sonarr; do
    count=$(seerr GET "/settings/$app" | jq length) || { problem "seerr: could not read its $app settings"; continue; }
    if [ "$count" != 0 ]; then say "$app already linked"; continue; fi
    want=$(render $T/seerr/$app.json json) || { problem "seerr: $app template"; continue; }
    want=$(jq --arg n "$QUALITY_PROFILE" '.activeProfileName = $n' <<<"$want")
    # Profile ids differ per machine; look up QUALITY_PROFILE's.
    pid=$(arr $app GET qualityprofile | jq -r --arg n "$QUALITY_PROFILE" '.[] | select(.name == $n) | .id')
    [ -z "$pid" ] || want=$(jq --argjson id "$pid" '.activeProfileId = $id' <<<"$want")
    seerr POST "/settings/$app" "$want" >/dev/null && say "linked $app" || problem "seerr: could not link $app"
  done
  # New downloads show as available within a minute of Jellyfin adding them (default: 5).
  seerr POST /settings/jobs/jellyfin-recently-added-scan/schedule '{"schedule":"0 * * * * *"}' >/dev/null     && say "recently-added scan every minute" || problem "seerr: could not set the recently-added scan schedule"
  seerr POST /settings/jobs/jellyfin-full-scan/schedule '{"schedule":"0 0 6 * * *"}' >/dev/null \
    && say "full library scan every day at 6:00" || problem "seerr: could not set the scan schedule"
  seerr POST /settings/initialize >/dev/null || problem "seerr: could not finish setup"
fi

# ---------------------------------------------------------------------------------
step "Cron jobs"
# The jobs README's Maintenance section describes, in root's crontab. Earlier lines
# for these scripts are replaced; other cron jobs are kept. A secondary machine sharing
# a Windscribe port runs its nightly job at 04:05, after the primary renewed the port.
JOBS='port-sync.sh|nightly.sh|after-boot.sh|qbit-watchdog.sh|livetv-watchdog.sh|shrink-images.sh'
minute=0
[ "$(env_val PORT_REFRESH)" = yes ] && [ "$(env_val ROLE)" = secondary ] && minute=5
want="$minute 4 * * * $PWD/scripts/nightly.sh --reboot >> /var/log/nightly.log 2>&1
@reboot $PWD/scripts/after-boot.sh >> /var/log/nightly.log 2>&1
*/5 * * * * $PWD/scripts/qbit-watchdog.sh >> /var/log/nightly.log 2>&1
* * * * * $PWD/scripts/livetv-watchdog.sh >> /var/log/nightly.log 2>&1
0 6 * * * $PWD/scripts/shrink-images.sh >> /var/log/shrink-images.log 2>&1"
if ! command -v crontab >/dev/null; then
  problem "crontab isn't installed (apt install cron; systemctl enable --now cron)"
else
  have=$(crontab -l 2>/dev/null)
  if [ "$(grep -E "$JOBS" <<<"$have")" = "$want" ]; then
    say "already installed"
  elif { grep -vE "$JOBS" <<<"$have" | grep -v '^$'; printf '%s\n' "$want"; } | crontab -; then
    say "installed: nightly at 04:0$minute, artwork shrinking at 06:00, after-boot, qBittorrent and Live TV watchdogs"
  else
    problem "could not install the cron jobs"
  fi
fi

# ---------------------------------------------------------------------------------
step "Done"
if [ ${#PROBLEMS[@]} -gt 0 ]; then
  echo "Problems (fix them, then run this again; finished steps are skipped):"
  printf '  - %s\n' "${PROBLEMS[@]}"
fi
HOST=$(hostname -I 2>/dev/null | awk '{print $1}'); HOST=${HOST:-<this machine>}
JF_USER=$(env_val JELLYFIN_ADMIN_USER)
cat <<EOF

Left to do by hand, in this order:
  1. Prowlarr (http://$HOST:9696, login $ADMIN_USER): add your indexer.
  2. Dispatcharr (http://$HOST:9191, login $D_USER): add the IPTV provider
     under M3U & EPG Manager and the EPG source, then refresh both.
  3. Dispatcharr: pick what you want. Live TV: add the channels and run Auto-match
     EPG on them. Movies and series: turn on their groups in the provider's account.
     Whatever you leave off stays out of Jellyfin.
  4. Jellyfin (http://$HOST:8096, login $JF_USER):
     Dashboard -> Live TV -> Refresh Guide Data, so the channels and guide show up.
  5. Jellyfin: Dashboard -> Plugins -> Xtream Library: pick the movie/series
     categories, then Sync. Wait for it to finish.
  6. Jellyfin: Dashboard -> Libraries -> Scan All Libraries. Wait for it to finish.
  7. Seerr (http://$HOST:5055, login $JF_USER):
     Settings -> Jellyfin -> Start Scan, so it knows what you already have.
  8. Tailscale: open the login link from \`docker logs tailscale\`.
EOF
[ ${#PROBLEMS[@]} -eq 0 ]
