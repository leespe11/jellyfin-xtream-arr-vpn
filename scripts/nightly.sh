#!/usr/bin/env bash
# Nightly job, run by cron on every machine. In order:
#   1. port-sync.sh (Windscribe port renewal, when PORT_REFRESH=yes)
#   2. host setup that should always hold: timezone + NTP, docker log limits,
#      journald cap, no sleep/suspend, log rotation
#   3. OS updates (apt full-upgrade)
#   4. git pull, docker compose up -d (new tags only), remove unused images; then,
#      unless rebooting, after-boot.sh reattaches anything cut off from gluetun;
#      then the result of this morning's shrink-images.sh (its own 06:00 cron job)
#   5. health: SMART drive status, disk space, containers; SSD TRIM on Sundays
#   6. report to Discord (DISCORD_WEBHOOK in .env)
#   7. reboot, only with --reboot (which the cron line passes) and only when the
#      OS updates need it or on Sundays. VPN trouble is handled by qbit-watchdog.sh.
# Usage: nightly.sh [--reboot]     (run by hand without --reboot to test it)
set -uo pipefail
cd "$(dirname "$0")/.."
source scripts/lib.sh

# Everything lives in main(), so bash has read the whole script before
# `git pull` can replace this file mid-run.
main() {
  local REBOOT=false; [ "${1:-}" = "--reboot" ] && REBOOT=true
  local problems="" changes=""
  problem() { problems+="• $*"$'\n'; echo "PROBLEM: $*"; }
  change()  { changes+="• $*"$'\n'; echo "$*"; }
  step()    { echo; echo "--- $* ---"; }
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
  echo; echo "=========== $(date) nightly ==========="

  # ------------------------------------------------------------------
  step "Port sync"
  local port_before; port_before=$(env_val TORRENT_PORT)
  if [ "$(env_val PORT_REFRESH)" = yes ]; then
    scripts/port-sync.sh || problem "port-sync.sh failed (see /var/log/nightly.log)"
    [ "$(env_val TORRENT_PORT)" = "$port_before" ] \
      || change "torrent port $port_before → $(env_val TORRENT_PORT)"
  fi

  # ------------------------------------------------------------------
  step "Host setup"
  local tz; tz=$(env_val TZ)
  if [ -n "$tz" ] && [ "$(timedatectl show -p Timezone --value)" != "$tz" ]; then
    timedatectl set-timezone "$tz" && systemctl restart cron
    change "host timezone set to $tz"
  fi
  timedatectl set-ntp true || problem "could not turn on NTP time sync"

  # Container logs: at most 3 x 10 MB each, so they can't fill the disk.
  local want='{"log-driver":"local","log-opts":{"max-size":"10m","max-file":"3"}}'
  if [ "$(jq -c . /etc/docker/daemon.json 2>/dev/null)" != "$want" ]; then
    mkdir -p /etc/docker; jq . <<<"$want" > /etc/docker/daemon.json
    systemctl restart docker
    docker compose up -d --force-recreate   # existing containers keep old log settings until recreated
    change "docker log limits applied"
  fi

  if [ ! -f /etc/systemd/journald.conf.d/size.conf ]; then
    mkdir -p /etc/systemd/journald.conf.d
    printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/size.conf
    systemctl restart systemd-journald
  fi
  systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1
  # This job does the apt updates; stop Ubuntu's own updater competing for the lock.
  systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
  printf '/var/log/nightly.log /var/log/port-sync.log /var/log/shrink-images.log {\n  monthly\n  rotate 6\n  compress\n  missingok\n  notifempty\n}\n' \
    > /etc/logrotate.d/media-stack

  # ------------------------------------------------------------------
  step "OS updates"
  local APT="apt-get -y -q -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
  if $APT update >/dev/null; then
    local out n
    out=$($APT full-upgrade 2>&1) || problem "apt full-upgrade failed"
    echo "$out"
    n=$(grep -oE '^[0-9]+ upgraded' <<<"$out" | cut -d' ' -f1)
    [ "${n:-0}" = 0 ] || change "$n OS packages upgraded"
    $APT autoremove --purge >/dev/null; $APT clean
  else
    problem "apt update failed (no internet?)"
  fi

  local reboot_why="" rebooting=false
  if   [ -f /var/run/reboot-required ]; then reboot_why="OS updates need it"
  elif [ "$(date +%u)" = 7 ];            then reboot_why="weekly reboot"; fi
  $REBOOT && [ -n "$reboot_why" ] && rebooting=true

  # ------------------------------------------------------------------
  step "Stack update"
  local before after
  before=$(git rev-parse --short HEAD)
  if git pull -q --ff-only; then
    after=$(git rev-parse --short HEAD)
    [ "$before" = "$after" ] || change "repo $before → $after: $(git log -1 --format=%s)"
  else
    problem "git pull failed"
  fi
  # No `docker compose pull`: up -d only downloads images it doesn't have, so a
  # container changes only when you push a new tag. Unchanged ones are left running.
  docker compose up -d --remove-orphans || problem "docker compose up failed"
  # gluetun may have been recreated above or by port-sync.sh, which cuts off the
  # containers on its network. After a reboot @reboot runs after-boot.sh instead.
  $rebooting || scripts/after-boot.sh || problem "after-boot.sh failed (see /var/log/nightly.log)"

  # Every image no container uses; volumes and config/ are never touched.
  local freed
  freed=$(docker image prune -af | awk -F': ' '/reclaimed space/{print $2}')
  [ -z "$freed" ] || [ "$freed" = 0B ] || change "removed old images, freed $freed"

  # ------------------------------------------------------------------
  step "Shrink Jellyfin artwork"
  # shrink-images.sh runs on its own at 06:00 (a first run can take hours); this reports
  # its last result, once.
  local result=config/jellyfin/.shrink-images-result shrink
  if [ -s "$result" ]; then
    shrink=$(cat "$result"); echo "$shrink"; rm -f "$result"
    [[ $shrink == "shrank 0 images"* ]] || change "artwork: $shrink"
  else
    echo "nothing new to report"
  fi

  # ------------------------------------------------------------------
  step "Health"
  local drives="" d out status temp realloc
  for d in $(lsblk -dno NAME,TYPE | awk '$2=="disk"{print "/dev/"$1}'); do
    out=$(smartctl -H -A "$d" 2>/dev/null)
    if   grep -qiE 'FAILED|FAILING' <<<"$out"; then status="❌ FAILING"; problem "SMART says $d is failing: replace it"
    elif grep -qiE 'PASSED|: OK' <<<"$out";   then status="✅ healthy"
    else status="❔ no SMART data"; fi
    temp=$(awk '/Temperature_Celsius/{print $10; exit} /^Temperature:/{print $2; exit}' <<<"$out")
    realloc=$(awk '/Reallocated_Sector_Ct/{print $10}' <<<"$out")
    [ -z "$realloc" ] || [ "$realloc" = 0 ] || problem "$d has $realloc reallocated sectors (early sign of failure)"
    drives+="\`$d\` $(lsblk -dno SIZE "$d" | xargs) $status${temp:+, ${temp}°C}"$'\n'
  done

  local space="" target pct avail
  while read -r target pct avail; do
    [ "${pct%\%}" -lt 90 ] || problem "$target is $pct full"
    space+="\`$target\` $pct used, $avail free"$'\n'
  done < <(df -h -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs --output=target,pcent,avail | tail -n +2)

  [ "$(date +%u)" != 7 ] || fstrim -av || true   # weekly SSD TRIM

  sleep 60   # give containers recreated above time to pass their health checks
  local containers="" name state health icon
  while read -r name state; do
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$name")
    icon="✅"
    if [ "$state" != running ] || [ "$health" = unhealthy ]; then
      icon="❌"; problem "container $name is $state${health:+ ($health)}"
    fi
    containers+="$icon $name"$'\n'
  done < <(docker compose ps -a --format '{{.Name}} {{.State}}')

  # ------------------------------------------------------------------
  step "Report"
  local title color body name
  name=$(env_val SERVER_NAME); name=${name:-$(hostname)}
  if [ -n "$problems" ]; then title="⚠️ $name: problems found"; color=15548997
  else                        title="✅ $name: nightly run OK";  color=5763719; fi
  body="${problems:+**Problems**
$problems
}${changes:+**Changes**
$changes
}**Torrent port** $(p=$(env_val TORRENT_PORT); echo "${p:-none}")

**Drives**
${drives:-none found
}
**Disk space**
$space
**Containers**
${containers:-none
}
_$(if $rebooting; then echo "Rebooting now ($reboot_why)."
   elif $REBOOT; then echo "No reboot needed tonight."
   else echo "Manual run, not rebooting${reboot_why:+ (would reboot: $reboot_why)}."; fi)_"
  echo "$title"; echo "$body"

  local hook notify; hook=$(env_val DISCORD_WEBHOOK); notify=$(env_val DISCORD_NOTIFY)
  if [ -n "$hook" ] && { [ "$notify" != failures ] || [ -n "$problems" ]; }; then
    jq -n --arg t "$title" --arg d "${body:0:4000}" --argjson c "$color" \
      '{username:"Media Server", embeds:[{title:$t, description:$d, color:$c, timestamp:(now|todate)}]}' \
    | curl -fsS -m 15 --retry 3 -H 'Content-Type: application/json' -d @- "$hook" >/dev/null \
    || echo "Discord post failed"
  fi

  # ------------------------------------------------------------------
  if $rebooting; then
    sync
    systemctl reboot
  fi
}

main "$@"; exit
