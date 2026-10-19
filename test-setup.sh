#!/bin/sh
# Smoke test for the setup page in --dry-run mode (touches nothing on the system).
#   sh scripts/beacon-image/test-setup.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
SETUP="$HERE/stage-voydar/00-voydar/files/voydar-setup.py"
TEMPLATE="$HERE/stage-voydar/00-voydar/files/voydar-wifi.txt"
PORT="${PORT:-18080}"
TMP="$(mktemp -d)"
KEY="sbk_0123456789abcdef0123456789abcdef0123456789abcdef"
BASE="http://127.0.0.1:$PORT"
PID="" PID2=""
trap 'kill $PID $PID2 2>/dev/null || true; rm -rf "$TMP"' EXIT INT TERM

python3 -m py_compile "$SETUP" "$HERE/stage-voydar/00-voydar/files/voydar-preload.py" "$HERE/fetch-image.py"
# An output saved earlier (voydar-outputs), plus tampered lines the page must ignore.
mkdir -p "$TMP/state"
printf '# earlier\nudp 5.9.207.224 10110  # MarineTraffic\nudp $(reboot) 1\ntcp 1.2.3.4 5;id\n' > "$TMP/state/outputs.conf"
python3 "$SETUP" --dry-run --port "$PORT" --state-dir "$TMP/state" > "$TMP/server.log" 2>&1 &
PID=$!
i=0; until curl -fsS -o /dev/null "$BASE/" 2>/dev/null; do i=$((i+1)); [ $i -lt 50 ] || { cat "$TMP/server.log"; exit 1; }; sleep 0.2; done

fail() { echo "FAIL: $1"; cat "$TMP/server.log"; exit 1; }
token_of() { sed -n 's/.*name=csrf value="\([^"]*\)".*/\1/p' "$1"; }
curl -fsS -c "$TMP/c" "$BASE/" > "$TMP/form.html"
grep -q 'Set up your Voydar Beacon' "$TMP/form.html" || fail "form not served"
grep -q '00001090' "$TMP/form.html" || fail "dongle serials not shown"
if grep -Eq '(src|href)=["'"'"']?https?://' "$TMP/form.html"; then fail "external asset referenced"; fi
TOKEN="$(token_of "$TMP/form.html")"
[ -n "$TOKEN" ] || fail "no csrf token"
grep -q 'name=aishub_host' "$TMP/form.html" || fail "no Also feed section"
grep -q 'id=keep_outputs name=keep_outputs value=1 checked' "$TMP/form.html" || fail "existing outputs not offered"
grep -q 'udp 5.9.207.224 10110' "$TMP/form.html" || fail "existing output not shown"
if grep -q 'reboot' "$TMP/form.html"; then fail "invalid saved output shown"; fi

# Foreign Host header (DNS rebinding / captive probe) -> redirect, no form
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' "$BASE/")"
[ "$code" = 302 ] || fail "foreign Host not redirected ($code)"

# Missing / wrong token -> 403
code="$(curl -s -o /dev/null -w '%{http_code}' --data-urlencode "key=$KEY" --data 'mode=ais&csrf=nope' "$BASE/setup")"
[ "$code" = 403 ] || fail "bad token accepted ($code)"

# Cross-origin POST -> 403
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'Origin: http://evil.example' --data "csrf=$TOKEN&mode=ais" "$BASE/setup")"
[ "$code" = 403 ] || fail "cross-origin POST accepted ($code)"

# Invalid key -> 400 with a message (the token was not consumed by the refused requests above? it was
# not: origin check happens before token use)
curl -s -o "$TMP/bad.html" -w '%{http_code}' --data "csrf=$TOKEN&key=nope&mode=ais" "$BASE/setup" > "$TMP/code"
[ "$(cat "$TMP/code")" = 400 ] || fail "invalid key not rejected"
grep -q 'not a station key' "$TMP/bad.html" || fail "no validation message"
TOKEN2="$(token_of "$TMP/bad.html")"

# Private + ADS-B is refused by the installer, so the form refuses it first
curl -s -o "$TMP/priv.html" -w '%{http_code}' --data "csrf=$TOKEN2&key=$KEY&mode=adsb&private=1" "$BASE/setup" > "$TMP/code"
[ "$(cat "$TMP/code")" = 400 ] || fail "private ADS-B not rejected"
grep -q 'Private feeds are AIS only' "$TMP/priv.html" || fail "no private ADS-B message"
TOKEN2="$(token_of "$TMP/priv.html")"

# Both without choosing the dongles -> 400 (the installer would stop and ask on a tty nobody sees)
curl -s -o "$TMP/both.html" -w '%{http_code}' --data "csrf=$TOKEN2&key=$KEY&mode=both" "$BASE/setup" > "$TMP/code"
[ "$(cat "$TMP/code")" = 400 ] || fail "both without serials not rejected"
TOKEN2="$(token_of "$TMP/both.html")"

# Also feed: strict validation. Every injection attempt is refused with a message, nothing installed.
curl -s -o "$TMP/out.html" -w '%{http_code}' --data-urlencode "csrf=$TOKEN2" --data-urlencode "key=$KEY" --data 'mode=ais' \
  --data-urlencode 'aishub_host=1.2.3.4;reboot' --data-urlencode 'aishub_port=2948' \
  --data-urlencode 'mt_host=$(reboot)' --data-urlencode 'mt_port=1' \
  --data-urlencode 'vf_host=ais.vesselfinder.com' --data-urlencode 'vf_port=99999' \
  --data 'c1_type=udp' --data-urlencode 'c1_target=a`id`b:10110' \
  --data 'c2_type=http' --data-urlencode 'c2_target=http://x.example/$(id)' "$BASE/setup" > "$TMP/code"
[ "$(cat "$TMP/code")" = 400 ] || fail "injected outputs not rejected"
for m in 'AISHub: the host is' 'MarineTraffic: the host is' 'VesselFinder: the port is' 'Custom output 1: use host:port' 'Custom output 2: use an http'; do
  grep -q "$m" "$TMP/out.html" || fail "missing output error: $m"
done
TOKEN2="$(token_of "$TMP/out.html")"
curl -s -o "$TMP/out.html" --data-urlencode "csrf=$TOKEN2" --data-urlencode "key=$KEY" --data 'mode=ais' \
  --data-urlencode "$(printf 'vf_host=a\nb.example')" --data 'vf_port=5500' --data 'aishub_host=data.aishub.net' \
  --data 'c1_type=exec' --data-urlencode 'c1_target=sh:1' \
  --data 'c2_type=http' --data-urlencode "c2_target=https://x.example/a'b" "$BASE/setup"
grep -q 'VesselFinder: the host is' "$TMP/out.html" || fail "newline in a host accepted"
grep -q 'AISHub: enter both the host and the port' "$TMP/out.html" || fail "host without port accepted"
grep -q 'Custom output 1: choose UDP, TCP or HTTP' "$TMP/out.html" || fail "unknown output type accepted"
grep -q 'Custom output 2: use an http' "$TMP/out.html" || fail "quote in a URL accepted"
TOKEN2="$(token_of "$TMP/out.html")"
curl -s -o "$TMP/out.html" --data-urlencode "csrf=$TOKEN2" --data-urlencode "key=$KEY" --data 'mode=adsb' \
  --data 'aishub_host=data.aishub.net&aishub_port=2948' "$BASE/setup"
grep -q 'Other networks are fed from the AIS receiver' "$TMP/out.html" || fail "outputs accepted with ADS-B only"
TOKEN2="$(token_of "$TMP/out.html")"
[ ! -f "$TMP/state/beacon.env" ] || fail "a rejected form changed the system"

# Token is one-time
code="$(curl -s -o /dev/null -w '%{http_code}' --data "csrf=$TOKEN&key=$KEY&mode=ais" "$BASE/setup")"
[ "$code" = 403 ] || fail "csrf token reused ($code)"

# Valid submit (hotspot mode in dry-run: joins fake Wi-Fi, runs fake installer), with extra outputs:
# keep the saved MarineTraffic one, add AISHub, the same MarineTraffic again (deduplicated), TCP, HTTP.
curl -fsS -o "$TMP/ok.html" --data-urlencode "csrf=$TOKEN2" --data-urlencode "key=$KEY" \
  --data-urlencode 'ssid=café;wifi' --data-urlencode 'wifipw=hunter2hunter2' --data 'country=nl&mode=both&ais_serial=00000162&adsb_serial=00001090&gain=33.8&lat=51.9&lon=4.4&ssh=1' \
  --data-urlencode 'sshpw=correct horse battery' \
  --data 'keep_outputs=1&aishub_host=data.aishub.net&aishub_port=2948&mt_host=5.9.207.224&mt_port=10110' \
  --data 'c1_type=tcp&c1_target=192.168.1.20:10110&c2_type=http' \
  --data-urlencode 'c2_target=https://feed.example.org:8443/ais?station=7&fmt=json' "$BASE/setup"
grep -q 'Joining your Wi-Fi' "$TMP/ok.html" || fail "submit page"
i=0; until grep -q 'Setup complete' "$TMP/state/setup.log" 2>/dev/null; do i=$((i+1)); [ $i -lt 60 ] || fail "install did not finish"; sleep 0.25; done
curl -fsS "$BASE/status" > "$TMP/status.html"
grep -q 'Done.' "$TMP/status.html" || fail "status not done"
# Exactly the installer's usage: [--private] MODE [--adsb-serial S] [--ais-serial S] [--lat --lon] [--yes] <key> [gain]
grep -q -- 'voydar-install.sh --both --adsb-serial 00001090 --ais-serial 00000162 --lat 51.90000 --lon 4.40000 --yes sbk_…(hidden) 33.8$' "$TMP/state/setup.log" || fail "installer args"

# Secrets never in logs or pages
for f in "$TMP/state/setup.log" "$TMP/server.log" "$TMP/status.html"; do
  ! grep -q "$KEY" "$f" || fail "station key leaked into $f"
  ! grep -q 'hunter2hunter2' "$f" || fail "wifi password leaked into $f"
  ! grep -q 'correct horse' "$f" || fail "ssh password leaked into $f"
done
# beacon.env is the installer/feeder contract: VOYDAR_KEY=<key>; the form's choices go elsewhere
grep -qx "VOYDAR_KEY=$KEY" "$TMP/state/beacon.env" || fail "env file"
grep -qx "VOYDAR_MODE=both" "$TMP/state/setup.env" || fail "setup.env"
! grep -q "$KEY" "$TMP/state/setup.env" || fail "key in setup.env"
for f in beacon.env setup.env; do
  perm="$(stat -c %a "$TMP/state/$f" 2>/dev/null || stat -f %Lp "$TMP/state/$f")"
  [ "$perm" = 600 ] || fail "$f mode $perm"
done
grep -q 'Voydar-Beacon-ABCD' "$TMP/state/voydar-hotspot.txt" || fail "hotspot label"
[ -f "$TMP/state/configured" ] || fail "not marked configured"

# outputs.conf: the installer v7 / voydar-outputs format, root only, kept + new, deduplicated, and
# nothing invalid carried over
cat > "$TMP/want.conf" <<'EOF'
# Voydar Beacon: extra AIS outputs (also feed other networks). Root only.
# One per line:  udp HOST PORT  |  tcp HOST PORT  |  http URL     (text after # is a comment)
# Change with: sudo voydar-outputs add|remove|list; the AIS container is recreated with them.
udp 5.9.207.224 10110  # MarineTraffic
udp data.aishub.net 2948  # AISHub
tcp 192.168.1.20 10110  # custom
http https://feed.example.org:8443/ais?station=7&fmt=json  # custom
EOF
cmp -s "$TMP/want.conf" "$TMP/state/outputs.conf" || { diff "$TMP/want.conf" "$TMP/state/outputs.conf" || true; fail "outputs.conf"; }
perm="$(stat -c %a "$TMP/state/outputs.conf" 2>/dev/null || stat -f %Lp "$TMP/state/outputs.conf")"
[ "$perm" = 600 ] || fail "outputs.conf mode $perm"
grep -q 'Also feeding 4 other outputs: MarineTraffic, AISHub, custom, custom' "$TMP/state/setup.log" || fail "outputs not logged"
! grep -q '2948' "$TMP/status.html" || fail "output ports shown on the status page"

# voydar-wifi.txt on the boot partition (--import-wifi): applied, then blanked; mistakes leave it alone
W="$TMP/wifi"; mkdir -p "$W"
cp "$TEMPLATE" "$W/voydar-wifi.txt"
python3 "$SETUP" --dry-run --import-wifi --state-dir "$W" > "$TMP/imp0.log" 2>&1 || fail "import of the empty template"
[ ! -f "$W/voydar-wifi-import.json" ] || fail "the empty template produced a status"
cmp -s "$TEMPLATE" "$W/voydar-wifi.txt" || fail "the empty template was rewritten"

# Saved by Windows Notepad: BOM, CRLF, spaces around =, quotes, a password with shell characters
printf '\357\273\277# from Windows\r\nssid = Home Net\r\npassword="s3cret pass $(reboot)"\r\ncountry=nl\r\nhotspot_password=boat-hotspot-42\r\n' > "$W/voydar-wifi.txt"
python3 "$SETUP" --dry-run --import-wifi --state-dir "$W" > "$TMP/imp1.log" 2>&1 || fail "import"
grep -q "would save Wi-Fi 'Home Net' (country NL)" "$W/setup.log" || fail "Wi-Fi from voydar-wifi.txt not saved"
cmp -s "$TEMPLATE" "$W/voydar-wifi.txt" || fail "voydar-wifi.txt not blanked back to the template"
grep -q '"ok": true' "$W/voydar-wifi-import.json" || fail "import status"
grep -qx 'boat-hotspot-42' "$W/hotspot.psk" || fail "hotspot password from voydar-wifi.txt"
perm="$(stat -c %a "$W/hotspot.psk" 2>/dev/null || stat -f %Lp "$W/hotspot.psk")"
[ "$perm" = 600 ] || fail "hotspot.psk mode $perm"
for f in "$W/setup.log" "$TMP/imp1.log" "$W/voydar-wifi-import.json"; do
  ! grep -q 's3cret' "$f" || fail "wifi password leaked into $f"
  ! grep -q 'boat-hotspot-42' "$f" || fail "hotspot password leaked into $f"
done

rm -f "$W/voydar-wifi-import.json"
printf 'ssid=Home\npassword=short\npasword=typo\n' > "$W/voydar-wifi.txt"
cp "$W/voydar-wifi.txt" "$TMP/bad-wifi.txt"
python3 "$SETUP" --dry-run --import-wifi --state-dir "$W" > "$TMP/imp2.log" 2>&1 || fail "import of a bad file"
cmp -s "$TMP/bad-wifi.txt" "$W/voydar-wifi.txt" || fail "a file with mistakes was changed"
grep -q '"ok": false' "$W/voydar-wifi-import.json" || fail "bad file status"
for m in 'line 3 has an unknown name' 'password must be 8 to 63' 'country must be'; do
  grep -q "$m" "$W/voydar-wifi-import.json" || fail "bad file: missing '$m'"
done
! grep -q 'short\|typo' "$W/voydar-wifi-import.json" || fail "bad file status quotes a value"

# The page shows the import result, and the hotspot uses the contributor's own password
python3 "$SETUP" --dry-run --port "$((PORT + 1))" --state-dir "$W" > "$TMP/server2.log" 2>&1 &
PID2=$!
i=0; until curl -fsS -o "$TMP/form2.html" "http://127.0.0.1:$((PORT + 1))/" 2>/dev/null; do i=$((i+1)); [ $i -lt 50 ] || { cat "$TMP/server2.log"; fail "second server"; }; sleep 0.2; done
grep -q 'voydar-wifi.txt was not used' "$TMP/form2.html" || fail "import problem not shown on the page"
grep -q 'password:  boat-hotspot-42' "$W/voydar-hotspot.txt" || fail "hotspot label does not use the chosen password"
grep -q 'Note: voydar-wifi.txt was not used' "$W/voydar-hotspot.txt" || fail "hotspot label lacks the import note"
echo "setup page dry-run: all checks passed"
