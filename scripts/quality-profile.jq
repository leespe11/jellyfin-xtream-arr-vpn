# Builds a Sonarr/Radarr quality profile from the app's blank one (GET qualityprofile/schema).
# The qualities listed in $q become one group (one quality: just that one), allowed, and the
# cutoff; every other quality is off. Inside a group the qualities count as equal, so the
# custom format scores decide between them: an x265 Bluray loses to any other 1080p release.
# Arguments: $name, $q (array of quality names), $x265 (score), $cfs (GET customformat),
# $want (templates/<app>/settings.json .qualityProfile).
def inq: .quality.name | IN($q[]);
def off: .allowed = false | .items |= map(.allowed = false);

. as $schema
| ([.items[] | select(.quality == null) | .id] | max // 999) as $maxgroup
# The group's name: the resolution they share (1080p, 2160p), else the profile's name.
| ([$q[] | capture("-(?<r>[0-9]+p)") | .r] | unique) as $res
| [.items[] | if .quality then . else .items[] end | select(inq) | .allowed = true] as $picked
| (if ($picked | length) == 1 then $picked[0]
   else {id: ($maxgroup + 1), name: (if ($res | length) == 1 then $res[0] else $name end),
         allowed: true, items: $picked} end) as $group
# The group goes where the first of its qualities was; groups it took qualities from keep
# the rest (a group of one becomes a plain quality, as the apps require).
| (reduce .items[] as $it ({out: [], done: false};
    (if $it.quality then ($it | inq) else any($it.items[]; inq) end) as $hit
    | if $hit and (.done | not) then .out += [$group] | .done = true else . end
    | if $it.quality then (if $hit then . else .out += [$it | off] end)
      else ([$it.items[] | select(inq | not)]) as $left
        | if ($left | length) > 1 then .out += [$it | .items = $left | off]
          elif ($left | length) == 1 then .out += [$left[0] | off]
          else . end
      end)
  | .out) as $items
| $schema
| .name = $name
| .items = $items
| .cutoff = ($group.id // $group.quality.id)
| . + ($want | {upgradeAllowed, minFormatScore, cutoffFormatScore, minUpgradeFormatScore})
| if has("language") and ($want.language != null) then .language = $want.language else . end
| .formatItems = [$cfs[] | {format: .id, name, score: (if .name == "x265" then $x265 else 0 end)}]
