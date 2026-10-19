#!/bin/sh
# Installer v7 migration test: runs the real installer v6 and v7 scripts as root in a
# debian:trixie-slim container with fake docker / systemctl / modprobe (no radios, no network, no
# containers started), and checks that v7 moves a v6 install to the Voydar names in place
# without losing the station key or the extra AIS outputs, and that rerunning it changes nothing.
#
#   sh scripts/beacon-image/test-installer-migration.sh INSTALL_V6.sh INSTALL_V7.sh
#
# Get the two scripts from a dev server or production (/api/beacon/install-v6.sh and
# /api/beacon/install-v7.sh). Needs Docker. Two fake RTL-SDR dongles (serials 00000162 and
# 00001090) appear in a tmpfs over /sys/bus/usb/devices, so --both works.
set -eu
[ $# -eq 2 ] || { echo "usage: sh $0 INSTALL_V6.sh INSTALL_V7.sh" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
WORK="${WORK_DIR:-$(mktemp -d)}"
mkdir -p "$WORK"
cp "$1" "$WORK/install-v6.sh"
cp "$2" "$WORK/install-v7.sh"

cat > "$WORK/inner.sh" <<'INNER'
#!/bin/sh
set -eu
KEY=sbk_$(printf '0123456789abcdef%.0s' 1 2 3)   # a dummy station key
FAILS=0
ok() { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
check() { name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }

# ---- fakes ----------------------------------------------------------------------------------
mkdir -p /fake/bin /var/lib/fakedocker /var/lib/fakesystemd /etc/systemd/system /usr/local/bin /etc/modprobe.d
cat > /fake/bin/docker <<'EOF'
#!/bin/sh
# Fake docker: containers are files in /var/lib/fakedocker (NAME = one argument per line).
S=/var/lib/fakedocker
cmd=$1; shift
case "$cmd" in
  version) echo 27.3.1 ;;
  rm)
    [ "$1" = -f ] && shift
    rc=0
    for n in "$@"; do if [ -f "$S/$n" ]; then rm -f "$S/$n"; else echo "No such container: $n" >&2; rc=1; fi; done
    exit $rc ;;
  rename) [ -f "$S/$1" ] && [ ! -f "$S/$2" ] || exit 1; mv "$S/$1" "$S/$2" ;;
  run)
    name=""
    for a in "$@"; do [ "${prev:-}" = --name ] && name=$a; prev=$a; done
    [ -n "$name" ] || exit 1
    [ ! -f "$S/$name" ] || { echo "Conflict: $name exists" >&2; exit 125; }
    printf '%s\n' "$@" > "$S/$name"; echo "id-$name" ;;
  ps)
    all=0 filter="" q=0
    while [ $# -gt 0 ]; do
      case "$1" in -a) all=1 ;; -q) q=1 ;; --filter) shift; case "$1" in name=*) filter=${1#name=} ;; esac ;; --format) shift ;; esac
      shift
    done
    for f in "$S"/*; do
      [ -f "$f" ] || continue
      n=${f##*/}
      case "$n" in *"$filter"*) ;; *) continue ;; esac
      if [ "$q" = 1 ]; then echo "id-$n"; else echo "$n"; fi
    done ;;
  logs) : ;;
  *) echo "fake docker: $cmd not supported" >&2; exit 1 ;;
esac
EOF
cat > /fake/bin/systemctl <<'EOF'
#!/bin/sh
# Fake systemd: enabled units are files in /var/lib/fakesystemd.
S=/var/lib/fakesystemd
case "$1" in
  daemon-reload) : ;;
  enable) touch "$S/${2%.service}" ;;
  restart|start) : ;;
  disable) [ "$2" = --now ] && shift; rm -f "$S/${2%.service}" ;;
  status|is-active) [ -f "$S/${2%.service}" ] ;;
  list-units) : ;;
  *) : ;;
esac
EOF
for c in modprobe; do printf '#!/bin/sh\nexit 0\n' > /fake/bin/$c; done
printf '#!/bin/sh\necho "fake apt-get: no network in this test" >&2\nexit 1\n' > /fake/bin/apt-get
printf '#!/bin/sh\nexit 0\n' > /fake/bin/curl
chmod 755 /fake/bin/*
export PATH=/fake/bin:$PATH

# Two RTL-SDR dongles: AIS (serial 00000162) and ADS-B (serial 00001090).
for d in 1-1:00000162 1-2:00001090; do
  dir=/sys/bus/usb/devices/${d%%:*}
  mkdir -p "$dir"
  echo 0bda > "$dir/idVendor"; echo 2838 > "$dir/idProduct"; echo "${d#*:}" > "$dir/serial"
  echo Realtek > "$dir/manufacturer"; echo RTL2838UHIDIR > "$dir/product"
done

reset_system() {
  rm -rf /etc/seadar /etc/voydar /var/lib/fakedocker/* /var/lib/fakesystemd/* /run/seadar-adsb /run/voydar-adsb
  rm -f /usr/local/bin/seadar-* /usr/local/bin/voydar-* /etc/systemd/system/seadar-* /etc/systemd/system/voydar-* /etc/modprobe.d/*-rtl-sdr.conf
}
v6() { sh /work/install-v6.sh --yes "$@" > /tmp/v6.log 2>&1 || { cat /tmp/v6.log; bad "installer v6 $*"; }; }
v7() { sh /work/install-v7.sh --yes "$@" > /tmp/v7.log 2>&1 || { cat /tmp/v7.log; bad "installer v7 $*"; }; }
arg() { grep -qxF -- "$2" "/var/lib/fakedocker/$1"; }
snapshot() {
  { ls -la /etc/voydar /usr/local/bin; ls /var/lib/fakedocker /var/lib/fakesystemd /etc/systemd/system
    for f in /etc/voydar/* /var/lib/fakedocker/* /etc/systemd/system/*adsb*; do echo "== $f"; cat "$f"; done; } 2>&1 | grep -v ' total \|^total' | sed 's/ [A-Z][a-z][a-z] [ 0-9][0-9] [0-9:]* / /'
}

echo "### 1. v6 AIS with two extra outputs, then v7 (AIS)"
reset_system
v6 --ais --output udp:5.9.207.224:10110 --output udp:144.76.105.244:2948 "$KEY" 33.8
check "v6 left seadar-beacon" test -f /var/lib/fakedocker/seadar-beacon
check "v6 left /etc/seadar/outputs.conf" grep -q '144.76.105.244' /etc/seadar/outputs.conf
v7 --ais "$KEY" 33.8
check "/etc/voydar is a directory" test -d /etc/voydar -a ! -L /etc/voydar
check "/etc/seadar is a link to /etc/voydar" test "$(readlink /etc/seadar)" = /etc/voydar
check "key kept as VOYDAR_KEY" grep -qx "VOYDAR_KEY=$KEY" /etc/voydar/beacon.env
check "no SEADAR_ lines left in beacon.env / ais.env" sh -c '! grep -q "^SEADAR_" /etc/voydar/beacon.env /etc/voydar/ais.env'
check "ais.env has VOYDAR_AIS_GAIN=33.8" grep -qx 'VOYDAR_AIS_GAIN=33.8' /etc/voydar/ais.env
check "both outputs kept" sh -c 'grep -q "udp 5.9.207.224 10110" /etc/voydar/outputs.conf && grep -q "udp 144.76.105.244 2948" /etc/voydar/outputs.conf'
check "beacon.env, ais.env, outputs.conf mode 600" sh -c '[ "$(stat -c %a /etc/voydar/beacon.env /etc/voydar/ais.env /etc/voydar/outputs.conf | sort -u)" = 600 ]'
check "no seadar-beacon container" test ! -f /var/lib/fakedocker/seadar-beacon
check "voydar-beacon container" test -f /var/lib/fakedocker/voydar-beacon
check "AIS feeds receiver.voydar.com" arg voydar-beacon https://receiver.voydar.com/v1/beacon/ingest
check "AIS authenticates as voydar:<key>" arg voydar-beacon "voydar:$KEY"
check "AIS keeps the UDP outputs" sh -c 'grep -qxF 5.9.207.224 /var/lib/fakedocker/voydar-beacon && grep -qxF 144.76.105.244 /var/lib/fakedocker/voydar-beacon'
check "AIS at gain 33.8" grep -qxF 33.8 /var/lib/fakedocker/voydar-beacon
check "modprobe file renamed" sh -c 'test -f /etc/modprobe.d/voydar-rtl-sdr.conf && test ! -e /etc/modprobe.d/seadar-rtl-sdr.conf'
check "voydar-outputs installed" test -x /usr/local/bin/voydar-outputs
check "seadar-outputs is a wrapper, not the helper" sh -c 'test ! -L /usr/local/bin/seadar-outputs && grep -q "exec /usr/local/bin/voydar-outputs" /usr/local/bin/seadar-outputs'
check "old command name still lists the outputs" sh -c 'seadar-outputs list | grep -q 144.76.105.244'
snapshot > /tmp/after-first
v7 --ais "$KEY" 33.8
snapshot > /tmp/after-second
check "rerunning v7 changes nothing" cmp /tmp/after-first /tmp/after-second
[ "$FAILS" -eq 0 ] || diff /tmp/after-first /tmp/after-second || true
check "outputs not duplicated" test "$(grep -c '^udp ' /etc/voydar/outputs.conf)" -eq 2
check "voydar-outputs add still recreates the container" sh -c 'voydar-outputs add tcp ais.example.com 5631 >/dev/null 2>&1; grep -qxF ais.example.com /var/lib/fakedocker/voydar-beacon'

echo "### 2. v6 AIS + ADS-B, then v7 AIS only (ADS-B is kept, under Voydar names)"
reset_system
v6 --both "$KEY"
check "v6 left the seadar-adsb-feed service" test -f /etc/systemd/system/seadar-adsb-feed.service
check "v6 left seadar-adsb" test -f /var/lib/fakedocker/seadar-adsb
v7 --ais "$KEY"
check "seadar-adsb renamed to voydar-adsb" sh -c 'test -f /var/lib/fakedocker/voydar-adsb && test ! -f /var/lib/fakedocker/seadar-adsb'
check "legacy feeder service removed" sh -c 'test ! -f /etc/systemd/system/seadar-adsb-feed.service && test ! -f /var/lib/fakesystemd/seadar-adsb-feed'
check "voydar-adsb-feed service enabled" test -f /var/lib/fakesystemd/voydar-adsb-feed
check "voydar-adsb-feed reads the same source" grep -qx 'ExecStart=/usr/local/bin/voydar-adsb-feed /run/seadar-adsb/aircraft.json' /etc/systemd/system/voydar-adsb-feed.service
check "feeder unit reads /etc/voydar/beacon.env" grep -qx 'EnvironmentFile=/etc/voydar/beacon.env' /etc/systemd/system/voydar-adsb-feed.service
check "seadar-adsb-feed is a wrapper" grep -q 'exec /usr/local/bin/voydar-adsb-feed' /usr/local/bin/seadar-adsb-feed
check "voydar-adsb-feed is feeder v3" sh -c '/usr/local/bin/voydar-adsb-feed --version | grep -q v3'
check "AIS is voydar-beacon with voydar:<key>" arg voydar-beacon "voydar:$KEY"

echo "### 3. v6 AIS + ADS-B, then v7 AIS + ADS-B"
reset_system
v6 --both --output udp:5.9.207.224:10110 "$KEY"
v7 --both "$KEY"
check "voydar-adsb uses /run/voydar-adsb" grep -qxF /run/voydar-adsb:/run/readsb /var/lib/fakedocker/voydar-adsb
check "ADS-B on dongle 00001090" grep -qxF READSB_RTLSDR_DEVICE=00001090 /var/lib/fakedocker/voydar-adsb
check "feeder source /run/voydar-adsb" grep -qx 'ExecStart=/usr/local/bin/voydar-adsb-feed /run/voydar-adsb/aircraft.json' /etc/systemd/system/voydar-adsb-feed.service
check "only Voydar containers and services" sh -c '! ls /var/lib/fakedocker /var/lib/fakesystemd /etc/systemd/system | grep -q seadar'
check "output kept" grep -qxF 5.9.207.224 /var/lib/fakedocker/voydar-beacon

echo "### 4. v6 AIS, then v7 ADS-B only (AIS restarted under Voydar names with its outputs)"
reset_system
v6 --ais --output udp:5.9.207.224:10110 "$KEY"
v7 --adsb --adsb-serial 00001090 "$KEY"
check "voydar-beacon recreated with voydar:<key>" arg voydar-beacon "voydar:$KEY"
check "voydar-beacon keeps its output" grep -qxF 5.9.207.224 /var/lib/fakedocker/voydar-beacon
check "voydar-adsb running" test -f /var/lib/fakedocker/voydar-adsb

echo "### 5. both /etc/seadar and /etc/voydar exist (merged, nothing lost)"
reset_system
v6 --ais --output udp:5.9.207.224:10110 "$KEY"
mkdir -p /etc/voydar
printf 'udp 144.76.105.244 2948\n' > /etc/voydar/outputs.conf
chmod 600 /etc/voydar/outputs.conf
v7 --ais "$KEY"
check "/etc/seadar became a link" test -L /etc/seadar
check "outputs from both kept" sh -c 'grep -qxF 5.9.207.224 /var/lib/fakedocker/voydar-beacon && grep -qxF 144.76.105.244 /var/lib/fakedocker/voydar-beacon'

echo "### 6. fresh install with v7, SD-image environment names (old SEADAR_* and new VOYDAR_*)"
reset_system
env SEADAR_IMAGE=1 SEADAR_LAT=27.76 SEADAR_LON=-82.63 sh /work/install-v7.sh --both "$KEY" > /tmp/v7.log 2>&1 || { cat /tmp/v7.log; bad "v7 with SEADAR_*"; }
check "SEADAR_LAT/LON still accepted" grep -qxF READSB_LAT=27.76 /var/lib/fakedocker/voydar-adsb
reset_system
env VOYDAR_IMAGE=1 VOYDAR_LAT=1.5 VOYDAR_LON=2.5 sh /work/install-v7.sh --both "$KEY" > /tmp/v7.log 2>&1 || { cat /tmp/v7.log; bad "v7 with VOYDAR_*"; }
check "VOYDAR_LAT/LON read" grep -qxF READSB_LAT=1.5 /var/lib/fakedocker/voydar-adsb
check "fresh install has no /etc/seadar" test ! -e /etc/seadar

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; exit 1; fi
INNER

docker run --rm -v "$WORK:/work" --tmpfs /sys/bus/usb/devices debian:trixie-slim sh /work/inner.sh
