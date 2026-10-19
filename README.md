# Voydar Beacon SD-card image

A ready-to-flash Raspberry Pi image for Voydar Beacon receivers. The contributor flashes it,
optionally types their home Wi-Fi into `voydar-wifi.txt` on the card, boots the Pi, opens a local
setup page, pastes their station key, picks **AIS**, **ADS-B** or **both**, optionally keeps
feeding **AISHub, MarineTraffic, VesselFinder** or their own server ("Also feed"), and the Pi
installs itself with the Beacon installer that is embedded in the image.

Current image version: **1.2.0** (installer v7: Voydar names on the device, in-place migration of
older installs). 1.1.0 (installer v6) added extra AIS outputs and Wi-Fi before first boot; 1.0.0
(installer v5) passed its first boot test on our Pi Zero 2 W on 2026-10-17.

- Base: **Raspberry Pi OS Lite, Debian trixie, arm64** (Pi 3B/3B+/4/5/400/Zero 2 W), built with
  [pi-gen](https://github.com/RPi-Distro/pi-gen) pinned to tag `2026-09-15-raspios-trixie-arm64`
  = commit `2c235fa703cacb65e0fe0b2ab2fcb23d44dfd268` (checked in `build.sh` on every build).
- Adds: Docker (Debian's `docker.io`, 26.x), the first-boot setup page, the Beacon **installer v7**
  and **ADS-B feeder v3** (downloaded **at build time** from www.voydar.com and checked against
  the sha256 values pinned in `build.sh`), and the two receiver container images pinned by
  digest (`images.lock`), preloaded so first boot does not have to download them.
- Hosted only in our own storage (see `publish.md`). Nothing here uploads anything.

**Names.** Everything a contributor sees or types says Voydar (image 1.2.0, installer v7): hostname
`voydar-beacon`, hotspot `Voydar-Beacon-XXXX`, page copy, boot-partition files `voydar-wifi.txt` /
`voydar-hotspot.txt` / `voydar-reset`, the image file `voydar-beacon-<version>.img.xz`, the
receiver host `receiver.voydar.com`, the Basic-auth user `voydar`, `/etc/voydar`, `/opt/voydar`,
`/var/lib/voydar`, containers `voydar-beacon` / `voydar-adsb`, the `voydar-adsb-feed` service, the
`voydar-outputs` helper, the `voydar-*` systemd units and the local `voydar` account.

Devices flashed with image 1.0.0 / 1.1.0 (seadar names: `/etc/seadar`, `seadar-*` containers,
units and helper, local user `seadar`) keep working unchanged: the server keeps accepting
`receiver.seadar.io` and the `seadar:<key>` user forever. Rerunning installer v7 on such a device
moves it to the Voydar names in place (key and extra outputs carried over, `/etc/seadar` left as a
symlink to `/etc/voydar`, `seadar-outputs` / `seadar-adsb-feed` left as two-line wrapper scripts,
not symlinks, so an older installer writing to those paths cannot overwrite the new ones). The
image's own systemd units and the local account are only renamed by reflashing with 1.2.0. On a
1.0/1.1 device migrated by hand, change settings with `sudo voydar-outputs` or by rerunning
installer v7, not with the old setup page's reset: that page still runs its embedded v5/v6
installer, which would start a second `seadar-beacon` container next to `voydar-beacon`.
The migration itself is tested by `sh test-installer-migration.sh INSTALL_V6.sh INSTALL_V7.sh`
(real v6 and v7 scripts as root in debian:trixie-slim, fake docker/systemd, two fake dongles).

### Why trixie and not bookworm

The ADS-B image (`sdr-enthusiasts/docker-adsb-ultrafeeder`, build 965) is published with
**zstd-compressed layers only**. Docker added zstd support in 23.0. Bookworm's `docker.io` is
20.10.24, which cannot pull or load that image (installers v5 to v7 offer to replace it with Docker's
own packages; the image avoids that). Trixie's is 26.1.5, which can, and trixie is the current
Raspberry Pi OS release. The pinned AIS-catcher image uses gzip and works on both.

## Layout

| Path | What |
|---|---|
| `build.sh` | The only entry point. Verifies every input, stages pi-gen, runs `build-docker.sh`, writes `deploy/voydar-beacon-<version>.img.xz` + `.sha256` + `.buildinfo.txt` |
| `config.template` | pi-gen settings (hostname `voydar-beacon`, UTC, `en_US.UTF-8`, SSH off, cloud-init off, user `voydar` with no usable password) |
| `images.lock` | Container images, pinned by index digest (must equal `AIS_IMAGE` / `ADSB_IMAGE` in the installer; `build.sh` checks) |
| `fetch-image.py` | Stdlib-only registry client: fetches the linux/arm64 image for a pinned digest, checks every manifest and blob hash, writes a deterministic `docker load` archive |
| `stage-voydar/` | The custom pi-gen stage (runs after `stage2`, the Lite stage) |
| `stage-voydar/00-voydar/files/voydar-setup.py` | First-boot setup page (Python 3 stdlib, `http.server`); with `--import-wifi`, the boot-partition Wi-Fi import |
| `stage-voydar/00-voydar/files/voydar-wifi.txt` | The blank Wi-Fi file shipped on the boot partition (and the template the import writes back) |
| `stage-voydar/00-voydar/files/voydar-wifi-import.service` | Runs the Wi-Fi import on every boot, before the setup page |
| `stage-voydar/00-voydar/files/voydar-preload.py` | First-boot loader for the shipped container images |
| `stage-voydar/00-voydar/files/voydar-lowmem-swap.sh` | 512 MB disk swap safety net on boards with less than 1 GiB RAM (Pi Zero 2 W) |
| `test-setup.sh` | Dry-run smoke test of the setup page (no system changes) |
| `publish.md` | How a built image is published (manual) |

## Building

### What `build.sh` verifies before anything is built

1. `install-v7.sh` and `adsb-feed-v3.sh` match their pinned sha256 (the values
   `apps/web/src/lib/beacon/setup.ts` computes and www.voydar.com/contribute prints), start with
   `#!/bin/sh` and pass `sh -n`.
2. The installer accepts every flag the setup page passes (`--private --ais --adsb --both
   --adsb-serial --ais-serial --lat --lon --yes`), reads the env it sets (`VOYDAR_IMAGE`,
   `VOYDAR_FEEDER_FILE`, `VOYDAR_LAT`, `VOYDAR_LON`), uses `/etc/voydar/outputs.conf` (which the
   page writes) and embeds a `voydar-outputs` helper that passes `sh -n` and reads that file; the
   stage has `voydar-wifi.txt` and `voydar-wifi-import.service`.
3. The feeder embedded in the installer is byte-identical to the feeder file shipped on the card.
4. `images.lock` pins exactly the `AIS_IMAGE` and `ADSB_IMAGE` references the installer runs, so
   the preloaded images are the ones it uses.
5. Each container image is fetched by digest for linux/arm64 with every manifest and layer hash
   checked; pi-gen is at the pinned commit and the tag points at it.

`PREPARE_ONLY=1` stops after these checks (works on any OS, needs about 150 MB in
`stage-voydar/00-voydar/files/build/`, gitignored).

### Full build on an Apple Silicon Mac (one command)

pi-gen's `build-docker.sh` runs natively on arm64 (no qemu): Docker Desktop's VM is aarch64, so
pi-gen skips binfmt and builds arm64 directly. Needs Docker Desktop running, git, python3, curl.

```sh
cd ~/dev/voydar && VERSION=1.2.0 WORK_DIR=/tmp/beacon-work OUT_DIR=$HOME/beacon-deploy ./scripts/beacon-image/build.sh
```

- **Disk**: about **20 GB free** for pi-gen's work tree, which lives inside Docker Desktop's disk
  image (it grows on the Mac's disk), plus about 1 GB on the Mac for `pi-gen` image, inputs and
  the finished `.img.xz` (~0.7 GB). `build.sh` refuses to start with less than 20 GB free on the
  `WORK_DIR` volume (`MIN_FREE_GB`). Also make sure Docker Desktop's disk limit (Settings >
  Resources) has 20 GB headroom: `docker system df` shows what it holds; `docker builder prune`
  frees build cache.
- **Memory**: give Docker Desktop at least 4 GB (xz compression uses up to half of it).
- **Time**: 30 to 60 minutes on an M-series Mac (it downloads ~1 GB of Debian packages).
- A failed run leaves the container `voydar_beacon_pigen`; remove it with
  `docker rm -v voydar_beacon_pigen` before trying again (`build.sh` says so). Afterwards
  `docker rmi pi-gen` reclaims the build image.
- The build config (with a random, discarded `FIRST_USER_PASS`) is copied into the local
  `pi-gen` Docker image: pi-gen's `-c` handling uses GNU sed syntax that macOS ignores, so
  `build.sh` writes pi-gen's default `config` file instead. The account is locked in
  `stage-voydar`, so the value never works on a device.

### Linux

arm64 Linux: same command. x86_64 Linux: install `qemu-user-static` and `binfmt-support` first
(pi-gen runs arm64 binaries under emulation; slower). Intel Macs are not supported.

Other switches (all optional): `INSTALLER_SHA256` / `FEEDER_SHA256` with `INSTALLER_URL` /
`FEEDER_URL` (a newer published installer; still hash-checked), `INSTALLER_FILE` / `FEEDER_FILE`
(local copies, still hash-checked), `SKIP_PRELOAD=1` (do not ship the container images; about
108 MB smaller, first install downloads them by digest), `IMG_DATE`, `WORK_DIR`, `OUT_DIR`,
`MIN_FREE_GB`, `PREPARE_ONLY=1`.

Output in `OUT_DIR` (default `scripts/beacon-image/deploy/`, gitignored):

```
voydar-beacon-1.2.0.img.xz
voydar-beacon-1.2.0.img.xz.sha256
voydar-beacon-1.2.0.buildinfo.txt    # every pinned input and hash, also at /opt/voydar/BUILDINFO on the card
voydar-beacon-1.2.0.info             # pi-gen's package list (dpkg -l)
voydar-beacon-1.2.0.bmap / .build.log
```

### What "reproducible" means here

Every input is pinned and verified: the pi-gen commit, the installer and feeder (sha256), the
container images (digest, down to each layer), and the stage in this directory. The
container-image archives are byte-for-byte reproducible. The finished `.img.xz` is **not**
bit-for-bit reproducible, because pi-gen installs Debian and Raspberry Pi packages from the live
archives at build time (the latest security updates of the day) and the filesystem carries
timestamps. Two builds of the same commit give the same software set at the package versions
current on their build day; `buildinfo.txt` records the inputs, and the `.sha256` identifies the
exact artefact that was published.

## Verifying a download

```sh
sha256sum -c voydar-beacon-1.2.0.img.xz.sha256    # Linux
shasum -a 256 -c voydar-beacon-1.2.0.img.xz.sha256 # macOS
```

The hash must also match the one printed on www.voydar.com/contribute (the page is the trust
anchor; the `.sha256` file sits next to the image and only guards against a broken download).
Flash with Raspberry Pi Imager ("Use custom") or `xz -dc … | sudo dd of=/dev/sdX bs=4M
conv=fsync`. Do not use Imager's "OS customisation": cloud-init is off in this image and the
setup page does that job. To set the Wi-Fi before first boot, edit `voydar-wifi.txt` on the card
instead (next section).

## Wi-Fi before first boot (`voydar-wifi.txt`)

After flashing, the card's boot partition (the one any computer can open, `bootfs`) holds
`voydar-wifi.txt`. Filling it in is optional and is the easiest path for a headless Pi Zero 2 W:
the Beacon joins the home Wi-Fi by itself and the contributor never needs the hotspot or the card
again. The file is plain `name=value` lines (`#` starts a comment; Windows line endings, a UTF-8
BOM, spaces around `=` and double quotes around a value are all fine):

```
ssid=Home Net
password=correct horse battery
country=GB
hotspot_password=
```

| Name | Value |
|---|---|
| `ssid` | Wi-Fi name, up to 32 bytes |
| `password` | 8 to 63 printable ASCII characters; empty for an open network |
| `country` | 2-letter code; required with `ssid` (sets the legal Wi-Fi channels) |
| `hotspot_password` | optional, 8 to 63 printable ASCII: the setup hotspot's WPA2 password instead of a random one |

`voydar-wifi-import.service` runs `voydar-setup.py --import-wifi` on **every** boot, before the
setup page. If the file has values and they are valid it:

1. stores `hotspot_password` (if any) in `/etc/voydar/hotspot.psk` (root, 0600);
2. overwrites the file's bytes in place and writes the blank template back, so no password stays
   on the card (best effort: FAT has no secure delete; the old clusters are overwritten first);
3. saves the Wi-Fi as the NetworkManager keyfile the setup page uses (`voydar-wifi`, root 0600),
   sets the Wi-Fi country, and waits up to 2 minutes for NetworkManager to connect;
4. records the outcome (no secrets) in `/run/voydar-wifi-import.json`; the setup page shows it,
   and so does the hotspot label if it failed.

A file holding only the blank template is left alone. A file with mistakes (unknown name, short
password, no country) is not used and not changed; the reasons appear on the setup page, the HDMI
console and `voydar-hotspot.txt`, without quoting any value. Because it runs on every boot, a
configured Beacon can be moved to a new Wi-Fi the same way: edit the file on the card, boot. The
new Wi-Fi replaces the saved one, and a configured Beacon has no hotspot to fall back to, so when
unsure also create `voydar-reset` (the setup page and its hotspot then return if the join fails).

**Why not a documented default hotspot password.** A shared, published password would let anyone
within Wi-Fi range join the hotspot during the setup window and submit the form first (with their
own station key and Wi-Fi). The hotspot stays WPA2 with a per-device random password unless the
contributor chooses one in `voydar-wifi.txt`; the recommended path skips the hotspot entirely. The
setup page's CSRF, Host and client-address guards are unchanged.

## First boot

1. `voydar-wifi-import.service`: applies `voydar-wifi.txt` if it has values (previous section).
2. `voydar-lowmem-swap.service`: on boards with less than 1 GiB RAM (Pi Zero 2 W, 3A+) it adds
   a 512 MB swap file (`/var/lib/voydar/swapfile`, only if the card has 2 GB free) at a lower
   priority than Raspberry Pi OS's own zram swap (`rpi-swap`), so `docker load` and ultrafeeder
   cannot run the board out of memory. Larger boards: nothing changes.
3. `voydar-preload.service` loads the shipped container images into Docker, checks each image's
   layer digests against the pin, and records the pinned `repo@sha256:…` reference so the
   installer finds the image locally (Docker's `load` does not keep digests). Allowed 45
   minutes; on a Zero 2 W expect several minutes.
4. `voydar-setup.service` (only while `/etc/voydar/configured` does not exist):
   - **With a network** (Ethernet, Wi-Fi from `voydar-wifi.txt`, or Wi-Fi known from before a reset) it serves the setup page
     on the LAN at `http://voydar-beacon.local/` (avahi; a second Beacon on the same LAN becomes
     `voydar-beacon-2.local`) and prints the IP on the HDMI console.
   - **Without one** (always the case on a fresh Pi Zero 2 W, which has no Ethernet) it starts a
     WPA2 hotspot `Voydar-Beacon-XXXX` (last 4 hex digits of the Wi-Fi MAC) on 2.4 GHz channel 6,
     which is legal in every regulatory domain. The password is `hotspot_password` from
     `voydar-wifi.txt` if the contributor set one, otherwise generated on first boot; it is kept in
     `/etc/voydar/hotspot.psk`, written to `/boot/firmware/voydar-hotspot.txt` (readable by
     putting the card in any computer) and shown on the HDMI console. Every DNS name resolves to
     the Beacon on the hotspot, so phones usually open the page by themselves; otherwise browse
     to `http://10.42.0.1`.
5. The page asks for: station key (`^sbk_[0-9a-f]{48}$`), private-feed switch (AIS only), Wi-Fi
   name, password and country, mode (AIS / ADS-B / both) with the detected RTL-SDR serials, AIS
   gain, optional antenna latitude/longitude, optional extra AIS outputs ("Also feed", below),
   and optional SSH with a password of at least 12 characters. It refuses what installer v7 would
   refuse later: private + ADS-B, and "both" without two distinguishable dongles chosen by serial;
   and extra outputs with ADS-B only.
6. On submit it saves the Wi-Fi as a NetworkManager keyfile (root, 0600), sets the Wi-Fi
   country, joins the network (the hotspot goes away; if joining fails the hotspot comes back so
   the contributor can retry), optionally sets the `voydar` password and enables SSH, writes
   `/etc/voydar/beacon.env` (root, 0600, `VOYDAR_KEY=…`, the file the installer and feeder use)
   `/etc/voydar/setup.env` (the form's other choices, no secrets) and `/etc/voydar/outputs.conf`
   (root, 0600, the extra outputs; written every time, so it is exactly what the form chose), and runs
   `sh /opt/voydar/voydar-install.sh [--private] --ais|--adsb|--both [--adsb-serial S] [--ais-serial S] [--lat LAT --lon LON] --yes <key> [gain]`
   with `VOYDAR_IMAGE=1 VOYDAR_FEEDER_FILE=/opt/voydar/adsb-feed-v3.sh [VOYDAR_LAT VOYDAR_LON]`.
   Progress shows at `http://voydar-beacon.local/status`.
7. Installer v7 saves the AIS settings in `/etc/voydar/ais.env` (root, 0600), installs
   `/usr/local/bin/voydar-outputs` and starts AIS-catcher through it: Voydar over HTTPS plus
   `-u HOST PORT` / `-P HOST PORT` / `-H URL` for each line of `outputs.conf`.
8. On success it writes `/etc/voydar/configured`, deletes the hotspot, keeps showing the result
   for 10 minutes and exits. The unit never starts again.

**Reset:** put the card in a computer and create an empty file named `voydar-reset` on the boot
partition (`/boot/firmware/voydar-reset` on the Pi). On the next boot `voydar-reset.service`
removes the marker and the setup page runs again. Saved Wi-Fi and the running receivers stay
until the form is submitted again. The form offers the extra outputs already on the Beacon
("Keep the outputs already on this Beacon", ticked by default). For a new Wi-Fi, fill in
`voydar-wifi.txt` at the same time (it works without a reset too).

## Also feed other networks (extra AIS outputs)

Contributors who already feed AISHub, MarineTraffic or VesselFinder keep doing so from the same
receiver. On the setup page, "Also feed" has a row each for AISHub, MarineTraffic and VesselFinder
(host + UDP port from that network's sign-up email or station page) and two custom rows (UDP,
TCP or HTTP). The list lives in **`/etc/voydar/outputs.conf`** (root, 0600), one output per line:

```
# comments and blank lines are ignored; text after # on a line is a label
udp data.aishub.net 2948  # AISHub
udp 5.9.207.224 10110  # MarineTraffic
tcp 192.168.1.20 10110
http https://example.org/ais?station=7
```

| Line | AIS-catcher argument |
|---|---|
| `udp HOST PORT` | `-u HOST PORT` (NMEA over UDP; what AISHub, MarineTraffic and VesselFinder take) |
| `tcp HOST PORT` | `-P HOST PORT` (NMEA over a TCP connection) |
| `http URL` | `-H URL` (AIS-catcher's HTTP JSON output) |

`HOST` is a DNS name or IPv4 address, `PORT` 1 to 65535 without leading zeros, `URL` http(s)://
with only letters, digits and `. _ ~ % / ? = & + : , -`; at most 8 outputs. The setup page,
the installer's `--output` and `voydar-outputs` all apply these rules; an invalid line in the file
is skipped with a warning. Values reach Docker only as separate arguments (no `eval`, no shell
built from the file), so a crafted line cannot run anything.

After setup, without reflashing (needs SSH, or a keyboard and screen):

```sh
sudo voydar-outputs list
sudo voydar-outputs add udp data.aishub.net 2948      # recreates voydar-beacon with it (a few seconds without data)
sudo voydar-outputs add tcp:192.168.1.20:10110        # short forms work too
sudo voydar-outputs remove udp data.aishub.net 2948
sudo voydar-outputs --no-apply add udp HOST PORT      # only edit the list
sudo voydar-outputs apply                             # recreate the container from the list
```

Rerunning installer v7 (or resubmitting the setup page) keeps every output: the installer always
starts AIS-catcher from the file. Non-image users get the same with
`sudo sh voydar-install-v7.sh --output udp:HOST:PORT [--output ...] <station key>`; `--output`
values are added to the file. The extra outputs send what the receiver hears to those networks
under their own terms; Voydar only receives its own feed.

### Security of the setup page

- Listens on port 80 only while unconfigured; IPv4 only (never on a global IPv6 address).
- Answers only clients on private, link-local or CGNAT addresses; requests with a foreign
  `Host` header are redirected to the device's own address (DNS-rebinding guard, also how the
  captive-portal redirect works).
- One-time CSRF token per rendered form (expires after an hour), `Origin` check on POST,
  form-encoded bodies only, 16 KB cap.
- Strict headers: `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. Plain HTML,
  no scripts, no external assets.
- Secrets never go on a command line (Wi-Fi via keyfile, SSH password via `chpasswd` stdin),
  except the station key, which the installer takes as an argument by design, and the hotspot
  password, which `nmcli` takes as an argument (local only; the image has no other users).
- Extra outputs are validated against strict patterns on the page and again by `voydar-outputs`;
  hosts and ports are not shown on the status page (only the network names).
- `voydar-wifi.txt` is blanked as soon as it has been read; import errors never quote a value. The key, the
  Wi-Fi password and the SSH password are redacted from every log line, the journal and the
  status page (`/var/log/voydar-setup.log` is 0600).
- No default login: `root` and `voydar` have no usable password and SSH is off until the
  contributor turns it on with their own password. Each Pi generates its own SSH host keys on
  first boot.

## Testing

```sh
sh scripts/beacon-image/test-setup.sh     # dry-run: form, CSRF, Host guard, validation (incl. output injection attempts), exact installer argv, fake install, redaction, env files, outputs.conf, voydar-wifi.txt import
python3 scripts/beacon-image/stage-voydar/00-voydar/files/voydar-setup.py --dry-run --port 8080   # then open http://127.0.0.1:8080
python3 scripts/beacon-image/fetch-image.py --inspect --ref "$(awk '/ultrafeeder/{print $2}' scripts/beacon-image/images.lock)"
PREPARE_ONLY=1 WORK_DIR=/tmp/beacon-work ./scripts/beacon-image/build.sh   # every input + installer/image agreement
```

`--dry-run` pretends to be a Pi with two dongles and no network, and writes only to
`--state-dir` (`--dry-run --import-wifi --state-dir D` reads `D/voydar-wifi.txt`). The
`voydar-outputs` helper and installer v7 are tested in `apps/web/src/lib/beacon/scripts.test.ts`
(the shipped sh, run for real against a temporary outputs file).

## Installer v7 contract this image relies on

| Item | Where in install-v7.sh |
|---|---|
| `VOYDAR_IMAGE=1` | image mode: implies `--yes`, and copies the feeder from `VOYDAR_FEEDER_FILE` instead of writing its embedded copy |
| `VOYDAR_FEEDER_FILE=/opt/voydar/adsb-feed-v3.sh` | the image never downloads code at first boot |
| `VOYDAR_LAT`, `VOYDAR_LON` (and `--lat/--lon`) | antenna position for readsb (ADS-B only) |
| `/etc/voydar/beacon.env` `VOYDAR_KEY=` | the feeder's key file; the installer rewrites it for ADS-B |
| `docker run … repo:tag@sha256:…` with default `--pull missing` | uses the preloaded images (resolved through the `repo@sha256` reference the preload records) |
| Docker ≥ 23 for ADS-B | trixie's `docker.io` 26.x passes, so v7 never replaces Docker on the image |
| `/etc/voydar/outputs.conf` | `OUTPUTS_FILE`; the embedded `voydar-outputs` reads it every time it (re)creates `voydar-beacon` |
| `/etc/voydar/ais.env` | written by v7 (image, serial, gain, private) so `voydar-outputs apply` recreates the same container |

A newer installer must keep these, or the image needs a matching update (`build.sh` checks the
flags, env names, embedded feeder and image pins, and stops if they drift).

## Licences of what the image contains

| Component | Licence | Source |
|---|---|---|
| Raspberry Pi OS Lite (Debian trixie packages, Raspberry Pi kernel and tools) | Per package: mostly GPL-2.0/GPL-3.0/LGPL/BSD/MIT; Raspberry Pi firmware blobs under Raspberry Pi's redistributable firmware licence. Per-package terms in `/usr/share/doc/*/copyright` on the card | deb.debian.org, archive.raspberrypi.com (source packages alongside) |
| pi-gen (the build tool, not shipped on the card) | BSD-3-Clause | github.com/RPi-Distro/pi-gen @ `2c235fa7` |
| Docker (`docker.io`, `containerd`, `runc` from Debian) | Apache-2.0 | Debian source package `docker.io` |
| AIS-catcher (`ghcr.io/jvde-github/ais-catcher:v0.70`) | GPL-3.0 | github.com/jvde-github/AIS-catcher (tag v0.70) |
| docker-adsb-ultrafeeder (`latest-build-965`) | GPL-3.0; bundles readsb (GPL-3.0-or-later) and other GPL/MIT tools | github.com/sdr-enthusiasts/docker-adsb-ultrafeeder, github.com/wiedehopf/readsb |
| Voydar setup page, preload, swap helper, build scripts (this directory) | The Schoeller Group LLC | this repository |
| Voydar Beacon installer v7 (with `voydar-outputs`) and ADS-B feeder v3 | The Schoeller Group LLC | www.voydar.com/api/beacon/ |

Distributing an image that contains GPL binaries means we must make the corresponding source
available. Each release must carry, next to the image, the source locations for the exact
versions (pinned tags above, Debian source packages for the build date) or a mirrored copy; see
`publish.md`. The image is "based on Raspberry Pi OS" and must not be called Raspberry Pi OS
(Raspberry Pi trademark rules).

## Boot test on a Raspberry Pi Zero 2 W (our station: one Nooelec RTL-SDR)

Hardware: Pi Zero 2 W, 5 V 2.5 A supply, micro-USB OTG adapter for the dongle (the Zero has one
data port; "both" needs a powered hub), 16 GB+ card, the Nooelec on the AIS or 1090 MHz antenna.
Optional: mini-HDMI to a screen to see the hotspot password and console messages.

**Run A: hotspot path (as 1.0.0), with a chosen hotspot password and extra outputs.**

1. Flash `voydar-beacon-<version>.img.xz`; check the `.sha256` first. Before ejecting, confirm
   the boot partition has `voydar-wifi.txt` (blank template) and no `voydar-hotspot.txt` yet. In
   `voydar-wifi.txt` set only `hotspot_password=` to a value of your choice.
2. Boot with the dongle plugged in. Within ~3 minutes: hotspot `Voydar-Beacon-XXXX` appears and
   your chosen password works. Later on a computer: `voydar-wifi.txt` is back to the blank
   template and `voydar-hotspot.txt` shows your password.
3. Join it from a phone: the captive portal opens the page (else `http://10.42.0.1`). The page
   lists the Nooelec and its serial and has the "Also feed" section.
4. Submit station key, Wi-Fi, country, mode AIS, SSH on, and under "Also feed" one real output if
   the station has one (AISHub / MarineTraffic / VesselFinder host + port), else a custom UDP
   output to a computer on the LAN (`nc -ul 10110` there shows NMEA lines). The hotspot goes
   away; on the home Wi-Fi `http://voydar-beacon.local/status` shows progress and "Also feeding
   1 other output".
5. Via SSH (`ssh voydar@voydar-beacon.local`), during and after setup:
   - `free -m; swapon --show`: zram plus `/var/lib/voydar/swapfile` (priority 0).
   - `journalctl -u voydar-wifi-import -u voydar-lowmem-swap -u voydar-preload -b`: import says the
     hotspot password was taken and the file blanked; both images loaded, "ready (no download
     needed)" for both references.
   - `sudo cat /var/log/voydar-setup.log`: installer v7 ran without "Pulling" lines, exit 0; no
     key, Wi-Fi, SSH or hotspot password visible.
   - `sudo ls -l /etc/voydar`: `outputs.conf`, `ais.env`, `beacon.env`, `hotspot.psk` all `-rw-------`.
   - `sudo voydar-outputs list` shows the output; `docker inspect voydar-beacon --format '{{.Args}}'`
     ends with `-u HOST PORT`; the other network (or `nc`) receives NMEA.
   - `sudo voydar-outputs add udp <LAN computer IP> 10111` recreates the container (a few seconds)
     and `nc -ul 10111` receives too; `sudo voydar-outputs remove udp <IP> 10111` takes it out.
   - Rerun the installer by hand: `sudo VOYDAR_IMAGE=1 VOYDAR_FEEDER_FILE=/opt/voydar/adsb-feed-v3.sh sh /opt/voydar/voydar-install.sh --ais --yes "$(sudo sed -n 's/^VOYDAR_KEY=//p' /etc/voydar/beacon.env)"`;
     the extra output is still in `docker inspect` afterwards (the 1.0.0 gap).
   - `dmesg | grep -i -E 'oom|killed'`: nothing.
6. The station page on www.voydar.com turns green when the first message arrives.
7. Reboot: the setup page does not return, the containers come back with the extra output, the
   swap file is on again.
8. Failure path once (on a fresh flash or after a reset): submit a wrong Wi-Fi
   password; the hotspot comes back within about two minutes and the form works again.

**Run B: Wi-Fi before first boot (the headless path).**

1. Flash again. In `voydar-wifi.txt` on the card set `ssid=`, `password=`, `country=` (edit it
   on Windows or macOS once each if possible: line endings and BOM).
2. Boot. No hotspot appears; within ~3 minutes `http://voydar-beacon.local/` opens from a phone
   on the home Wi-Fi and shows "Joined the Wi-Fi … from voydar-wifi.txt" above the form.
3. Put the card in a computer later: `voydar-wifi.txt` is the blank template again (no password).
4. Submit with Wi-Fi left empty; setup completes as in run A.
5. Negative case: fill `voydar-wifi.txt` with a wrong password and `voydar-reset`, boot. The
   hotspot comes up with the "could not connect" note on the page and in `voydar-hotspot.txt`.
6. Reset path: create an empty `voydar-reset`, boot, the page runs on the LAN with "Keep the
   outputs already on this Beacon" ticked; resubmit keeps them.

Record the result (board, image version, mode, timings, peak memory) in this section and in
`publish.md` before any release.

## Not yet verified

Verified:
- **Image 1.0.0** (installer v5, built from main @ `46447b49f`) booted on our Pi Zero 2 W on
  2026-10-17: hotspot, setup page, Wi-Fi join, installer v5 and AIS feeding all worked. Two
  plug-and-play gaps found there are what 1.1.0 fixes: the setup page could not keep other AIS
  feeds (and rerunning the installer dropped any added by hand), and a headless Zero user had to
  pull the card to read the random hotspot password.
- For 1.1.0, on 2026-10-17 (macOS, arm64): `test-setup.sh` passes (under bash and dash),
  including the "Also feed" validation with injection attempts, `outputs.conf` contents and mode,
  and the `voydar-wifi.txt` import (Windows-style file, blanking, bad-file handling, hotspot
  password, no secret in any log). The web unit tests run the shipped `seadar-outputs` helper and
  `sh -n` installer v6. Installer v6 ran end to end in a `debian:trixie-slim` container (dash) with
  a fake `docker`: `--output` refusals happen before any change, the outputs file and `ais.env`
  are written root-only, the `docker run` argv is exactly v5's plus `-u` / `-P` / `-H` per output,
  a rerun without `--output` keeps them and `seadar-outputs add` recreates the container with the
  new list. `PREPARE_ONLY=1` passes with the local v6 file (`INSTALLER_FILE`).
- **Image 1.1.0 built** on 2026-10-17 from main @ `a0b6b8cac` (sha256 `ec4ad49f…7592e55`, embeds
  installer v6 at the live hash). On hardware (station one, Pi Zero 2 W running 1.0.0, upgraded in
  place over SSH rather than reflashed): installer v6 with `--output udp:<LAN host>:10110` started
  "Voydar plus 1 extra output" and the LAN host received NMEA; a rerun **without** `--output` kept
  the output (`outputs.conf` and the container's `-u`), and the host kept receiving;
  `seadar-outputs remove` dropped it. The 1.1.0 `seadar-setup.py` + `seadar-wifi-import.service`
  installed on the Pi, with `voydar-wifi.txt` (ssid/password/country) on `/boot/firmware`, then a
  reboot: the file was read and blanked in place on the FAT partition, Wi-Fi joined in 13 s, the
  service exited 0, AIS feeding resumed.

Not yet verified:
- **A fresh first boot of the 1.1.0 image** (card flashed with 1.1.0, no prior state): the import
  racing NetworkManager on a never-configured system, `raspi-config` country before the first
  connection, the hotspot with a `hotspot_password` from the file. The pieces were verified on
  hardware individually (above), not as a first boot.
- Real delivery to AISHub / MarineTraffic / VesselFinder (needs their host/port).
- `www.voydar.com/api/beacon/install-v6.sh` serves the pinned hash (checked 2026-10-17).
- **Image 1.2.0** (installer v7, Voydar names) is not built yet: build it once install-v7.sh is
  live, then run both boot-test runs above, plus the migration path: on a 1.1.0 device, rerun
  installer v7 by hand and confirm `/etc/seadar` is a symlink, `docker ps` shows only `voydar-*`
  containers, `voydar-outputs list` shows the old outputs and the station keeps feeding.
