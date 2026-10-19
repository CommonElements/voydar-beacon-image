# Publishing a Voydar Beacon image

Publishing is manual and done by the founder (or with the founder's go-ahead). No script or
workflow in this repo uploads an image. Images are hosted only in our own storage: our own
GitHub release assets or our own Supabase Storage bucket, never a third-party image store.

## Before publishing

1. Build from a clean checkout of `origin/main` (see `README.md`), after the web deploy that
   serves the pinned installer (`curl -fsSL https://www.voydar.com/api/beacon/install-v7.sh | shasum -a 256`
   prints the `INSTALLER_SHA256` in `build.sh`):
   `VERSION=1.2.0 ./scripts/beacon-image/build.sh` (installer v7 and feeder v3 hashes are pinned in `build.sh`)
2. Flash it and boot it on at least one Pi 4 or 5 (and ideally a 3B+ / Zero 2 W): hotspot
   appears, phone reaches the page, the form joins Wi-Fi, the installer runs, the station page on
   voydar.com turns green, and the page is gone after a reboot. Test the `voydar-reset` path once,
   the `voydar-wifi.txt` path (no hotspot) once, and one extra output ("Also feed" and
   `voydar-outputs add`). Use both runs of the "Boot test on a Raspberry Pi Zero 2 W" checklist in
   `README.md`, and the migration check there (installer v7 rerun on a 1.1.0 device).
3. Confirm `deploy/voydar-beacon-1.2.0.buildinfo.txt` lists the installer and feeder hashes shown
   on www.voydar.com/contribute.
4. On /contribute, next to the download (web lane): "Before first boot, open `voydar-wifi.txt` on
   the card and type your Wi-Fi name, password and country; the Beacon then joins your Wi-Fi by
   itself and you open http://voydar-beacon.local/". There is deliberately no default hotspot
   password to print (README, "Wi-Fi before first boot").
5. Prepare the GPL source notice for the release (see "Source for GPL components").

## Option A: GitHub release in a TSG-owned public repository

Release assets on a **private** repository cannot be downloaded by the public, and this monorepo
is private. Use a public repository owned by The Schoeller Group (founder decision; rule 14),
for example one that contains only `scripts/beacon-image/`.

Write `deploy/release-notes.md` first: version, the image sha256, the pinned inputs (copy
`buildinfo.txt`), supported Pi models, and the GPL source locations below.

```sh
cd scripts/beacon-image/deploy
gh release create beacon-image-v1.2.0 \
  --repo <tsg-org>/<public-repo> \
  --title "Voydar Beacon image 1.2.0" \
  --notes-file release-notes.md \
  voydar-beacon-1.2.0.img.xz \
  voydar-beacon-1.2.0.img.xz.sha256 \
  voydar-beacon-1.2.0.buildinfo.txt
```

GitHub release assets are free to host and download for public repositories, with a 2 GiB
per-file limit (the compressed image is well under that).

## Option B: Supabase Storage (our project)

```sh
# Public bucket "beacon-images" (create once, in the dashboard, public read, no listing needed).
# Upload with the service key from the environment, never pasted into a shell history or a doc.
curl -fsS -X POST "$SUPABASE_URL/storage/v1/object/beacon-images/v1.2.0/voydar-beacon-1.2.0.img.xz" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" -H "Content-Type: application/octet-stream" \
  --data-binary @voydar-beacon-1.2.0.img.xz
# …and the same for .sha256 and .buildinfo.txt
```

Cost warning (rule 13): each download is roughly the size of the `.img.xz`. The free Supabase
plan's egress allowance is small, so a few thousand downloads a month can exceed it and bill or
throttle the project that also serves the live site. The Seadar Supabase project is also still
under the Common Elements organisation (migration pending). Prefer option A unless the founder
decides otherwise.

## After publishing

- Link the download and print its sha256 on www.voydar.com/contribute (web lane). The page is the
  trust anchor for the hash.
- Never replace an asset in place under the same version: publish a new version
  (`beacon-image-v1.3.0`, `voydar-beacon-1.3.0`) so a published hash never goes stale.

## Source for GPL components

The image contains GPL software (Linux, Debian packages, AIS-catcher, readsb in ultrafeeder). The
release notes must say where the corresponding source is, for the exact versions shipped:

- AIS-catcher v0.70: github.com/jvde-github/AIS-catcher (tag v0.70)
- docker-adsb-ultrafeeder build 965 and the components it bundles: github.com/sdr-enthusiasts/docker-adsb-ultrafeeder
- Debian and Raspberry Pi OS packages: source packages at deb.debian.org / archive.raspberrypi.com
  (`dpkg -l` on the card lists the exact versions)

Pointing to upstream is common practice, but GPL-3.0 section 6(d) makes us responsible for that
source staying available as long as we distribute the image. The safest option is to mirror the
matching source archives next to the image in the same release. That is a founder/legal call.
