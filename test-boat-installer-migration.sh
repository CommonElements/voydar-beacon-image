#!/bin/sh
# Boat installer v4 migration test: runs the real Beacon at Sea installers v3 and v4 as root in a
# debian:trixie-slim container with fake node / npm / curl / systemctl (no network, no Signal K,
# nothing started), and checks that v4 moves a v3 boat (service and/or Signal K plugin) to the
# Voydar names in place without losing the station key, the settings or the buffered spool, and
# that running it again changes nothing.
#
#   sh scripts/beacon-image/test-boat-installer-migration.sh INSTALL_BOAT_V3.sh INSTALL_BOAT_V4.sh BUNDLE_V3.cjs BUNDLE_V4.cjs [INSTALL_BOAT_V1.sh BUNDLE_V1.cjs]
#
# Get them from a dev server or production: /api/beacon/install-boat-v3.sh, install-boat-v4.sh,
# voydar-beacon-at-sea-v3.cjs and voydar-beacon-at-sea-v4.cjs (optionally install-boat.sh and
# seadar-beacon.cjs for a v1 boat; v2 has the same on-board layout). The installers check the
# bundles' sha256, so the files must belong together. Needs Docker.
set -eu
[ $# -eq 4 ] || [ $# -eq 6 ] || { echo "usage: sh $0 INSTALL_BOAT_V3.sh INSTALL_BOAT_V4.sh BUNDLE_V3.cjs BUNDLE_V4.cjs [INSTALL_BOAT_V1.sh BUNDLE_V1.cjs]" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
WORK="${WORK_DIR:-$(mktemp -d)}"
mkdir -p "$WORK/bundles"
cp "$1" "$WORK/install-boat-v3.sh"
cp "$2" "$WORK/install-boat-v4.sh"
cp "$3" "$WORK/bundles/voydar-beacon-at-sea-v3.cjs"
cp "$4" "$WORK/bundles/voydar-beacon-at-sea-v4.cjs"
if [ $# -eq 6 ]; then cp "$5" "$WORK/install-boat-v1.sh"; cp "$6" "$WORK/bundles/seadar-beacon.cjs"; fi

cat > "$WORK/inner.sh" <<'INNER'
#!/bin/sh
set -eu
KEY=sbk_$(printf '0123456789abcdef%.0s' 1 2 3)    # dummy station keys
KEY2=sbk_$(printf 'fedcba9876543210%.0s' 1 2 3)
FAILS=0
ok() { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
check() { name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }

# ---- fakes (in /usr/local/bin, so they are on PATH for su too) -------------------------------
B=/usr/local/bin
mkdir -p /var/lib/fakesystemd /etc/systemd/system
cat > $B/systemctl <<'EOF'
#!/bin/sh
# Fake systemd: enabled units are files NAME, running ones active-NAME, in /var/lib/fakesystemd.
S=/var/lib/fakesystemd
q() { n=${1%.service}; echo "$n"; }
cmd=$1; shift
[ "${1:-}" = --now ] && { now=1; shift; } || now=0
[ "${1:-}" = --quiet ] && shift
case "$cmd" in
  daemon-reload) : ;;
  enable) touch "$S/$(q "$1")"; [ $now = 1 ] && touch "$S/active-$(q "$1")"; : ;;
  disable) rm -f "$S/$(q "$1")"; [ $now = 1 ] && rm -f "$S/active-$(q "$1")"; : ;;
  start|restart) touch "$S/active-$(q "$1")" ;;
  stop) rm -f "$S/active-$(q "$1")" ;;
  is-active) [ -f "$S/active-$(q "$1")" ] ;;
  list-units) for f in "$S"/active-*; do [ -f "$f" ] && echo "${f##*/active-}.service loaded active running"; done; : ;;
  *) : ;;
esac
EOF
cat > $B/node <<'EOF'
#!/bin/sh
case "$1" in -p) echo 22 ;; -v) echo v22.11.0 ;; *) exit 0 ;; esac
EOF
cat > $B/curl <<'EOF'
#!/bin/sh
# Fake curl: serves /work/bundles/<last path segment of the URL>.
out="" url=""
while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac; done
cp "/work/bundles/${url##*/}" "$out"
EOF
cat > $B/npm <<'EOF'
#!/bin/sh
# Fake npm in the current directory: node_modules/<name> links, package.json dependencies.
cmd=$1; shift
arg=""; for a in "$@"; do case "$a" in --*) ;; *) arg=$a ;; esac; done
touch .deps; mkdir -p node_modules
case "$cmd" in
  install)
    name=$(sed -n 's/.*"name":"\([^"]*\)".*/\1/p' "$arg/package.json")
    ln -sfn "$arg" "node_modules/$name"
    grep -v "^$name " .deps > .deps.new || true; echo "$name file:$arg" >> .deps.new; sort .deps.new > .deps; rm .deps.new ;;
  uninstall)
    rm -f "node_modules/$arg"; grep -v "^$arg " .deps > .deps.new || true; mv .deps.new .deps ;;
esac
{ printf '{"dependencies":{'; awk '{printf "%s\"%s\":\"%s\"", (NR>1?",":""), $1, $2}' .deps; printf '}}\n'; } > package.json
EOF
printf '#!/bin/sh\nexit 0\n' > $B/journalctl
printf '#!/bin/sh\necho "fake apt-get: no network in this test" >&2\nexit 1\n' > $B/apt-get
chmod 755 $B/*
useradd -m -s /bin/sh pi
SK=/home/pi/.signalk
V4SHA=$(sha256sum /work/bundles/voydar-beacon-at-sea-v4.cjs | cut -d' ' -f1)

reset_system() {
  rm -rf /opt/seadar-beacon /opt/voydar-beacon /var/lib/seadar-beacon /var/lib/voydar-beacon "$SK" /var/lib/fakesystemd/*
  rm -f /etc/seadar-beacon.env /etc/voydar-beacon.env /etc/systemd/system/*-beacon.service
  for u in seadar-beacon voydar-beacon; do userdel "$u" 2>/dev/null || true; groupdel "$u" 2>/dev/null || true; done
}
signalk_dir() { su pi -c "mkdir -p $SK/plugin-config-data && cd $SK && printf '{}\n' > package.json"; touch /var/lib/fakesystemd/signalk /var/lib/fakesystemd/active-signalk; }
chunk() { d=$1; n=$2; mkdir -p "$d"; printf 'chunk %s\n' "$n" > "$d/1760000000${n}_0123abcd${n}_10_1760000000${n}.json.gz"; }
v3() { sh /work/install-boat-v3.sh "$@" > /tmp/v3.log 2>&1 || { cat /tmp/v3.log; bad "installer v3 $*"; }; }
v4() { sh /work/install-boat-v4.sh "$@" > /tmp/v4.log 2>&1 || { cat /tmp/v4.log; bad "installer v4 $*"; }; }
snapshot() {
  { id voydar-beacon; id seadar-beacon; ls -la /etc/*-beacon.env /opt/*-beacon /var/lib/*-beacon /etc/systemd/system "$SK" "$SK/node_modules" "$SK/plugin-config-data" "$SK/plugin-config-data"/*/spool
    ls /var/lib/fakesystemd; for f in /etc/*-beacon.env /etc/systemd/system/*-beacon.service "$SK"/package.json "$SK"/plugin-config-data/*.json; do echo "== $f"; cat "$f"; done
  } 2>&1 | grep -v '^total' | sed 's/ [A-Z][a-z][a-z] [ 0-9][0-9] [0-9:]* / /'
}
idem() {
  snapshot > /tmp/after-first
  v4 "$@"
  snapshot > /tmp/after-second
  check "rerunning v4 changes nothing" cmp /tmp/after-first /tmp/after-second
  cmp -s /tmp/after-first /tmp/after-second || diff /tmp/after-first /tmp/after-second || true
}

echo "### 1. v3 service (private, custom gateway, own MMSI, buffered chunks), then v4 without a key"
reset_system
v3 --tcp 192.168.4.1:10110 --private "$KEY"
check "v3 left the seadar-beacon service running" test -f /var/lib/fakesystemd/active-seadar-beacon
echo "SEADAR_BEACON_OWN_MMSI=235009999" >> /etc/seadar-beacon.env
chunk /var/lib/seadar-beacon 1; chunk /var/lib/seadar-beacon 2; chown -R seadar-beacon /var/lib/seadar-beacon
OLDUID=$(id -u seadar-beacon)
v4
check "user renamed to voydar-beacon with the same uid" test "$(id -u voydar-beacon)" = "$OLDUID"
check "no seadar-beacon user or group" sh -c '! id seadar-beacon && ! getent group seadar-beacon'
check "key kept as VOYDAR_BEACON_KEY" grep -qx "VOYDAR_BEACON_KEY=$KEY" /etc/voydar-beacon.env
check "private kept" grep -qx 'VOYDAR_BEACON_PRIVATE=1' /etc/voydar-beacon.env
check "own ship still not shared" grep -qx 'VOYDAR_BEACON_SHARE_OWN=0' /etc/voydar-beacon.env
check "own MMSI kept" grep -qx 'VOYDAR_BEACON_OWN_MMSI=235009999' /etc/voydar-beacon.env
check "spool path is /var/lib/voydar-beacon" grep -qx 'VOYDAR_BEACON_SPOOL=/var/lib/voydar-beacon' /etc/voydar-beacon.env
check "no SEADAR_ lines left" sh -c '! grep -q SEADAR /etc/voydar-beacon.env'
check "env file mode 600" test "$(stat -c %a /etc/voydar-beacon.env)" = 600
check "old env file removed" test ! -e /etc/seadar-beacon.env
check "both buffered chunks moved" test "$(ls /var/lib/voydar-beacon | wc -l)" -eq 2
check "chunks owned by voydar-beacon" test "$(stat -c %U /var/lib/voydar-beacon/* | sort -u)" = voydar-beacon
check "old spool dir gone" test ! -e /var/lib/seadar-beacon
check "old unit removed, disabled and stopped" sh -c 'test ! -e /etc/systemd/system/seadar-beacon.service && test ! -e /var/lib/fakesystemd/seadar-beacon && test ! -e /var/lib/fakesystemd/active-seadar-beacon'
check "voydar-beacon enabled and running" sh -c 'test -f /var/lib/fakesystemd/voydar-beacon && test -f /var/lib/fakesystemd/active-voydar-beacon'
check "unit keeps the custom gateway" grep -qx 'ExecStart=/usr/local/bin/node /opt/voydar-beacon/voydar-beacon.cjs --tcp 192.168.4.1:10110' /etc/systemd/system/voydar-beacon.service
check "unit runs as voydar-beacon with the new env file" sh -c 'grep -qx User=voydar-beacon /etc/systemd/system/voydar-beacon.service && grep -qx EnvironmentFile=/etc/voydar-beacon.env /etc/systemd/system/voydar-beacon.service'
check "bundle v4 installed" test "$(sha256sum /opt/voydar-beacon/voydar-beacon.cjs | cut -d' ' -f1)" = "$V4SHA"
check "/opt/seadar-beacon removed" test ! -e /opt/seadar-beacon
check "no Signal K plugin set up" test ! -e /opt/voydar-beacon/signalk-voydar
idem
v4 "$KEY"
check "rerun with the key and no --private: public (options as given)" grep -qx 'VOYDAR_BEACON_PRIVATE=0' /etc/voydar-beacon.env
check "rerun with the key keeps the gateway and own MMSI" sh -c 'grep -q -- "--tcp 192.168.4.1:10110" /etc/systemd/system/voydar-beacon.service && grep -qx VOYDAR_BEACON_OWN_MMSI=235009999 /etc/voydar-beacon.env'

echo "### 2. v3 Signal K plugin (private, buffered chunks, Signal K running), then v4 without a key"
reset_system
signalk_dir
SUDO_USER=pi v3 --signalk --private "$KEY"
check "v3 left signalk-seadar installed" sh -c "test -L $SK/node_modules/signalk-seadar && grep -q '\"key\":\"$KEY\"' $SK/plugin-config-data/signalk-seadar.json"
su pi -c "mkdir -p $SK/plugin-config-data/signalk-seadar/spool"
chunk "$SK/plugin-config-data/signalk-seadar/spool" 3; chunk "$SK/plugin-config-data/signalk-seadar/spool" 4; chown -R pi "$SK/plugin-config-data"
SUDO_USER=pi v4
C=$SK/plugin-config-data
check "settings moved to signalk-voydar.json with the key" grep -q "\"key\":\"$KEY\"" "$C/signalk-voydar.json"
check "private kept" grep -q '"private":true' "$C/signalk-voydar.json"
check "settings file owned by pi, mode 600" test "$(stat -c '%U %a' "$C/signalk-voydar.json")" = 'pi 600'
check "signalk-seadar.json removed" test ! -e "$C/signalk-seadar.json"
check "both buffered chunks moved to the new data dir" test "$(ls "$C/signalk-voydar/spool" | wc -l)" -eq 2
check "old data dir gone" test ! -e "$C/signalk-seadar"
check "old plugin uninstalled" sh -c "test ! -e $SK/node_modules/signalk-seadar && ! grep -q signalk-seadar $SK/package.json"
check "signalk-voydar installed" sh -c "test \"\$(readlink $SK/node_modules/signalk-voydar)\" = /opt/voydar-beacon/signalk-voydar && grep -q '\"signalk-voydar\"' $SK/package.json"
check "plugin is bundle v4" test "$(sha256sum /opt/voydar-beacon/signalk-voydar/index.js | cut -d' ' -f1)" = "$V4SHA"
check "plugin package named signalk-voydar" grep -q '"name":"signalk-voydar"' /opt/voydar-beacon/signalk-voydar/package.json
check "Signal K running again" test -f /var/lib/fakesystemd/active-signalk
check "no service installed" sh -c 'test ! -e /etc/systemd/system/voydar-beacon.service && ! id voydar-beacon'
check "/opt/seadar-beacon removed" test ! -e /opt/seadar-beacon
SUDO_USER=pi idem

echo "### 3. v3 service and v3 plugin with different keys, then v4 --signalk with a new key"
reset_system
signalk_dir
v3 --udp 10110 "$KEY"
SUDO_USER=pi v3 --signalk "$KEY"
SUDO_USER=pi v4 --signalk --share-own-position "$KEY2"
check "service moved and keeps its own key" grep -qx "VOYDAR_BEACON_KEY=$KEY" /etc/voydar-beacon.env
check "service keeps its own options" sh -c 'grep -qx VOYDAR_BEACON_SHARE_OWN=0 /etc/voydar-beacon.env && grep -q -- "--udp 10110" /etc/systemd/system/voydar-beacon.service'
check "service running" test -f /var/lib/fakesystemd/active-voydar-beacon
check "plugin gets the new key and options" sh -c "grep -q '\"key\":\"$KEY2\"' $SK/plugin-config-data/signalk-voydar.json && grep -q '\"shareOwnPosition\":true' $SK/plugin-config-data/signalk-voydar.json"
check "nothing seadar left" sh -c "! ls -d /etc/seadar-beacon.env /opt/seadar-beacon /var/lib/seadar-beacon /etc/systemd/system/seadar-beacon.service $SK/node_modules/signalk-seadar $SK/plugin-config-data/signalk-seadar* 2>/dev/null && ! id seadar-beacon"
SUDO_USER=pi idem

echo "### 4. An interrupted move: old and new spool both exist"
reset_system
v3 --tcp 10.0.0.5:2000 "$KEY"
chunk /var/lib/seadar-beacon 5; chunk /var/lib/seadar-beacon 6
chunk /var/lib/voydar-beacon 7; chunk /var/lib/voydar-beacon 5
v4
check "chunks merged, none overwritten" test "$(ls /var/lib/voydar-beacon | wc -l)" -eq 3
check "the same-named chunk kept in the old dir" sh -c 'test "$(ls /var/lib/seadar-beacon | wc -l)" -eq 1'
check "key kept" grep -qx "VOYDAR_BEACON_KEY=$KEY" /etc/voydar-beacon.env

echo "### 5. A fresh v4 install uses only Voydar names"
reset_system
v4 --tcp 192.168.4.1:10110 "$KEY"
check "service, user and files are voydar-beacon" sh -c 'id voydar-beacon && test -f /etc/voydar-beacon.env && test -f /var/lib/fakesystemd/active-voydar-beacon'
check "no seadar name on the system" sh -c '! ls -d /etc/seadar* /opt/seadar* /var/lib/seadar* /etc/systemd/system/seadar* 2>/dev/null && ! id seadar-beacon && ! grep -rqi seadar /etc/voydar-beacon.env /etc/systemd/system/voydar-beacon.service'
check "a bad key is refused" sh -c '! sh /work/install-boat-v4.sh sbk_nothex'
idem

if [ -f /work/install-boat-v1.sh ]; then
  echo "### 6. v1 service (the original installer), then v4 without a key"
  reset_system
  sh /work/install-boat-v1.sh --udp 2000 --share-own-position "$KEY" > /tmp/v1.log 2>&1 || { cat /tmp/v1.log; bad "installer v1"; }
  chunk /var/lib/seadar-beacon 8
  v4
  check "key and options kept" sh -c "grep -qx VOYDAR_BEACON_KEY=$KEY /etc/voydar-beacon.env && grep -qx VOYDAR_BEACON_SHARE_OWN=1 /etc/voydar-beacon.env"
  check "source kept" grep -q -- '--udp 2000$' /etc/systemd/system/voydar-beacon.service
  check "chunk moved" test "$(ls /var/lib/voydar-beacon | wc -l)" -eq 1
  check "nothing seadar left" sh -c '! ls -d /etc/seadar-beacon.env /opt/seadar-beacon /var/lib/seadar-beacon /etc/systemd/system/seadar-beacon.service 2>/dev/null && ! id seadar-beacon'
  idem
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL OK"; else echo "$FAILS FAILED"; exit 1; fi
INNER

docker run --rm -v "$WORK:/work" debian:trixie-slim sh /work/inner.sh
