#!/usr/bin/env bash
# Shrinks the artwork Jellyfin downloads (posters, backdrops, thumbs, logos, actor photos)
# in config/jellyfin/metadata (or JELLYFIN_BULK_DIR/metadata). Jellyfin keeps them at full size (often 2000-4000 px and
# several MB), though clients only ever ask for small versions. Jellyfin has no setting
# for this. Each image is scaled down to fit the limits below and re-encoded only when
# that saves space; it keeps its name, format (JPEG stays JPEG, logos stay transparent
# PNG), owner and date, so Jellyfin sees no change. Trickplay images are left alone.
#
# Usage (as root, from the stack folder):
#   scripts/shrink-images.sh            first run: every image (hours; run it in screen or
#                                       with nohup), then only images added since the last run
#   scripts/shrink-images.sh --all      every image again, e.g. after changing the limits
#   scripts/shrink-images.sh --nightly  what nightly.sh runs: only images added since the
#                                       last run; fails if there was never a first run
# The last line printed is the summary nightly.sh reports. Needs ImageMagick.
set -uo pipefail
cd "$(dirname "$0")/.."

# kind      fit within  JPEG quality  re-encode anyway above
LIMITS='
poster      960x1200    82            200
people      500x750     80            100
thumb       1280x720    80            250
backdrop    1920x1080   80            450
logo        800x400     -             150
'
# poster: portrait posters end up 800x1200, wide ones (episode stills) 960x540.
# The last column (KB) re-encodes oversized files that are already small enough in pixels.

source scripts/lib.sh
META=$(jellyfin_bulk_dir)/metadata      # config/jellyfin/metadata, or JELLYFIN_BULK_DIR's
MARKER=jellyfin/.shrink-images-last-run
JOBS=${JOBS:-4}

MODE=new
case "${1:-}" in
  --all) MODE=all ;;
  --nightly) [ -f "$MARKER" ] || { echo "never run by hand yet: run scripts/shrink-images.sh once first"; exit 1; } ;;
  "") [ -f "$MARKER" ] || MODE=all ;;
  *) echo "usage: $0 [--all|--nightly]"; exit 2 ;;
esac

[ -d "$META/library" ] || { echo "no Jellyfin metadata on this machine, nothing to do"; exit 0; }
if command -v magick >/dev/null; then IM=magick; ID="magick identify"          # ImageMagick 7
elif command -v convert >/dev/null; then IM=convert; ID=identify                 # ImageMagick 6
else echo "ImageMagick is not installed (apt install imagemagick)"; exit 1; fi

shrink_one() {  # prints "<bytes before> <bytes after>" for each image it shrank
  local f=$1 name=${1##*/} kind line fit q maxkb w h fmt size tmp new
  case "$f" in
    */trickplay/*) return ;;
    "$META"/People/*) kind=people ;;
    *) case "$name" in
         poster.*|folder.*)    kind=poster ;;
         backdrop*)            kind=backdrop ;;
         thumb.*|landscape.*)  kind=thumb ;;
         logo.*)               kind=logo ;;
         *) return ;;
       esac ;;
  esac
  line=$(grep -m1 "^$kind " <<<"$LIMITS"); read -r _ fit q maxkb <<<"$line"

  read -r w h fmt < <($ID -ping -format '%w %h %m\n' "$f[0]" 2>/dev/null) || return
  [ "$fmt" = JPEG ] || [ "$fmt" = PNG ] || return
  size=$(stat -c %s "$f")
  if [ "$w" -le "${fit%x*}" ] && [ "$h" -le "${fit#*x}" ] && [ "$size" -le $((maxkb * 1024)) ]; then
    return
  fi

  tmp="$f.shrinking"
  if [ "$fmt" = JPEG ]; then
    [ "$q" = - ] && q=82
    $IM "$f[0]" -resize "$fit>" -strip -sampling-factor 4:2:0 -interlace JPEG -quality "$q" "jpg:$tmp" 2>/dev/null
  else
    $IM "$f[0]" -resize "$fit>" -strip -define png:compression-level=9 "png:$tmp" 2>/dev/null
  fi || { rm -f "$tmp"; return; }

  new=$(stat -c %s "$tmp")
  if [ "$new" -lt "$size" ]; then
    chown --reference="$f" "$tmp"; chmod --reference="$f" "$tmp"; touch -r "$f" "$tmp"
    mv -f "$tmp" "$f" && echo "$size $new"
  else
    rm -f "$tmp"
  fi
}
export -f shrink_one; export IM ID META LIMITS

start=$(date +%s)
newer=(); [ "$MODE" = new ] && newer=(-newer "$MARKER")
echo "$(date) shrinking $([ "$MODE" = all ] && echo every image || echo "images added since $(date -r "$MARKER")")"

find "$META/library" "$META/People" -type f -not -name '*.shrinking' "${newer[@]}" \
     \( -path "$META/People/*" -o -name 'poster.*' -o -name 'folder.*' -o -name 'backdrop*' \
        -o -name 'thumb.*' -o -name 'landscape.*' -o -name 'logo.*' \) -print0 \
| xargs -0 -r -n 50 -P "$JOBS" bash -c 'for f; do shrink_one "$f"; done' _ \
| awk '{n++; b+=$1; a+=$2} END {f=b-a; if (f >= 1e9) s=sprintf("%.1f GB", f/1e9); else s=sprintf("%.0f MB", f/1e6)
       printf "shrank %d images, freed %s\n", n, s}'

# Images Jellyfin writes while this runs are newer than $start, so the next run gets them.
touch -d "@$start" "$MARKER"
