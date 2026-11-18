#!/bin/sh
# Start-up wrapper for the Seerr container (its entrypoint in docker-compose.yml).
# Seerr only marks a Jellyfin movie or episode as available when Jellyfin reports a
# video stream with a width: <= 2000 px counts as the normal version, wider as 4K.
# Jellyfin never reads the streams of .strm files during a library scan (only on first
# playback), so the Xtream Library plugin's movies and series have no width, and Seerr
# skips them without an error: they keep a Request button although they're in Jellyfin.
# This edits Seerr's scanner so an item with no stream info counts as the normal
# version, then starts Seerr as usual. Real files (and 4K ones) are unaffected.
# If an update changes those lines, Seerr starts unpatched and says so in its log:
#   docker logs seerr 2>&1 | grep patch
# After the first start with this, run Settings -> Jobs -> Jellyfin Full Library Scan.
# To undo: remove this script's entrypoint and volume lines from docker-compose.yml,
# then `docker compose up -d seerr`.

SCANNER=/app/dist/lib/scanners/jellyfin/index.js

say() { echo "[seerr-patch] $*"; }

edit() {  # what, extended regex, replacement, regex that matches once already edited
  if grep -qE "$4" "$SCANNER" 2>/dev/null; then
    say "$1 (already applied)"
  elif [ "$(grep -cE "$2" "$SCANNER" 2>/dev/null)" = "1" ]; then
    sed -i -E "s/$2/$3/" "$SCANNER" && say "$1"
  else
    say "WARNING: '$1' not applied, the code in $SCANNER has changed; Seerr runs unpatched"
  fi
}

# Movies: `if (hasOtherResolution || (!this.enable4kMovie && has4k))` decides whether
# the normal version is available. Adding `!has4k` makes "no stream info" count too.
edit "movies without stream info count as available" \
  'if \(hasOtherResolution \|\| \(' \
  'if (hasOtherResolution || !has4k || (' \
  'if \(hasOtherResolution \|\| !has4k \|\| \('

# Series: each episode adds to the normal count only `if (hasStandard)`.
edit "episodes without stream info count as available" \
  'if \(hasStandard\)$' \
  'if (hasStandard || !has4k)' \
  'if \(hasStandard \|\| !has4k\)$'

# Overriding the entrypoint clears the image's command, so start Seerr the way it does.
exec npm start
