#!/usr/bin/env bash
# Build the Voydar Beacon SD-card image (Raspberry Pi OS Lite, trixie, arm64) with pi-gen in Docker.
#
#   VERSION=1.2.0 ./scripts/beacon-image/build.sh
#
# Embeds installer v7 and ADS-B feeder v3, downloaded at build time and checked against the
# sha256 values pinned below (the same values www.voydar.com/contribute prints). Optional:
#   VERSION            image version (default 1.2.0) -> deploy/voydar-beacon-<VERSION>.img.xz
#   IMG_DATE           date stamped into the image (default: today, UTC)
#   INSTALLER_SHA256 / FEEDER_SHA256   override the pinned hashes (for a newer published version)
#   INSTALLER_URL / FEEDER_URL         override the download URLs (still sha256-checked)
#   INSTALLER_FILE / FEEDER_FILE       use a local copy instead of downloading (still sha256-checked)
#   SKIP_PRELOAD=1     do not ship the container images on the card (smaller image; first
#                      install downloads them instead, still by pinned digest)
#   WORK_DIR / OUT_DIR default scripts/beacon-image/work and scripts/beacon-image/deploy
#   PREPARE_ONLY=1     fetch + verify every input and stage pi-gen, but do not run the image build
#   MIN_FREE_GB        free disk required before a full build (default 20; 0 skips the check)
#
# Needs Docker, git, python3, curl and sha256sum (or shasum). Hosts:
#   * arm64 Linux, or an Apple Silicon Mac with Docker Desktop: pi-gen runs natively (no qemu).
#   * x86_64 Linux: arm64 binaries run under qemu-user-static via binfmt_misc (install
#     qemu-user-static and binfmt-support first). Intel Macs are not supported.
# Expect 30-90 minutes and ~20 GB of free disk (on a Mac: inside Docker Desktop's disk image).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UA='Voydar-Beacon-Image-Build/1.0 (+https://www.voydar.com/contribute)'

# ---- pinned inputs -------------------------------------------------------------------------
PIGEN_URL="https://github.com/RPi-Distro/pi-gen.git"
PIGEN_TAG="2026-09-15-raspios-trixie-arm64"
PIGEN_COMMIT="2c235fa703cacb65e0fe0b2ab2fcb23d44dfd268"
INSTALLER_NAME="install-v7.sh"
FEEDER_NAME="adsb-feed-v3.sh"
INSTALLER_URL="${INSTALLER_URL:-https://www.voydar.com/api/beacon/$INSTALLER_NAME}"
FEEDER_URL="${FEEDER_URL:-https://www.voydar.com/api/beacon/$FEEDER_NAME}"
# sha256 of the exact bytes served (apps/web/src/lib/beacon/setup.ts computes the same values).
INSTALLER_SHA256="${INSTALLER_SHA256:-a90ae7a33268473f1ca5b93a6a5754412f14c605debb1f5859baa6145c294fbc}"
FEEDER_SHA256="${FEEDER_SHA256:-6a6d210080805150dcdac9eb46df949cb81b55870f5dc0d1e4a27b84eb87308b}"
# --------------------------------------------------------------------------------------------

VERSION="${VERSION:-1.2.0}"
IMG_DATE="${IMG_DATE:-$(date -u +%Y-%m-%d)}"
WORK_DIR="${WORK_DIR:-$HERE/work}"
OUT_DIR="${OUT_DIR:-$HERE/deploy}"
MIN_FREE_GB="${MIN_FREE_GB:-20}"
STAGE_BUILD="$HERE/stage-voydar/00-voydar/files/build"
CONTAINER_NAME="voydar_beacon_pigen"

die() { printf '\nbuild.sh: ERROR: %s\n' "$1" >&2; exit 1; }
say() { printf '\n==> %s\n' "$1"; }
if command -v sha256sum >/dev/null; then sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null; then sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else die "sha256sum or shasum is required"; fi

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "VERSION must look like 1.2.0"
[[ "$INSTALLER_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "INSTALLER_SHA256 must be 64 lowercase hex characters"
[[ "$FEEDER_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "FEEDER_SHA256 must be 64 lowercase hex characters"
for tool in git python3 curl; do command -v "$tool" >/dev/null || die "$tool is required"; done
if [ "${PREPARE_ONLY:-0}" != "1" ]; then
  case "$(uname -s)/$(uname -m)" in
    Linux/*|Darwin/arm64) ;;
    *) die "the image build needs Linux or an Apple Silicon Mac with Docker Desktop. Use PREPARE_ONLY=1 elsewhere." ;;
  esac
  command -v docker >/dev/null || die "docker is required"
  docker info >/dev/null 2>&1 || die "docker is not running"
  if docker ps -a --filter "name=^${CONTAINER_NAME}\$" -q | grep -q .; then
    die "a previous build container exists; remove it first: docker rm -v $CONTAINER_NAME"
  fi
fi

mkdir -p "$WORK_DIR" "$OUT_DIR"
if [ "${PREPARE_ONLY:-0}" != "1" ] && [ "$MIN_FREE_GB" != "0" ]; then
  free_gb=$(( $(df -Pk "$WORK_DIR" | awk 'NR==2 {print $4}') / 1048576 ))
  [ "$free_gb" -ge "$MIN_FREE_GB" ] || die "only ${free_gb} GB free on $(df -P "$WORK_DIR" | awk 'NR==2 {print $6}'); the build needs about ${MIN_FREE_GB} GB (MIN_FREE_GB=0 skips this check)"
fi
rm -rf "$STAGE_BUILD"
mkdir -p "$STAGE_BUILD/images"

# ---- 1. installer + feeder: fetched at build time, never at first boot, and hash-checked ----
fetch_verified() { # name url local_override expected_sha dest
  local name="$1" url="$2" local_file="$3" want="$4" dest="$5" got
  if [ -n "$local_file" ]; then
    say "Using local $name: $local_file"
    cp "$local_file" "$dest"
  else
    say "Downloading $name from $url"
    curl -fsSL --proto '=https' --tlsv1.2 -A "$UA" -o "$dest" "$url" || die "could not download $url"
  fi
  got="$(sha256 "$dest")"
  [ "$got" = "$want" ] || die "$name sha256 mismatch: expected $want, got $got. Refusing to build."
  head -n1 "$dest" | grep -q '^#!/bin/sh' || die "$name does not start with #!/bin/sh"
  sh -n "$dest" || die "$name is not valid sh"
  echo "$name sha256 $got OK"
}
fetch_verified "$INSTALLER_NAME" "$INSTALLER_URL" "${INSTALLER_FILE:-}" "$INSTALLER_SHA256" "$STAGE_BUILD/voydar-install.sh"
fetch_verified "$FEEDER_NAME"    "$FEEDER_URL"    "${FEEDER_FILE:-}"    "$FEEDER_SHA256"    "$STAGE_BUILD/$FEEDER_NAME"

# ---- 1b. the installer and the image must agree ---------------------------------------------
say "Checking the installer against the image (flags, env contract, feeder copy, image pins)"
INST="$STAGE_BUILD/voydar-install.sh"
# Flags the setup page passes (voydar-setup.py install_job) and env it sets.
for flag in -- --private --ais --adsb --both --adsb-serial --ais-serial --lat --lon --yes; do
  grep -Eq "^[[:space:]]+$flag(\|[^)]*)?\)" "$INST" || die "installer does not accept $flag, which the setup page passes"
done
for var in VOYDAR_IMAGE VOYDAR_FEEDER_FILE VOYDAR_LAT VOYDAR_LON; do
  grep -q "\${$var" "$INST" || die "installer does not read $var, which the setup page sets"
done
# Extra AIS outputs: the setup page writes /etc/voydar/outputs.conf; the installer must embed the
# voydar-outputs helper that reads it and start AIS-catcher through it.
grep -qx 'OUTPUTS_FILE=/etc/voydar/outputs.conf' "$INST" || die "installer does not use /etc/voydar/outputs.conf, which the setup page writes"
grep -q '"$OUTPUTS_BIN" apply' "$INST" || die "installer does not start AIS-catcher through voydar-outputs"
HELPER="$WORK_DIR/embedded-outputs-helper.sh"
awk "/<<'VOYDAR_OUTPUTS_EOF'\$/ {on=1; next} /^VOYDAR_OUTPUTS_EOF\$/ {on=0} on" "$INST" > "$HELPER"
head -n1 "$HELPER" | grep -q '^#!/bin/sh' || die "could not find the voydar-outputs helper embedded in the installer"
sh -n "$HELPER" || die "the embedded voydar-outputs helper is not valid sh"
grep -q 'FILE="${VOYDAR_OUTPUTS_FILE:-${SEADAR_OUTPUTS_FILE:-/etc/voydar/outputs.conf}}"' "$HELPER" || die "voydar-outputs does not read /etc/voydar/outputs.conf"
rm -f "$HELPER"
# The setup page's own files must be in the stage.
for f in voydar-wifi.txt voydar-wifi-import.service voydar-setup.py voydar-setup.service voydar-preload.py \
  voydar-preload.service voydar-reset.service voydar-lowmem-swap.sh voydar-lowmem-swap.service \
  voydar-rtl-sdr.conf dnsmasq-voydar.conf; do
  [ -s "$HERE/stage-voydar/00-voydar/files/$f" ] || die "stage-voydar is missing files/$f"
done
# The feeder the installer would write itself must be byte-identical to the one we embed.
EMBEDDED="$WORK_DIR/embedded-feeder.sh"
awk "/<<'VOYDAR_FEED_EOF'\$/ {on=1; next} /^VOYDAR_FEED_EOF\$/ {on=0} on" "$INST" > "$EMBEDDED"
[ -s "$EMBEDDED" ] || die "could not find the feeder embedded in the installer"
[ "$(sha256 "$EMBEDDED")" = "$FEEDER_SHA256" ] || die "the feeder embedded in $INSTALLER_NAME is not $FEEDER_NAME ($FEEDER_SHA256)"
rm -f "$EMBEDDED"
echo "installer accepts the setup page's flags, env and outputs file; its embedded feeder is $FEEDER_NAME"
# images.lock must pin exactly the references the installer runs (so the preload is used).
for var in AIS_IMAGE ADSB_IMAGE; do
  ref="$(sed -n "s/^$var=\"\\(.*\\)\"\$/\\1/p" "$INST")"
  [ -n "$ref" ] || die "installer has no $var"
  awk -v r="$ref" '$2 == r {found=1} END {exit !found}' "$HERE/images.lock" \
    || die "images.lock does not pin $ref ($var in $INSTALLER_NAME)"
  echo "$var pinned in images.lock: $ref"
done

# ---- 2. container images, pinned by digest, linux/arm64 ------------------------------------
IMAGES_INFO=""
while read -r name ref; do
  case "$name" in ''|'#'*) continue ;; esac
  if [ "${SKIP_PRELOAD:-0}" = "1" ]; then
    say "Verifying pin for $name (not shipping it: SKIP_PRELOAD=1)"
    python3 "$HERE/fetch-image.py" --ref "$ref" --platform linux/arm64 --inspect >/dev/null
  else
    say "Fetching $name (linux/arm64) by digest"
    python3 "$HERE/fetch-image.py" --ref "$ref" --platform linux/arm64 --out "$STAGE_BUILD/images/$name.tar" >/dev/null
    echo "$name: $(du -h "$STAGE_BUILD/images/$name.tar" | cut -f1) $(sha256 "$STAGE_BUILD/images/$name.tar")"
  fi
  IMAGES_INFO="${IMAGES_INFO}image         $ref"$'\n'
done < "$HERE/images.lock"

# ---- 3. pi-gen at the pinned commit --------------------------------------------------------
PIGEN="$WORK_DIR/pi-gen"
if [ ! -d "$PIGEN/.git" ]; then
  say "Cloning pi-gen"
  git -c http.userAgent="$UA" clone --quiet --filter=blob:none "$PIGEN_URL" "$PIGEN"
fi
git -C "$PIGEN" -c http.userAgent="$UA" fetch --quiet --tags origin
git -C "$PIGEN" checkout --quiet --force "$PIGEN_COMMIT"
git -C "$PIGEN" clean -qfdx -e work -e deploy
[ "$(git -C "$PIGEN" rev-parse HEAD)" = "$PIGEN_COMMIT" ] || die "pi-gen is not at $PIGEN_COMMIT"
[ "$(git -C "$PIGEN" rev-parse "refs/tags/$PIGEN_TAG^{commit}")" = "$PIGEN_COMMIT" ] || die "tag $PIGEN_TAG does not point at $PIGEN_COMMIT"
echo "pi-gen $PIGEN_TAG @ $PIGEN_COMMIT"

rm -rf "$PIGEN/stage-voydar"
cp -R "$HERE/stage-voydar" "$PIGEN/stage-voydar"
touch "$PIGEN/stage2/SKIP_IMAGES"   # export only our image, not the plain Lite one

# ---- 4. config + build info ----------------------------------------------------------------
CONFIG="$WORK_DIR/config"
{
  cat "$HERE/config.template"
  echo
  echo "# generated by build.sh"
  echo "IMG_DATE=\"$IMG_DATE\""
  echo "PI_GEN_RELEASE=\"Voydar Beacon $VERSION\""
  # Required by pi-gen when DISABLE_FIRST_BOOT_USER_RENAME=1; replaced by a locked password in
  # stage-voydar, so this value never works on a device and is not kept anywhere.
  echo "FIRST_USER_PASS=\"$(python3 -c 'import secrets; print(secrets.token_hex(24))')\""
} > "$CONFIG"
chmod 600 "$CONFIG"
# pi-gen's build-docker.sh rewrites "-c <path>" to the in-container path with GNU sed syntax that
# macOS sed ignores, so the config goes where pi-gen looks by default instead (no -c needed).
cp "$CONFIG" "$PIGEN/config"

VOYDAR_COMMIT="$(git -C "$HERE" rev-parse HEAD 2>/dev/null || echo unknown)"
cat > "$STAGE_BUILD/BUILDINFO" <<EOF
Voydar Beacon image $VERSION ($IMG_DATE)
repo          $VOYDAR_COMMIT (scripts/beacon-image)
pi-gen        $PIGEN_TAG @ $PIGEN_COMMIT
base          Raspberry Pi OS Lite, Debian trixie, arm64
installer     ${INSTALLER_FILE:+local file, published at }$INSTALLER_URL sha256 $INSTALLER_SHA256
feeder        ${FEEDER_FILE:+local file, published at }$FEEDER_URL sha256 $FEEDER_SHA256
${IMAGES_INFO}preloaded     $([ "${SKIP_PRELOAD:-0}" = "1" ] && echo no || echo yes)
EOF
cat "$STAGE_BUILD/BUILDINFO"
rm -rf "$PIGEN/stage-voydar/00-voydar/files/build"
cp -R "$STAGE_BUILD" "$PIGEN/stage-voydar/00-voydar/files/build"

if [ "${PREPARE_ONLY:-0}" = "1" ]; then
  say "PREPARE_ONLY=1: inputs verified and pi-gen staged in $PIGEN; not building."
  exit 0
fi

# ---- 5. build ------------------------------------------------------------------------------
say "Building with pi-gen in Docker (this takes a while)"
rm -rf "$PIGEN/deploy"
( cd "$PIGEN" && CONTAINER_NAME="$CONTAINER_NAME" ./build-docker.sh )

IMG="$(find "$PIGEN/deploy" -maxdepth 1 -name '*voydar-beacon*.img.xz' 2>/dev/null | head -n1 || true)"
[ -n "$IMG" ] || die "pi-gen produced no .img.xz (see $PIGEN/deploy/build-docker.log)"
OUT_NAME="voydar-beacon-$VERSION.img.xz"
mv "$IMG" "$OUT_DIR/$OUT_NAME"
cp "$STAGE_BUILD/BUILDINFO" "$OUT_DIR/voydar-beacon-$VERSION.buildinfo.txt"
# pi-gen's package list (.info), block map (.bmap), SBOM (.sbom.xz, only if syft is present), log.
for ext in info bmap sbom.xz; do
  for f in "$PIGEN"/deploy/*."$ext"; do [ -f "$f" ] && cp "$f" "$OUT_DIR/voydar-beacon-$VERSION.$ext"; done
done
[ -f "$PIGEN/deploy/build.log" ] && cp "$PIGEN/deploy/build.log" "$OUT_DIR/voydar-beacon-$VERSION.build.log"
( cd "$OUT_DIR" && echo "$(sha256 "$OUT_NAME")  $OUT_NAME" > "$OUT_NAME.sha256" && cat "$OUT_NAME.sha256" )
say "Done: $OUT_DIR/$OUT_NAME (+ .sha256, .buildinfo.txt). Publishing: see scripts/beacon-image/publish.md"
