# jellyfin-xtream-arr-vpn

A home media server in one Docker Compose stack: **Jellyfin** with Live TV and your IPTV
provider's movies and series, **Seerr** for requests, **Sonarr / Radarr / Prowlarr / Bazarr**
and **qBittorrent** for downloads, all behind a **VPN** (gluetun). Two scripts set up a new
machine and link every app together; cron jobs keep it healthy and up to date.

**Contents:**
[Features](#features) ·
[The stack](#the-stack) ·
[Requirements](#requirements) ·
[Setup](#setup) ·
[Settings](#settings) ·
[Maintenance](#maintenance) ·
[VPN server moves](#vpn-server-moves) ·
[Live TV](#live-tv) ·
[Provider movies and series](#movies-and-series-from-the-provider) ·
[Artwork size](#jellyfin-artwork-size) ·
[Remote access](#remote-access-tailscale) ·
[Clients](#recommended-clients) ·
[Windscribe extras](#windscribe-extras) ·
[Troubleshooting](#troubleshooting) ·
[Credits](#credits)

---

## Features

**Set up in two commands.** `scripts/generate-env.sh` asks for the few things only you
know (VPN, folders, admin login) and generates every key and password the apps use with
each other. `scripts/bootstrap.sh` then sets up and links everything: Jellyfin (admin,
Live TV, libraries, plugins and their settings), Dispatcharr's logins, qBittorrent,
Sonarr, Radarr, Prowlarr, Bazarr and Seerr. What's left by hand is your own accounts: the
Prowlarr indexer and the IPTV provider in Dispatcharr.

**Download profiles for small servers.** Sonarr and Radarr get two profiles and nothing
else: **HD-1080p**, the default for requests, which skips x265/HEVC releases (many TV
apps can't play them without the server transcoding), and **Ultra-HD** for 4K, picked
per request in Seerr. Each profile's qualities form one group, so the custom format
score decides between a WEB-DL and a Bluray, and size limits keep 1080p files small.
Nothing is re-downloaded as an upgrade. Profiles, qualities and x265 scores are
settings in `.env`.

**Live TV that survives a cheap IPTV provider.**
- `livetv-proxy` cleans up channel names and guide titles (strips tags like `US:` or
  `FHD`), sorts the channels (by name, group or Dispatcharr's order, ascending or
  descending) and numbers them 1..N.
- It also passes streams through FFmpeg so their timestamps only move forward. When the
  provider drops a stream and Dispatcharr reconnects, the provider replays a few seconds,
  which makes players freeze, loop the audio or kick you back to the guide; the proxy
  skips the replayed part instead.
- `dispatcharr-patch.sh` makes Dispatcharr wait longer between reconnects, since the
  provider refuses new connections for a few seconds after a drop.
- `livetv-watchdog.sh` moves the VPN to another server when the provider starts refusing
  streams (403 Forbidden) from the current VPN address.

**Your provider's movies and series in Jellyfin.** The Xtream Library plugin turns them
into a normal Jellyfin library through Dispatcharr, with the provider's title tags
(`NF -`, `TOP -`...) removed. `seerr-patch.sh` makes Seerr count them as available, so
they don't show a Request button for things you already have.

**A VPN that fixes itself.** Any WireGuard provider gluetun supports. `vpn-reconnect.sh`
moves to another server without restarting any container; `qbit-watchdog.sh` does it when
a server blocks torrent traffic, `livetv-watchdog.sh` when the IPTV provider blocks it.
Extra for Windscribe Pro without a static IP: the 7-day forwarded port renews itself.

**Maintenance on autopilot.** Every night: OS updates, container updates (only when a
tag in `docker-compose.yml` changes), drive health, disk space, container checks, a
Discord report, and a reboot only when needed. `after-boot.sh` reconnects containers
that started before the VPN. `shrink-images.sh` shrinks Jellyfin's artwork, which
otherwise grows past 100 GB.

**Remote access** through Tailscale, without opening ports, and the **Wholphin** Fire TV
app with Seerr requests built in.

---

## The stack

| App | Port | Login | Through the VPN | What it does |
|---|---|---|---|---|
| Jellyfin | 8096 | Jellyfin admin | yes | Movies, series, Live TV |
| Seerr | 5055 | Jellyfin admin | | Requests, discovery |
| Sonarr / Radarr | 8989 / 7878 | admin | | Series / movie downloads |
| Prowlarr | 9696 | admin | | Indexers for Sonarr and Radarr |
| Bazarr | 6767 | admin | | Subtitles |
| qBittorrent | 8080 | qBittorrent | yes, bound to the tunnel | Torrents |
| Dispatcharr | 9191 | Dispatcharr admin | yes | IPTV provider login, channels, guide, reconnects |
| livetv-proxy | 8097 (internal) | | yes | Channel clean-up, sorting, stream fixes |
| gluetun | | | is the VPN | |
| Tailscale | | | | Remote access |

"admin" is `ADMIN_USER` / `ADMIN_PASSWORD` in `.env`; the Jellyfin admin
(`JELLYFIN_ADMIN_*`), Dispatcharr admin (`DISPATCHARR_ADMIN_*`) and qBittorrent
(`QBIT_*`) logins are the same unless you change them.

**Where things live:**

| In git | Only on the machine |
|---|---|
| `docker-compose.yml`, `scripts/`, `templates/`, `.env.example` | `.env` (every login and key), `config/` (every app's settings and database) |

```
jellyfin-xtream-arr-vpn/      the stack folder (this repo)
├── .env                      your settings, logins and keys
├── config/<app>/             each app's settings and database
└── config/jellyfin/          Jellyfin (artwork and STRM files too, unless JELLYFIN_BULK_DIR is set)

/mnt/storage/                 DATA_ROOT: your media folder
├── Movies/                   MOVIES_DIR
├── Series/                   SERIES_DIR
├── torrents/                 TORRENTS_DIR
└── jellyfin/                 JELLYFIN_BULK_DIR, if you set one: artwork (metadata/) and STRM files (xtream-library/)
```

---

## Requirements

- **Ubuntu or Debian**, with your media drive mounted (e.g. at `/mnt/storage`).
- **A VPN with WireGuard** that gluetun supports. Make a WireGuard config for this machine
  on the provider's website first; `generate-env.sh` asks for its values:

  | Provider | Asked for | Forwarded port |
  |---|---|---|
  | ProtonVPN | private key; server countries | none set (Proton's port keeps changing) |
  | AirVPN | private key, address, preshared key; server regions | optional: one you made on AirVPN's site |
  | Mullvad | private key, address; server countries | none (Mullvad has no port forwarding) |
  | Windscribe | private key, address, preshared key; server regions | your static port, or renewed every week ([Windscribe extras](#windscribe-extras)) |
  | any other | everything, with empty allowed | optional |

  A forwarded port only makes torrents reach more peers; everything works without one.
- **An IPTV provider with Xtream Codes access**, for Live TV and the provider's movies.
- **A torrent indexer** for Prowlarr.

---

## Setup

Every command runs **as root** from the **stack folder** (where the repo is cloned).

### 1. Install git

```bash
sudo -i
apt install -y git
```

`bootstrap.sh` (step 4) installs everything else it needs when it's missing: Docker and
Compose, jq, curl, Python, ImageMagick, smartmontools, cron and logrotate.

### 2. Clone

```bash
cd "/home/$SUDO_USER"
git clone https://github.com/leespe11/jellyfin-xtream-arr-vpn.git
cd jellyfin-xtream-arr-vpn
```

### 3. Create `.env`

```bash
scripts/generate-env.sh
nano .env
```

It asks only for what's still empty, so you can run it again any time:

| Question | Notes |
|---|---|
| Timezone | Defaults to the machine's |
| VPN provider | `protonvpn`, `airvpn`, `mullvad`, `windscribe`, ... (gluetun's names) |
| Windscribe only: port renewal | Explained first; only if yes: this machine's role, your Windscribe login and 2FA secret |
| WireGuard private key, address, preshared key | From the provider's WireGuard config for this machine; only the ones your provider uses |
| Server regions / countries | Empty = any server |
| Forwarded port | Only for providers that forward one (see [Requirements](#requirements)) |
| Media folder | Default `/mnt/storage`, on your big drive |
| Movies, series and downloads folder names | Folders inside the media folder (defaults `Movies`, `Series`, `torrents`), so downloads are hard-linked, not copied; created if missing |
| Jellyfin artwork and STRM folder | Tens of GB. Enter keeps them in `./config/jellyfin`; give a folder on the big drive if the system drive is small |
| Admin username and password | One login for every web page |

Every other key and password is generated. `.env` is written in a fixed order with an
explanation above each setting; in `nano`, change the [settings](#settings) you want.
Values containing `$` stay in single quotes.

### 4. Set up the apps

```bash
scripts/bootstrap.sh
```

It creates the media folders, starts the stack, sets up and links every app (the
first start takes a few minutes, mostly Dispatcharr), and installs the
[cron jobs](#maintenance). At the end it lists any problems and what's left to do by hand.

It's safe to run again, for example after fixing a problem it listed: settings files are
only created when missing, finished steps are skipped, and a login that's already set is
never changed.

### 5. Finish by hand, in this order

1. **Prowlarr** (`http://<machine>:9696`, admin login): **Indexers → Add Indexer**. They
   sync to Sonarr and Radarr. A cookie-based indexer wants the cookie on one line,
   e.g. `uid=1234567; pass=abcdef...`, and the **User-Agent** of the browser you copied
   it from (`navigator.userAgent` in that browser's console).
2. **Dispatcharr** (`http://<machine>:9191`, Dispatcharr admin), **M3U & EPG Manager**:
   - Add an M3U account, type **Xtream Codes**, with the provider's server, username and
     password, **Max Streams** set to the provider's connection limit, and **Enable VOD
     Scanning** on.
   - Add an EPG source, type **XMLTV**:
     `http://<provider>/xmltv.php?username=<user>&password=<pass>`.
   - Refresh both.
3. **Dispatcharr: pick what you want.** Whatever you leave off stays out of Jellyfin.
   - **Live TV:** add the channels you want and run **Auto-match EPG** on them.
   - **Movies and series:** in the M3U account's **Groups**, turn on the VOD groups you
     want.
4. **Jellyfin** (`http://<machine>:8096`, Jellyfin admin): **Dashboard → Live TV →
   Refresh Guide Data**, so the channels and their guide show up.
5. **Jellyfin:** **Dashboard → Plugins → Xtream Library**, pick the movie and series
   categories, then **Run Sync Now**. Everything else in the plugin is already set. Wait
   for it to finish. After this it syncs every day at 3:00.
6. **Jellyfin:** **Dashboard → Libraries → Scan All Libraries**, and wait for it to finish.
   After this it scans every day at 4:30.
7. **Seerr** (`http://<machine>:5055`, Jellyfin admin): **Settings → Jellyfin → Start
   Scan**, so Seerr knows what you already have. After this it does a full scan every day
   at 6:00, and checks for newly added titles every minute.

All times are the machine's local time (`TZ` in `.env`). They're spaced so each step has
the previous one's results: the provider's sync, then the nightly job at 4:00 (which may
reboot), then Jellyfin's scan, then Seerr's.
8. **Tailscale**, if you want remote access: see [Remote access](#remote-access-tailscale).

### 6. Test the nightly job

```bash
scripts/nightly.sh
```

This runs everything except the reboot. The log is `/var/log/nightly.log`.

---

## Settings

All in `.env`; `.env.example` explains each one. The ones you're most likely to change:

| Setting | Default | What it does |
|---|---|---|
| `DATA_ROOT` | asked | Your media folder; mounted as `/data` in Sonarr, Radarr, Bazarr and qBittorrent |
| `MOVIES_DIR`, `SERIES_DIR`, `TORRENTS_DIR` | asked | Full paths of the folders inside `DATA_ROOT` (they must be inside it) |
| `JELLYFIN_BULK_DIR` | empty | Jellyfin's artwork and STRM files; empty = `./config/jellyfin` |
| `QUALITY_PROFILE` | `HD-1080p` | The profile Seerr requests with |
| `QUALITIES` | `WEBDL-1080p,WEBRip-1080p,Bluray-1080p,HDTV-1080p` | What it accepts, as one group |
| `X265_SCORE` | `-100` | Below 0 skips x265/HEVC releases, `0` accepts them |
| `QUALITY_PROFILE_4K` | `Ultra-HD` | A second profile, picked per request; empty = none |
| `QUALITIES_4K`, `X265_SCORE_4K` | 2160p WEB-DL, WEBRip, Bluray, HDTV; `0` | Same for the 4K profile (most 4K releases are x265) |
| `LIVETV_SORT_BY` | `name` | `name`, `group` or `dispatcharr` |
| `LIVETV_SORT_ORDER` | `asc` | `asc` or `desc` |
| `LIVETV_STRIP_PREFIXES` / `_SUFFIXES` | provider tags | Removed from channel names and guide titles |
| `VPN_SERVICE_PROVIDER` | asked | Any gluetun WireGuard provider |
| `PORT_REFRESH` | `no` | Windscribe only: `yes` renews the 7-day forwarded port ([Windscribe extras](#windscribe-extras)) |
| `DISCORD_WEBHOOK` | | Channel → Edit Channel → Integrations → Webhooks; `DISCORD_NOTIFY=failures` for problems only |

Sonarr and Radarr keep only these profiles: their other built-in ones are removed (a
profile that something uses is kept). Size limits per quality (MB per minute) are in
`templates/radarr/qualitydefinitions.json` and `templates/sonarr/...`.

**Requesting in 4K:** in Seerr's request window, open **Advanced** and pick the
**Ultra-HD** profile. Admins always see it; give other users the **Advanced Requests**
permission (Settings → Users).

**Region and language:** metadata and Seerr's streaming providers use the country of your
`TZ` (e.g. `America/Toronto` → Canada), in English, with English subtitles. To change them:
Jellyfin → Dashboard → General and each library's settings, Seerr → Settings → General,
Bazarr → Settings → Languages.

**Hardware transcoding:** with an Intel GPU (Quick Sync) or an AMD one (VA-API),
`bootstrap.sh` turns it on in Jellyfin and gives Jellyfin the GPU through
`docker-compose.override.yml`. It only reads which GPU `/sys/class/drm` lists. Other
machines (ARM, NVIDIA, none) transcode on the CPU; set it up by hand in Jellyfin →
Dashboard → Playback → Transcoding.

**After changing a setting:**
- Download settings (profiles, qualities, scores, size limits): `scripts/bootstrap.sh`.
- Live TV or VPN settings: `docker compose up -d`.

**Templates.** The app settings `bootstrap.sh` applies live in `templates/`, taken from a
working setup; `{{NAME}}` in a template is filled in from `.env`.
- Settings files (Jellyfin and plugin `.xml`, `qBittorrent.conf`, Bazarr's `config.yaml`,
  the `config.xml` files, Seerr's `settings.json`) are used only when a machine doesn't
  have that file yet.
- The Sonarr/Radarr profiles, size limits, custom formats, naming and media management
  are applied on every run, so changes made in their web pages are undone; change
  `.env` or `templates/` instead.

---

## Maintenance

Five cron jobs, which `bootstrap.sh` installs in root's crontab:

**`nightly.sh`**, at 04:00:
1. `port-sync.sh`, only with Windscribe port renewal on (see
   [Windscribe extras](#windscribe-extras)).
2. Host settings: timezone and NTP from `TZ`, container log limits (3 x 10 MB), a
   500 MB journal cap, no sleep/suspend, log rotation.
3. OS updates (`apt full-upgrade`).
4. Stack update: `git pull`, `docker compose up -d` (a container only changes when its
   tag in `docker-compose.yml` does), unused images removed.
5. The result of that morning's artwork shrinking (below).
6. Health: SMART status and temperature of each drive, free space, container states;
   SSD TRIM on Sundays.
7. Discord report: green when all is well, red with the problems.
8. Reboot, only when the OS updates need it, and on Sundays.

**`shrink-images.sh`**, at 06:00, after any reboot: shrinks Jellyfin's artwork (see
[Jellyfin artwork size](#jellyfin-artwork-size)).

**`after-boot.sh`**, at every boot: Docker starts containers without waiting for the
VPN, so the ones sharing its network (qBittorrent, Jellyfin, Dispatcharr, livetv-proxy)
can end up without one. This waits for gluetun, recreates only those that are cut off,
and stops qBittorrent if its traffic isn't going through the VPN.

**`qbit-watchdog.sh`**, every 5 minutes, and **`livetv-watchdog.sh`**, every minute:
see [VPN server moves](#vpn-server-moves).

**Updating a container:** change its tag in `docker-compose.yml`, test it, push; every
machine picks it up that night. Images left on `:latest` never update, so pin versions.

---

## VPN server moves

`scripts/vpn-reconnect.sh` moves the VPN to another server (random within
`VPN_SERVER_REGIONS` / `VPN_SERVER_HOSTNAMES`) through gluetun's control server, without
restarting any container. It tries until the public IP actually changes, then has
qBittorrent re-announce. Jellyfin and Dispatcharr drop for a few seconds.

- **`qbit-watchdog.sh`** runs it when torrents show the signs of a server that blocks
  torrent traffic: no torrent can reach its tracker, or a download sees 3+ seeders but
  connects to none. It needs the problem on two checks in a row, waits 30 minutes
  between moves, and after 3 moves that didn't help it stops and warns on Discord.
- **`livetv-watchdog.sh`** runs it when Dispatcharr logs `HTTP error 403` (the IPTV
  provider blocks some VPN addresses). It only reads Dispatcharr's log, moves at most
  once every 10 minutes, and after 3 moves within 2 hours stops and warns on Discord
  (then it's probably the provider or the account). Start the channel again after about
  a minute.

Some servers block torrent traffic for good (trackers say "End of file", downloads stall
with seeders listed). List the servers that work in `VPN_SERVER_HOSTNAMES` to avoid them.
qBittorrent is bound to the tunnel (`tun0`), so it never sends anything outside the VPN,
and `qbit-https-trackers.sh` switches new torrents' trackers to `https://` where that
works, since some servers cut off plain `http://` announces.

---

## Live TV

```
Jellyfin tuner + guide -> livetv-proxy (:8097) -> Dispatcharr (:9191) -> IPTV provider
```

`bootstrap.sh` already points Jellyfin's tuner at `http://localhost:8097/LiveTv.m3u` and
its guide at `http://localhost:8097/Epg.xml`. Channel names, sorting and the stream fix
are the `LIVETV_*` settings; how the stream fix works is described at the top of
`scripts/livetv-proxy.py`. The channels are numbered 1..N only for order, so a small script
in the **JavaScript Injector** plugin hides the numbers in Jellyfin's guide and channel
list.

**Guide looks stale after a change?** Jellyfin re-uses its downloaded guide for about an
hour. Clear it, then refresh the guide in Jellyfin:
```bash
docker exec jellyfin sh -c 'rm -fv /cache/xmltv/*'
```

**Dispatcharr reconnects (`dispatcharr-patch.sh`):** after a drop the provider can refuse
new connections for a few seconds, and Dispatcharr's 3 reconnect attempts all happen
within ~2.5 s. This start-up wrapper changes them to 6 attempts over ~15 s (values at the
top of the script). `docker logs dispatcharr 2>&1 | grep dispatcharr-patch` shows whether
it applied; a `WARNING` means an update changed Dispatcharr's code and it runs unpatched.

---

## Movies and series from the provider

The **Xtream Library** plugin writes the provider's movies and series as STRM files to
`xtream-library/` (in `JELLYFIN_BULK_DIR`, or `config/jellyfin`), read by the **Movies**
and **Shows** libraries; your own downloads are in **Movies Local** and **Shows Local**.

- It signs in to Dispatcharr (as the `DISPATCHARR_XC_USER` that `bootstrap.sh` creates),
  not the provider, so Live TV and movies share Dispatcharr's connection limit.
- The provider's title tags are removed by the plugin's **Custom Title Removal Terms**
  (Provider tab).

**Seerr sees them as available (`seerr-patch.sh`):** Seerr only marks a title available
when Jellyfin reports its video resolution, which Jellyfin doesn't read from STRM files
until they're played. This start-up wrapper edits Seerr's scanner so a title without one
counts as the normal (non-4K) version. `docker logs seerr 2>&1 | grep seerr-patch` shows
whether it applied. Seerr checks Jellyfin for new titles every minute, and runs a full
library scan every day at 6:00.

---

## Jellyfin artwork size

Jellyfin keeps every poster, backdrop, logo and actor photo at full size in `metadata/`
(in `JELLYFIN_BULK_DIR`, or `config/jellyfin`) and has no setting to limit it.
`scripts/shrink-images.sh` scales them to what clients show (posters 800x1200, backdrops
1920x1080, actor photos 500x750...; limits at the top of the script), keeping names,
formats and dates.

It runs by itself every day at 06:00, at low priority. The first run goes through every
image, which takes hours on a big library; after that it only does what Jellyfin added
since. The log is `/var/log/shrink-images.log`, and the nightly report shows how much it
freed. To redo every image (after changing the limits): `scripts/shrink-images.sh --all`.

---

## Remote access (Tailscale)

1. Add each remote device's Tailscale IP as a `/32` to `LAN_SUBNETS` in `.env`, so
   Jellyfin can reply to it, then `docker compose up -d`. For example
   `LAN_SUBNETS=172.16.0.0/12,192.168.0.0/16,100.101.102.103/32`.

   **Don't add Tailscale's whole range, `100.64.0.0/10`.** Jellyfin shares gluetun's
   network, and gluetun sends traffic for the `LAN_SUBNETS` around the VPN, out your
   normal connection. Tailscale isn't the only one using that range: some VPN providers
   hand out WireGuard addresses in it too (Windscribe's are `100.x.x.x`), and their DNS
   and gateway live there as well. List the whole range and traffic meant for the tunnel
   goes around it instead. The VPN stops working: no internet in the containers, gluetun
   unhealthy, or traffic leaving outside the VPN. One `/32` per device only matches that
   device.
2. Log in once, opening the link within a minute:
   ```bash
   docker logs -f tailscale 2>&1 | grep --line-buffered login.tailscale.com
   ```
3. In the Tailscale admin console, open the machine's **⋯** menu: rename it, and
   **Disable key expiry** (otherwise it logs out after ~6 months).
4. On your phone, install Tailscale and add the server in the Jellyfin app as
   `http://<machine-name>:8096`. SSH works too: `ssh <user>@<machine-name>`.

---

## Recommended clients

- **Computer:** the web interface, `http://<server>:8096` (or `http://<machine-name>:8096`
  through Tailscale). Jellyfin Enhanced adds Seerr requests right inside it.
- **Phone or tablet:** the official **Jellyfin** app from Google Play or the App Store.
  Away from home, turn on Tailscale on the phone first (see
  [Remote access](#remote-access-tailscale)).
- **Fire TV:** **Wholphin**, with Seerr requests built in. Install it from Wholphin's
  GitHub releases, **not** from the Amazon Appstore: the Appstore version doesn't have the
  Seerr integration (as of this writing).
  1. On the Fire TV, turn on **Settings → My Fire TV → Developer options → Install
     unknown apps** (for Downloader).
  2. Install **Downloader** from the Appstore, open it and enter the code `8668671`.
  3. In Wholphin's settings, add Seerr as `http://<server's LAN IP>:5055`, signing in
     with your Jellyfin account.

  The Wholphin plugin it needs in Jellyfin is already installed.

---

## Windscribe extras

Windscribe Pro forwards one port. With a static IP it's permanent: answer `no` to the
renewal question and give the port. Without one it's an "ephemeral" port that expires
after 7 days: answer `yes`, and the nightly job (`port-sync.sh`) renews it about every 6
days, puts it in qBittorrent and gluetun, and updates `TORRENT_PORT`. It signs in with
your Windscribe login (and your 2FA secret key, if 2FA is on). If you don't have a port
yet, leave it empty during setup: the first renewal makes one.

Machines on one Windscribe account share one forwarded port:
- Give each machine its own WireGuard config and a different `VPN_SERVER_REGIONS`.
- Set `ROLE=primary` on one: it renews the port every ~6 days at 04:00.
- Set `ROLE=secondary` on the others: they read it at 04:05 (`bootstrap.sh` sets their
  nightly job to that time).
- Every machine's clock must be in the same timezone.

---

## Troubleshooting

**`bootstrap.sh` listed problems.** Fix them and run it again; finished steps are
skipped.

**An app won't start or keeps restarting.** Look at its log:
```bash
docker logs <app> --tail 50
```

**Start one app's settings over.** This deletes that app's settings and database; the
next `bootstrap.sh` creates and links it again:
```bash
docker compose stop <app> && rm -rf config/<app> && scripts/bootstrap.sh
```

**A web page says its login or token isn't valid** after a reinstall at the same
address: the browser kept the old login. Hard-refresh with **Ctrl+Shift+R**.

**gluetun is unhealthy or has no traffic.** `docker logs gluetun --tail 50`. Usually a
WireGuard value: compare the private key, address and (Windscribe, AirVPN) preshared key
in `.env` with the provider's config for this machine, then `docker compose up -d`.

**Seerr shows Request on things you already have.** Run Jellyfin's **Scan All
Libraries**, then Seerr's **Settings → Jellyfin → Start Scan**.

---

## Credits

This repo is glue: scripts and settings that set up and connect other people's work.
All the real work is theirs; please star and support their projects.

| Project | What it does here |
|---|---|
| [Jellyfin](https://github.com/jellyfin/jellyfin) | The media server |
| [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) | IPTV provider login, channels, guide, VOD |
| [Jellyfin Xtream Library](https://github.com/firestaerter3/Jellyfin-Xtream-Library) by firestaerter3 | The provider's movies and series as a Jellyfin library |
| [Jellyfin Enhanced](https://github.com/n00bcodr/Jellyfin-Enhanced) by n00bcodr | Seerr requests and extras inside Jellyfin |
| [JavaScript Injector](https://github.com/n00bcodr/Jellyfin-JavaScript-Injector) by n00bcodr | Small tweaks to Jellyfin's web pages |
| [Intro Skipper](https://github.com/intro-skipper/intro-skipper) | Skip intros and credits |
| [Wholphin](https://github.com/damontecres/Wholphin) by damontecres | Fire TV / Android TV app and its Jellyfin plugin |
| [Seerr](https://github.com/seerr-team/seerr) | Requests and discovery |
| [Sonarr](https://github.com/Sonarr/Sonarr), [Radarr](https://github.com/Radarr/Radarr), [Prowlarr](https://github.com/Prowlarr/Prowlarr) | Series, movies, indexers |
| [Bazarr](https://github.com/morpheus65535/bazarr) | Subtitles |
| [qBittorrent](https://github.com/qbittorrent/qBittorrent) | Torrents |
| [gluetun](https://github.com/qdm12/gluetun) by qdm12 | The VPN container |
| [Tailscale](https://github.com/tailscale/tailscale) | Remote access |
| [LinuxServer.io](https://www.linuxserver.io/) | The qBittorrent, Sonarr, Radarr, Prowlarr and Bazarr images |
| [ImageMagick](https://imagemagick.org/), [jq](https://jqlang.org/), [FFmpeg](https://ffmpeg.org/) | Artwork shrinking, JSON in the scripts, the Live TV stream fix |

Each project keeps its own license. This repo doesn't include their code: the containers
come from their own images, and the plugins install from their authors' repositories.
`seerr-patch.sh` and `dispatcharr-patch.sh` change a few lines of Seerr and Dispatcharr
when their containers start, on your machine only. This repo's own scripts are under the
[MIT license](LICENSE).

**Use it legally.** This stack doesn't provide any content. Use an IPTV provider that
has the rights to what it streams, and download only what you're allowed to; a VPN
doesn't change what's legal where you live.
