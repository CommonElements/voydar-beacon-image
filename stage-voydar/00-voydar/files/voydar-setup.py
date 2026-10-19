#!/usr/bin/env python3
"""Voydar Beacon first-boot setup page.

Runs on the Beacon SD-card image until the station is configured, then never again
(systemd: ConditionPathExists=!/etc/voydar/configured; create /boot/firmware/voydar-reset
to bring it back). Python 3 stdlib only.

  * With a network (Ethernet, or Wi-Fi saved earlier): serves http://voydar-beacon.local/ on the LAN.
  * Without one: starts a Wi-Fi hotspot "Voydar-Beacon-XXXX" (password on the label file
    /boot/firmware/voydar-hotspot.txt and on the HDMI console) and serves http://10.42.0.1/.

The form takes the station key, Wi-Fi, mode (AIS / ADS-B / both), optional position, optional
extra AIS outputs ("Also feed": AISHub, MarineTraffic, VesselFinder, custom UDP / TCP / HTTP) and an
optional SSH password; it then joins the Wi-Fi, stores the key in /etc/voydar/beacon.env (root,
0600), the extra outputs in /etc/voydar/outputs.conf (root, 0600) and runs the Beacon installer v7
embedded in the image (/opt/voydar/voydar-install.sh; never downloaded here), which starts
AIS-catcher with every output in that file.

Before first boot the contributor can fill in /boot/firmware/voydar-wifi.txt on any computer (home
Wi-Fi, country, optional hotspot password). voydar-wifi-import.service runs this script with
--import-wifi on every boot: it saves that Wi-Fi as a NetworkManager keyfile, joins it, and blanks
the values in the file so no password stays on the card. With home Wi-Fi known, a headless Pi Zero
2 W comes up on the LAN at http://voydar-beacon.local/ and never needs the hotspot.
Image 1.2.0 (installer v7) uses Voydar names everywhere on the device: /etc/voydar, /opt/voydar,
voydar-* services, containers and commands, and the local user voydar. Stations set up by image
1.0 / 1.1 (seadar names) keep working; rerunning installer v7 moves them to these names.

Security: IPv4 only; answers only clients on private/link-local addresses; Host header must be
this device (DNS-rebinding guard); one-time CSRF token per form; strict CSP; never logs the key,
Wi-Fi password or SSH password (every log line is redacted before it is stored or shown).

  --dry-run      touch nothing on the system: fake dongles, fake network, fake installer.
                 Binds 127.0.0.1 on --port (default 8080). For local testing. Every file this
                 script would read or write (boot partition, /etc/voydar) lives in --state-dir.
  --import-wifi  read voydar-wifi.txt from the boot partition, apply it, blank it, exit.
"""

import argparse
import html
import ipaddress
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOSTNAME = "voydar-beacon"
HOTSPOT_IP = "10.42.0.1"
HOTSPOT_CON = "Voydar-Hotspot"
WIFI_CON = "voydar-wifi"
ETC = "/etc/voydar"
CONFIGURED = ETC + "/configured"
ENV_FILE = ETC + "/beacon.env"  # installer/feeder contract: VOYDAR_KEY=sbk_...
SETUP_ENV_FILE = ETC + "/setup.env"  # what the form chose (no secrets), for support and re-runs
OUTPUTS_FILE = ETC + "/outputs.conf"  # installer v7 / voydar-outputs contract: extra AIS outputs
HOTSPOT_PSK_FILE = ETC + "/hotspot.psk"
BOOT = "/boot/firmware"
LABEL_FILE = BOOT + "/voydar-hotspot.txt"
WIFI_FILE = BOOT + "/voydar-wifi.txt"  # filled in by the contributor before first boot (optional)
WIFI_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voydar-wifi.txt")
WIFI_STATUS_FILE = "/run/voydar-wifi-import.json"  # what --import-wifi did this boot (no secrets)
OPT = "/opt/voydar"
INSTALLER = OPT + "/voydar-install.sh"
FEEDER = OPT + "/adsb-feed-v3.sh"
IMAGES_DIR = OPT + "/images"
PRELOAD_DONE = "/var/lib/voydar/preload.done"
LOG_FILE = "/var/log/voydar-setup.log"
ISSUE_FILE = "/etc/issue.d/voydar-beacon.issue"
SSH_USER = "voydar"

KEY_RE = re.compile(r"^sbk_[0-9a-f]{48}$")
ANY_KEY_RE = re.compile(r"sbk_[0-9a-f]{8,}")
SERIAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
GAIN_RE = re.compile(r"^(auto|[0-9]{1,2}(\.[0-9])?)$")
# Extra AIS outputs: the same rules as voydar-outputs (installer v7), checked with fullmatch.
HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)(?::([0-9]+))?(/[A-Za-z0-9._~%/?=&+:,-]*)?")
LABEL_RE = re.compile(r"[A-Za-z0-9 ._-]{1,32}")
MAX_OUTPUTS = 8
# (form field prefix, name, where the contributor finds host and port). All three take NMEA over UDP.
OUTPUT_PRESETS = (
    ("aishub", "AISHub", "the host and UDP port in your AISHub station email"),
    ("mt", "MarineTraffic", "the IP and UDP port MarineTraffic gave your station"),
    ("vf", "VesselFinder", "the host and UDP port VesselFinder gave your station"),
)
CUSTOM_OUTPUTS = 2
WIFI_KEYS = ("ssid", "password", "country", "hotspot_password")
RTL_IDS = {("0bda", "2838"), ("0bda", "2832")}
MAX_BODY = 16 * 1024
TOKEN_TTL = 3600
DONE_GRACE_S = 600  # after success on the LAN, keep showing the result page this long, then exit

# ---------------------------------------------------------------------------------------------
# Redaction: every line that reaches the log file, the journal or the status page goes through here.

_secrets_lock = threading.Lock()
_secrets = set()


def add_secret(s):
    if s and len(s) >= 4:
        with _secrets_lock:
            _secrets.add(s)


def redact(line):
    line = ANY_KEY_RE.sub("sbk_…(hidden)", line)
    with _secrets_lock:
        for s in sorted(_secrets, key=len, reverse=True):
            line = line.replace(s, "(hidden)")
    return line


# ---------------------------------------------------------------------------------------------
# Extra AIS outputs (/etc/voydar/outputs.conf). Format, shared with voydar-outputs in installer v7:
# one output per line, "udp HOST PORT", "tcp HOST PORT" or "http URL"; text after # is a comment.
# Values are checked here and again by voydar-outputs, and only ever reach AIS-catcher as separate
# docker arguments: nothing in the file is run by a shell.


def valid_host(h):
    return bool(h) and len(h) <= 253 and HOST_RE.fullmatch(h) is not None


def valid_port(p):
    return bool(re.fullmatch(r"[1-9][0-9]{0,4}", p or "")) and int(p) <= 65535


def valid_url(u):
    m = URL_RE.fullmatch(u or "")
    if not m or len(u) > 512:
        return False
    return valid_host(m.group(1)) and (m.group(2) is None or valid_port(m.group(2)))


def parse_output_line(line):
    """(type, a, b, label) for a valid line, None for a blank/comment or invalid one."""
    body, _, comment = line.partition("#")
    parts = body.split()
    label = comment.strip()
    label = label if LABEL_RE.fullmatch(label) else ""
    if len(parts) == 3 and parts[0] in ("udp", "tcp") and valid_host(parts[1]) and valid_port(parts[2]):
        return (parts[0], parts[1], parts[2], label)
    if len(parts) == 2 and parts[0] == "http" and valid_url(parts[1]):
        return ("http", parts[1], "", label)
    return None


def parse_outputs(text):
    out, seen = [], set()
    for line in (text or "").splitlines():
        o = parse_output_line(line)
        if o and o[:3] not in seen and len(out) < MAX_OUTPUTS:
            seen.add(o[:3])
            out.append(o)
    return out


def output_line(o):
    t, a, b, label = o
    return "%s %s%s%s" % (t, a, " " + b if b else "", "  # " + label if label else "")


def outputs_text(outputs):
    return ("# Voydar Beacon: extra AIS outputs (also feed other networks). Root only.\n"
            "# One per line:  udp HOST PORT  |  tcp HOST PORT  |  http URL     (text after # is a comment)\n"
            "# Change with: sudo voydar-outputs add|remove|list; the AIS container is recreated with them.\n"
            + "".join(output_line(o) + "\n" for o in outputs))


# ---------------------------------------------------------------------------------------------
# voydar-wifi.txt on the boot partition: plain name=value lines the contributor fills in before
# first boot. Read on every boot by --import-wifi, then blanked.


def parse_wifi_file(text):
    """(settings, errors). settings holds only the names that have a value; errors never quote a value."""
    if text.startswith("\ufeff"):
        text = text[1:]
    vals, errors = {}, []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append("line %d is not name=value" % n)
            continue
        k, val = line.split("=", 1)
        k, val = k.strip().lower(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] == '"':
            val = val[1:-1]
        if k not in WIFI_KEYS:
            errors.append("line %d has an unknown name (use ssid, password, country, hotspot_password)" % n)
            continue
        vals[k] = val
    settings = {k: v for k, v in vals.items() if v}
    if "country" in settings:
        settings["country"] = settings["country"].upper()

    def ascii_pw(pw):
        return 8 <= len(pw) <= 63 and all(32 <= ord(c) <= 126 for c in pw)

    if settings.get("ssid"):
        if len(settings["ssid"].encode("utf-8")) > 32:
            errors.append("ssid is longer than 32 bytes")
        if settings.get("password") and not ascii_pw(settings["password"]):
            errors.append("password must be 8 to 63 ordinary characters (leave it empty for an open network)")
        if not COUNTRY_RE.match(settings.get("country", "")):
            errors.append("country must be your 2-letter country code, for example US, GB or NL")
    elif settings.get("password") or settings.get("country"):
        errors.append("ssid (your Wi-Fi name) is missing")
    if settings.get("hotspot_password") and not ascii_pw(settings["hotspot_password"]):
        errors.append("hotspot_password must be 8 to 63 ordinary characters")
    return settings, errors


def blank_file(path, text):
    """Overwrite a file's old bytes in place (best effort on FAT), then give it new contents."""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as fh:
            fh.write(b" " * size)
            fh.flush()
            os.fsync(fh.fileno())
            fh.seek(0)
            fh.truncate()
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass


def wifi_template():
    try:
        with open(WIFI_TEMPLATE, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return "# Voydar Beacon Wi-Fi settings (read on boot)\nssid=\npassword=\ncountry=\nhotspot_password=\n"


# ---------------------------------------------------------------------------------------------
# System access. RealSystem changes the Pi; DrySystem only pretends.


def run(cmd, timeout=60, input_text=None, check=False):
    """Run a command without a shell. Never pass secrets on argv (they would show in ps)."""
    p = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("%s failed (%d): %s" % (cmd[0], p.returncode, redact(p.stderr.strip())[:300]))
    return p


def gkeyfile_escape(v):
    out = v.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    if out.startswith(" "):
        out = "\\s" + out[1:]
    return out


class RealSystem:
    dry = False

    def path(self, p):
        """Where a system file lives (DrySystem maps every path into --state-dir)."""
        return p

    def read_text(self, p):
        try:
            with open(self.path(p), encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def write_private(self, p, text):
        """Write a root-only (0600) file atomically."""
        p = self.path(p)
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(p + ".tmp", 0o600)
        os.replace(p + ".tmp", p)

    def set_hotspot_psk(self, psk):
        self.write_private(HOTSPOT_PSK_FILE, psk + "\n")

    def wifi_status(self):
        try:
            with open(self.path(WIFI_STATUS_FILE)) as fh:
                d = json.load(fh)
            return {"ok": bool(d.get("ok")), "message": str(d.get("message", ""))}
        except (OSError, ValueError, AttributeError):
            return None

    def write_wifi_status(self, ok, message):
        try:
            with open(self.path(WIFI_STATUS_FILE), "w") as fh:
                json.dump({"ok": ok, "message": redact(message)}, fh)
        except OSError:
            pass

    def log(self, msg):
        line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), redact(msg))
        print(line, flush=True)
        try:
            fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def console(self, text):
        """Show a message on the HDMI console (tty1) and in the pre-login banner."""
        try:
            os.makedirs("/etc/issue.d", exist_ok=True)
            with open(ISSUE_FILE, "w") as fh:
                fh.write(text.replace("\\", "\\\\") + "\n")
        except OSError:
            pass
        try:
            with open("/dev/tty1", "w") as tty:
                tty.write("\n" + text + "\n")
        except OSError:
            pass

    def detect_sdrs(self):
        found = []
        base = "/sys/bus/usb/devices"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return found
        for name in names:
            d = os.path.join(base, name)

            def rd(f):
                try:
                    with open(os.path.join(d, f)) as fh:
                        return fh.read().strip()
                except OSError:
                    return ""

            if (rd("idVendor"), rd("idProduct")) in RTL_IDS:
                serial = rd("serial")
                found.append({"serial": serial if SERIAL_RE.match(serial or "") else "", "product": rd("product") or "RTL-SDR"})
        return found

    def has_network(self):
        p = run(["ip", "-4", "route", "show", "default"], timeout=10)
        routes = [l for l in p.stdout.splitlines() if l.strip()]
        # A default route through our own hotspot does not count (it has none, but be explicit).
        return any(HOTSPOT_IP not in l for l in routes)

    def online(self):
        p = run(["nmcli", "-t", "networking", "connectivity", "check"], timeout=30)
        return p.stdout.strip() in ("full", "portal", "limited")

    def lan_ips(self):
        p = run(["hostname", "-I"], timeout=10)
        return [a for a in p.stdout.split() if ":" not in a and a != HOTSPOT_IP]

    def wlan_mac(self):
        try:
            with open("/sys/class/net/wlan0/address") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def has_wifi(self):
        return os.path.exists("/sys/class/net/wlan0")

    def hotspot_psk(self):
        try:
            with open(HOTSPOT_PSK_FILE) as fh:
                psk = fh.read().strip()
                if len(psk) >= 8:
                    return psk
        except OSError:
            pass
        alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to read off a screen
        psk = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
        os.makedirs(ETC, mode=0o700, exist_ok=True)
        fd = os.open(HOTSPOT_PSK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(psk + "\n")
        return psk

    def write_label(self, text):
        try:
            with open(LABEL_FILE, "w") as fh:
                fh.write(text + "\n")
        except OSError as e:
            self.log("could not write %s: %s" % (LABEL_FILE, e))

    def scan_ssids(self):
        run(["rfkill", "unblock", "wlan"], timeout=10)
        run(["nmcli", "radio", "wifi", "on"], timeout=10)
        p = run(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "yes"], timeout=30)
        seen = []
        for line in p.stdout.splitlines():
            s = line.replace("\\:", ":").replace("\\\\", "\\").strip()
            if s and s not in seen:
                seen.append(s)
        return seen[:40]

    def start_hotspot(self, ssid, psk):
        run(["rfkill", "unblock", "wlan"], timeout=10)
        run(["nmcli", "radio", "wifi", "on"], timeout=10)
        run(["nmcli", "connection", "delete", HOTSPOT_CON], timeout=20)
        # 2.4 GHz channel 6 is usable in every regulatory domain, including the world domain a
        # fresh card starts in; the contributor's country is set when they submit the form.
        run(["nmcli", "device", "wifi", "hotspot", "ifname", "wlan0", "con-name", HOTSPOT_CON,
             "ssid", ssid, "band", "bg", "channel", "6", "password", psk], timeout=40, check=True)
        run(["nmcli", "connection", "modify", HOTSPOT_CON,
             "connection.autoconnect", "no",
             "ipv4.method", "shared", "ipv4.addresses", HOTSPOT_IP + "/24",
             "ipv6.method", "disabled",
             "802-11-wireless-security.proto", "rsn",
             "802-11-wireless-security.pairwise", "ccmp",
             "802-11-wireless-security.group", "ccmp",
             "802-11-wireless-security.pmf", "disable"], timeout=20)
        run(["nmcli", "connection", "up", HOTSPOT_CON], timeout=40, check=True)

    def stop_hotspot(self, delete=False):
        run(["nmcli", "connection", "down", HOTSPOT_CON], timeout=20)
        if delete:
            run(["nmcli", "connection", "delete", HOTSPOT_CON], timeout=20)

    def hotspot_active(self):
        p = run(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"], timeout=10)
        return HOTSPOT_CON in p.stdout.splitlines()

    def save_wifi(self, ssid, psk, country):
        """Save the contributor's Wi-Fi as a NetworkManager keyfile (secrets never on argv)."""
        run(["raspi-config", "nonint", "do_wifi_country", country], timeout=30)
        lines = [
            "[connection]", "id=" + WIFI_CON, "uuid=" + str(uuid.uuid4()), "type=wifi",
            "interface-name=wlan0", "autoconnect=true", "autoconnect-priority=10", "",
            "[wifi]", "mode=infrastructure",
            # SSID as a byte list: exact for any characters, including ';' and non-ASCII.
            "ssid=" + "".join("%d;" % b for b in ssid.encode("utf-8")), "",
        ]
        if psk:
            lines += ["[wifi-security]", "key-mgmt=wpa-psk", "psk=" + gkeyfile_escape(psk), ""]
        lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
        path = "/etc/NetworkManager/system-connections/%s.nmconnection" % WIFI_CON
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines))
        run(["nmcli", "connection", "reload"], timeout=20, check=True)

    def apply_wifi(self, ssid, psk, country):
        """Save the Wi-Fi and join it now (the hotspot goes down first: one radio)."""
        self.save_wifi(ssid, psk, country)
        self.stop_hotspot()
        p = run(["nmcli", "--wait", "60", "connection", "up", WIFI_CON], timeout=90)
        if p.returncode != 0:
            return False
        for _ in range(30):
            if self.has_network() and self.online():
                return True
            time.sleep(2)
        return False

    def join_saved_wifi(self, timeout=120):
        """At boot (--import-wifi): NetworkManager may still be bringing wlan0 up, so wait for it to
        autoconnect, nudging it once after 15 s. True when the Beacon is online."""
        start = time.time()
        nudged = False
        while time.time() - start < timeout:
            if self.has_network() and self.online():
                return True
            if not nudged and time.time() - start > 15:
                run(["nmcli", "--wait", "45", "connection", "up", WIFI_CON], timeout=60)
                nudged = True
            time.sleep(3)
        return False

    def enable_ssh(self, password):
        run(["chpasswd"], input_text="%s:%s\n" % (SSH_USER, password), timeout=20, check=True)
        run(["usermod", "-U", SSH_USER], timeout=20)
        run(["systemctl", "enable", "--now", "ssh"], timeout=60, check=True)

    def write_env(self, path, values, header):
        os.makedirs(ETC, mode=0o700, exist_ok=True)
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(env_text(values, header))
        os.chmod(path + ".tmp", 0o600)
        os.replace(path + ".tmp", path)

    def wait_preload(self, log):
        """The shipped container images are loaded by voydar-preload.service; wait for it."""
        deadline = time.time() + 2700  # voydar-preload.service allows 45 minutes (Pi Zero 2 W)
        announced = False
        while time.time() < deadline:
            if os.path.exists(PRELOAD_DONE) or not any(f.endswith(".tar") for f in _listdir(IMAGES_DIR)):
                return
            if not announced:
                log("Unpacking the receiver software shipped on the card (a few minutes on first boot)")
                announced = True
            time.sleep(5)
        log("Image preload did not finish; the installer will download what it needs")

    def run_installer(self, args, env, log):
        if not os.path.exists(INSTALLER):
            log("ERROR: the installer is missing from this image (%s)" % INSTALLER)
            return 1
        full_env = dict(os.environ)
        full_env.update(env)
        p = subprocess.Popen(["sh", INSTALLER] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=full_env, cwd=OPT)
        for line in p.stdout:
            line = line.rstrip()
            if line:
                log(line)
        return p.wait()

    def mark_configured(self):
        os.makedirs(ETC, mode=0o700, exist_ok=True)
        with open(CONFIGURED, "w") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
        try:
            os.remove(ISSUE_FILE)
        except OSError:
            pass


def env_text(values, header):
    """KEY=value lines, shell-quoted (systemd EnvironmentFile and sh both read them)."""
    return "# %s\n" % header + "".join("%s=%s\n" % (k, shlex.quote(v)) for k, v in values.items())


def _listdir(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


class DrySystem(RealSystem):
    """Pretends to be a Pi with two dongles and no network. Touches nothing outside --state-dir."""

    dry = True

    def __init__(self, state_dir):
        self.state = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self._net = False
        self._hotspot = False

    def path(self, p):
        return os.path.join(self.state, os.path.basename(p))

    def log(self, msg):
        line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), redact(msg))
        print(line, flush=True)
        with open(os.path.join(self.state, "setup.log"), "a") as fh:
            fh.write(line + "\n")

    def console(self, text):
        self.log("[console] " + text.replace("\n", " | "))

    def detect_sdrs(self):
        return [{"serial": "00001090", "product": "RTL2838UHIDIR"}, {"serial": "00000162", "product": "RTL2838UHIDIR"}]

    def has_network(self):
        return self._net

    def online(self):
        return self._net

    def lan_ips(self):
        return ["127.0.0.1"]

    def wlan_mac(self):
        return "dc:a6:32:12:ab:cd"

    def has_wifi(self):
        return True

    def hotspot_psk(self):
        psk = (self.read_text(HOTSPOT_PSK_FILE) or "").strip()
        return psk if len(psk) >= 8 else "abcd-efgh-jkmn"

    def write_label(self, text):
        with open(os.path.join(self.state, "voydar-hotspot.txt"), "w") as fh:
            fh.write(text + "\n")

    def scan_ssids(self):
        return ["HomeNet", "Marina Guest", "café;wifi"]

    def start_hotspot(self, ssid, psk):
        self._hotspot = True
        self.log("[dry-run] would start hotspot %s" % ssid)

    def stop_hotspot(self, delete=False):
        self._hotspot = False

    def hotspot_active(self):
        return self._hotspot

    def apply_wifi(self, ssid, psk, country):
        self.log("[dry-run] would save Wi-Fi %r (country %s) and join it" % (ssid, country))
        time.sleep(1)
        self._net = True
        return True

    def save_wifi(self, ssid, psk, country):
        self.log("[dry-run] would save Wi-Fi %r (country %s)" % (ssid, country))

    def join_saved_wifi(self, timeout=120):
        self._net = True
        return True

    def enable_ssh(self, password):
        self.log("[dry-run] would set the %s password and enable ssh" % SSH_USER)

    def write_env(self, path, values, header):
        path = os.path.join(self.state, os.path.basename(path))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(env_text(values, header))

    def wait_preload(self, log):
        return

    def run_installer(self, args, env, log):
        shown = " ".join(shlex.quote(a) for a in args)
        log("[dry-run] would run: sh %s %s" % (INSTALLER, shown))
        for step in ("Looking for RTL-SDR dongles", "Starting containers", "Running."):
            time.sleep(0.5)
            log("==> " + step)
        return 0

    def mark_configured(self):
        with open(os.path.join(self.state, "configured"), "w") as fh:
            fh.write("dry-run\n")


# ---------------------------------------------------------------------------------------------
# Setup state shared between the HTTP handler and the worker thread.


class State:
    def __init__(self, system, mode, base_url, allowed_hosts):
        self.sys = system
        self.mode = mode  # "hotspot" | "lan"
        self.base_url = base_url
        self.allowed_hosts = allowed_hosts
        self.lock = threading.Lock()
        self.tokens = {}
        self.phase = "form"  # form | running | done | failed
        self.lines = []
        self.message = ""
        self.ssids = []
        self.done_at = None

    def new_token(self):
        t = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.tokens = {k: v for k, v in self.tokens.items() if v > now}
            if len(self.tokens) > 50:
                self.tokens.pop(next(iter(self.tokens)))
            self.tokens[t] = now + TOKEN_TTL
        return t

    def use_token(self, t):
        with self.lock:
            exp = self.tokens.pop(t, None)
        return exp is not None and exp > time.time()

    def log(self, msg):
        msg = redact(msg)
        self.sys.log(msg)
        with self.lock:
            self.lines.append(msg)
            self.lines = self.lines[-200:]


# ---------------------------------------------------------------------------------------------
# HTML. Plain, self-contained, no scripts, no external assets.

CSS = """
:root{--bg:#F3F5F4;--surface:#FFFFFF;--line:#D5DBDA;--text:#14202B;--muted:#4A5560;--primary:#1F5FCC;
--on-primary:#FFFFFF;--live:#1F7A3A;--error:#B42318;--soft:#E6EEFB}
@media (prefers-color-scheme:dark){:root{--bg:#0C1114;--surface:#12181C;--line:#27323A;--text:#E8E6DE;
--muted:#A3ADB3;--primary:#7FAEFF;--on-primary:#0C1114;--live:#8BEA4A;--error:#FF8A80;--soft:#16253D}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:560px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;margin:0 0 4px;letter-spacing:-.01em}p.lead{color:var(--muted);margin:0 0 20px}
fieldset{border:1px solid var(--line);border-radius:6px;background:var(--surface);padding:14px 16px;margin:0 0 16px}
legend{font-size:.72rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:0 4px;color:var(--muted)}
label{display:block;font-weight:500;margin:10px 0 4px}small,.hint{color:var(--muted);font-size:.85rem}
input[type=text],input[type=password],input[type=number],select{width:100%;height:40px;padding:0 10px;
border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--text);font:inherit}
input.mono{font-family:ui-monospace,"JetBrains Mono",Menlo,monospace;font-size:.9rem}
.radio{display:flex;gap:8px;align-items:flex-start;margin:6px 0}.radio input{margin-top:5px}
button{height:44px;padding:0 20px;border:0;border-radius:4px;background:var(--primary);color:var(--on-primary);
font:inherit;font-weight:600;cursor:pointer;width:100%}
.err{border-left:3px solid var(--error);padding:8px 12px;background:var(--surface);margin:0 0 16px}
.ok{border-left:3px solid var(--live);padding:8px 12px;background:var(--surface);margin:0 0 16px}
pre{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:12px;overflow-x:auto;
font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
code{font-family:ui-monospace,Menlo,monospace}.row{display:flex;gap:12px}.row>div{flex:1}
"""


def page(title, body, refresh=None):
    meta = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ""
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>%s"
            "<title>%s</title><style>%s</style></head><body><main>%s</main></body></html>"
            % (meta, html.escape(title), CSS, body))


def e(s):
    return html.escape(str(s), quote=True)


def render_form(st, token, errors=None, v=None):
    v = v or {}
    sdrs = st.sys.detect_sdrs()
    serials = [d["serial"] for d in sdrs if d["serial"]]
    errs = "".join("<div class=err>%s</div>" % e(x) for x in (errors or []))
    if sdrs:
        dongles = "<p class=hint>Found %d RTL-SDR dongle%s: %s</p>" % (
            len(sdrs), "" if len(sdrs) == 1 else "s",
            ", ".join("%s <code>%s</code>" % (e(d["product"]), e(d["serial"] or "no serial")) for d in sdrs))
    else:
        dongles = "<p class=hint>No RTL-SDR dongle found yet. Plug it in and reload this page; you can also continue and plug it in later.</p>"

    def sel(name, current):
        opts = ['<option value="">Automatic</option>'] + [
            '<option value="%s"%s>%s</option>' % (e(s), " selected" if s == current else "", e(s)) for s in serials]
        return '<select name="%s">%s</select>' % (name, "".join(opts))

    mode = v.get("mode", "ais")

    def radio(val, label, hint):
        return ('<div class=radio><input type=radio id=m_%s name=mode value=%s%s><label for=m_%s style="margin:0">'
                '%s<br><small>%s</small></label></div>' % (val, val, " checked" if mode == val else "", val, label, hint))

    ssid_list = "".join('<option value="%s">' % e(s) for s in st.ssids)
    net_note = ("This Beacon is on its own hotspot. Enter the Wi-Fi it should use from now on."
                if st.mode == "hotspot" else
                "This Beacon already has a network connection. Wi-Fi is optional (leave it empty to stay on the current network).")
    ws = st.sys.wifi_status()
    if ws and ws["message"]:
        net_note_html = "<div class=%s>%s</div>" % ("ok" if ws["ok"] else "err", e(ws["message"]))
    else:
        net_note_html = ""

    # Also feed: presets (UDP host + port each), custom slots (type + target), keep what is there.
    existing = parse_outputs(st.sys.read_text(OUTPUTS_FILE))
    rows = []
    if existing:
        keep = v.get("keep_outputs", "1")
        rows.append('<div class=radio><input type=checkbox id=keep_outputs name=keep_outputs value=1%s>'
                    '<label for=keep_outputs style="margin:0">Keep the outputs already on this Beacon<br><small>%s</small>'
                    '</label></div>' % (" checked" if keep else "", "<br>".join(e(output_line(o)) for o in existing)))
    for pre, name, where in OUTPUT_PRESETS:
        rows.append('<label for=%(p)s_host>%(n)s (UDP)</label><div class=row><div><input class=mono type=text id=%(p)s_host '
                    'name=%(p)s_host maxlength=253 autocapitalize=off spellcheck=false placeholder="Host or IP" value="%(h)s">'
                    '</div><div style="flex:0 0 110px"><input class=mono type=text name=%(p)s_port maxlength=5 inputmode=numeric '
                    'placeholder="Port" aria-label="%(n)s port" value="%(pt)s"></div></div><small>Use %(w)s.</small>'
                    % {"p": pre, "n": e(name), "w": e(where), "h": e(v.get(pre + "_host", "")), "pt": e(v.get(pre + "_port", ""))})
    for i in range(1, CUSTOM_OUTPUTS + 1):
        cur = v.get("c%d_type" % i, "udp")
        opts = "".join('<option value=%s%s>%s</option>' % (t, " selected" if cur == t else "", t.upper()) for t in ("udp", "tcp", "http"))
        rows.append('<label for=c%(i)d_target>Custom output %(i)d</label><div class=row><div style="flex:0 0 100px">'
                    '<select name=c%(i)d_type aria-label="Custom output %(i)d type">%(o)s</select></div><div>'
                    '<input class=mono type=text id=c%(i)d_target name=c%(i)d_target maxlength=512 autocapitalize=off spellcheck=false '
                    'placeholder="host:port, or URL for HTTP" value="%(t)s"></div></div>'
                    % {"i": i, "o": opts, "t": e(v.get("c%d_target" % i, ""))})
    outputs_html = "".join(rows)
    body = """
<h1>Set up your Voydar Beacon</h1>
<p class=lead>Paste the station key from <b>voydar.com/contribute</b>, connect the Beacon to your network and choose what it receives.</p>
%(errs)s
<form method=post action="/setup" autocomplete=off>
<input type=hidden name=csrf value="%(token)s">
<fieldset><legend>Station</legend>
<label for=key>Station key</label>
<input class=mono type=text id=key name=key required pattern="sbk_[0-9a-f]{48}" spellcheck=false autocapitalize=off
 placeholder="sbk_…" value="%(key)s">
<small>Starts with <code>sbk_</code>. It is stored only on this device (readable by root only) and sent only to Voydar.</small>
<div class=radio><input type=checkbox id=private name=private value=1%(private)s><label for=private style="margin:0">
Private feed<br><small>Only you see this receiver's data (AIS only; needs a plan with private feeds).</small></label></div>
</fieldset>
<fieldset><legend>Network</legend>
%(net_note_html)s<p class=hint style="margin-top:0">%(net_note)s</p>
<label for=ssid>Wi-Fi name (SSID)</label>
<input type=text id=ssid name=ssid list=ssids maxlength=32 autocapitalize=off spellcheck=false value="%(ssid)s">
<datalist id=ssids>%(ssid_list)s</datalist>
<label for=wifipw>Wi-Fi password</label>
<input type=password id=wifipw name=wifipw maxlength=63 autocomplete=new-password>
<small>Leave empty for an open network.</small>
<label for=country>Country (2 letters, sets the legal Wi-Fi channels)</label>
<input type=text id=country name=country maxlength=2 pattern="[A-Za-z]{2}" placeholder="US" value="%(country)s">
</fieldset>
<fieldset><legend>What to receive</legend>
%(dongles)s
%(r_ais)s%(r_adsb)s%(r_both)s
<div class=row><div><label>AIS dongle</label>%(ais_sel)s</div><div><label>ADS-B dongle</label>%(adsb_sel)s</div></div>
<small>For "both", use two dongles and pick which one has the AIS antenna (162 MHz) and which the ADS-B antenna (1090 MHz).</small>
<label for=gain>AIS tuner gain</label>
<input type=text id=gain name=gain placeholder="auto" maxlength=4 value="%(gain)s">
<div class=row><div><label for=lat>Latitude (ADS-B, optional)</label>
<input type=text id=lat name=lat inputmode=decimal placeholder="51.5074" value="%(lat)s"></div>
<div><label for=lon>Longitude</label><input type=text id=lon name=lon inputmode=decimal placeholder="-0.1278" value="%(lon)s"></div></div>
<small>The antenna position helps decode aircraft without a position. It stays on the device and is used only for decoding.</small>
</fieldset>
<fieldset><legend>Also feed (optional, AIS)</legend>
<p class=hint style="margin-top:0">Keep feeding other AIS networks from this receiver. Enter the host and port each one gave you;
leave a row empty to skip it. Later: <code>sudo voydar-outputs add udp HOST PORT</code> (also <code>list</code>, <code>remove</code>).</p>
%(outputs)s
</fieldset>
<fieldset><legend>Remote access (optional)</legend>
<div class=radio><input type=checkbox id=ssh name=ssh value=1%(ssh)s><label for=ssh style="margin:0">Turn on SSH
<br><small>Log in as <code>voydar</code> with the password below. Off by default; leave it off unless you need it.</small></label></div>
<label for=sshpw>SSH password (12 characters or more)</label>
<input type=password id=sshpw name=sshpw maxlength=128 autocomplete=new-password>
</fieldset>
<button type=submit>Save and install</button>
</form>
""" % {
        "errs": errs, "token": e(token), "key": e(v.get("key", "")), "private": " checked" if v.get("private") else "",
        "net_note": e(net_note), "net_note_html": net_note_html, "outputs": outputs_html, "ssid": e(v.get("ssid", "")), "ssid_list": ssid_list,
        "country": e(v.get("country", "")), "dongles": dongles,
        "r_ais": radio("ais", "AIS (ships)", "One dongle with a marine VHF antenna."),
        "r_adsb": radio("adsb", "ADS-B (aircraft)", "One dongle with a 1090 MHz antenna."),
        "r_both": radio("both", "Both", "Two dongles."),
        "ais_sel": sel("ais_serial", v.get("ais_serial", "")), "adsb_sel": sel("adsb_serial", v.get("adsb_serial", "")),
        "gain": e(v.get("gain", "")), "lat": e(v.get("lat", "")), "lon": e(v.get("lon", "")),
        "ssh": " checked" if v.get("ssh") else "",
    }
    return page("Voydar Beacon setup", body)


def render_status(st):
    with st.lock:
        phase, lines, message = st.phase, list(st.lines), st.message
    if phase == "done":
        head = "<div class=ok><b>Done.</b> Your Beacon is running. Its station page on voydar.com turns green when the first message arrives. This setup page now switches itself off.</div>"
        refresh = None
    elif phase == "failed":
        head = "<div class=err><b>Setup did not finish.</b> %s</div><p><a href=\"/\">Back to the form</a></p>" % e(message)
        refresh = None
    else:
        head = "<p class=lead>Installing. This page refreshes itself; it can take several minutes.</p>"
        refresh = 3
    body = "<h1>Voydar Beacon</h1>%s<pre>%s</pre>" % (head, e("\n".join(lines[-80:])) or "…")
    return page("Voydar Beacon setup", body, refresh)


# ---------------------------------------------------------------------------------------------
# Validation


def validate(form, st):
    g = lambda k: (form.get(k, [""])[0] or "").strip()
    v = {k: g(k) for k in ("key", "ssid", "country", "mode", "ais_serial", "adsb_serial", "gain", "lat", "lon", "private", "ssh")}
    v["country"] = v["country"].upper()
    wifipw = form.get("wifipw", [""])[0] or ""
    sshpw = form.get("sshpw", [""])[0] or ""
    errors = []
    if not KEY_RE.match(v["key"]):
        errors.append("That is not a station key. It starts with sbk_ followed by 48 characters (0-9, a-f).")
    if v["ssid"]:
        if len(v["ssid"].encode("utf-8")) > 32:
            errors.append("The Wi-Fi name is longer than 32 bytes.")
        if wifipw and not (8 <= len(wifipw) <= 63 and all(32 <= ord(c) <= 126 for c in wifipw)):
            errors.append("A Wi-Fi password is 8 to 63 ordinary characters.")
        if not COUNTRY_RE.match(v["country"]):
            errors.append("Enter the 2-letter country code (for example US, GB, NL) so the Wi-Fi uses legal channels.")
    elif st.mode == "hotspot" and not st.sys.has_network():
        errors.append("Enter the Wi-Fi the Beacon should join (or plug in an Ethernet cable and reload).")
    if v["mode"] not in ("ais", "adsb", "both"):
        errors.append("Choose AIS, ADS-B or both.")
    for k in ("ais_serial", "adsb_serial"):
        if v[k] and not SERIAL_RE.match(v[k]):
            errors.append("Unrecognised dongle serial.")
    if v["private"] and v["mode"] in ("adsb", "both"):
        # Installer v7 (like v5) refuses this (the server answers 409 for private ADS-B).
        errors.append("Private feeds are AIS only for now. Untick Private feed, or choose AIS.")
    if v["mode"] == "both":
        sdrs = st.sys.detect_sdrs()
        serials = [d["serial"] for d in sdrs if d["serial"]]
        if len(sdrs) < 2:
            errors.append("Both needs two RTL-SDR dongles; %d found. Plug in both and reload this page." % len(sdrs))
        elif len(set(serials)) < len(sdrs):
            errors.append("Your dongles share a serial number (or have none), so they cannot be told apart. "
                          "Give one a new serial with rtl_eeprom -s 1090 on another computer, or use one mode per Beacon.")
        elif not (v["ais_serial"] and v["adsb_serial"]):
            errors.append("For both, choose which dongle is on the AIS antenna and which on the ADS-B antenna.")
        elif v["ais_serial"] == v["adsb_serial"]:
            errors.append("AIS and ADS-B need two different dongles.")
    if v["gain"] and not GAIN_RE.match(v["gain"]):
        errors.append("Gain is auto or a number such as 33.8.")
    for k, lo, hi in (("lat", -90, 90), ("lon", -180, 180)):
        if v[k]:
            try:
                f = float(v[k])
                if not (lo <= f <= hi):
                    raise ValueError
                v[k] = "%.5f" % f
            except ValueError:
                errors.append("%s must be a number between %d and %d." % ("Latitude" if k == "lat" else "Longitude", lo, hi))
    if bool(v["lat"]) != bool(v["lon"]):
        errors.append("Give both latitude and longitude, or neither.")
    if v["ssh"] and len(sshpw) < 12:
        errors.append("The SSH password must be at least 12 characters.")
    for k in [p + s for p, _, _ in OUTPUT_PRESETS for s in ("_host", "_port")] + \
            [("c%d_" % i) + s for i in range(1, CUSTOM_OUTPUTS + 1) for s in ("type", "target")] + ["keep_outputs"]:
        v[k] = g(k)
    v["outputs"], out_errors = validate_outputs(v, st)
    errors += out_errors
    return v, wifipw, sshpw, errors


def validate_outputs(v, st):
    """The "Also feed" rows -> [(type, a, b, label)] for outputs.conf, plus error messages."""
    new, errors = [], []
    for pre, name, _ in OUTPUT_PRESETS:
        h, p = v[pre + "_host"], v[pre + "_port"]
        if not h and not p:
            continue
        if not (h and p):
            errors.append("%s: enter both the host and the port, or neither." % name)
        elif not valid_host(h):
            errors.append("%s: the host is a name such as data.example.com or an IPv4 address such as 192.0.2.10." % name)
        elif not valid_port(p):
            errors.append("%s: the port is a number from 1 to 65535." % name)
        else:
            new.append(("udp", h, p, name))
    for i in range(1, CUSTOM_OUTPUTS + 1):
        t, target = v["c%d_type" % i], v["c%d_target" % i]
        if not target:
            continue
        if t in ("udp", "tcp"):
            host, sep, port = target.rpartition(":")
            if sep and valid_host(host) and valid_port(port):
                new.append((t, host, port, "custom"))
            else:
                errors.append("Custom output %d: use host:port, for example 192.168.1.20:10110." % i)
        elif t == "http":
            if valid_url(target):
                new.append(("http", target, "", "custom"))
            else:
                errors.append("Custom output %d: use an http:// or https:// address with letters, digits and . _ ~ %% / ? = & + : , - only." % i)
        else:
            errors.append("Custom output %d: choose UDP, TCP or HTTP." % i)
    if new and v["mode"] == "adsb":
        errors.append("Other networks are fed from the AIS receiver. Choose AIS or both, or leave Also feed empty.")
    kept = parse_outputs(st.sys.read_text(OUTPUTS_FILE)) if v["keep_outputs"] else []
    outputs, seen = [], set()
    for o in kept + new:
        if o[:3] not in seen:
            seen.add(o[:3])
            outputs.append(o)
    if len(outputs) > MAX_OUTPUTS:
        errors.append("At most %d extra outputs." % MAX_OUTPUTS)
    return outputs, errors


def installer_command(v):
    """argv and env for installer v7 (flags as in v5; v6+ also read outputs.conf, v7 reads VOYDAR_*):
    [--private] --ais|--adsb|--both [--adsb-serial S] [--ais-serial S] [--lat LAT --lon LON] --yes <key> [gain]
    (--yes is implied by VOYDAR_IMAGE=1 too; passed explicitly so the run never waits for a tty)."""
    args = []
    if v["private"]:
        args.append("--private")
    args.append("--" + v["mode"])
    if v["adsb_serial"] and v["mode"] in ("adsb", "both"):
        args += ["--adsb-serial", v["adsb_serial"]]
    if v["ais_serial"] and v["mode"] in ("ais", "both"):
        args += ["--ais-serial", v["ais_serial"]]
    if v["lat"] and v["mode"] in ("adsb", "both"):
        args += ["--lat", v["lat"], "--lon", v["lon"]]
    args.append("--yes")
    args.append(v["key"])
    if v["gain"] and v["mode"] in ("ais", "both"):
        args.append(v["gain"])
    env = {"VOYDAR_FEEDER_FILE": FEEDER, "VOYDAR_IMAGE": "1"}
    if v["lat"]:
        env.update({"VOYDAR_LAT": v["lat"], "VOYDAR_LON": v["lon"]})
    return args, env


# ---------------------------------------------------------------------------------------------
# The install job (runs in a thread after a valid submit).


def install_job(st, v, wifipw, sshpw):
    s = st.sys
    try:
        if v["ssid"]:
            st.log("Joining Wi-Fi network %r" % v["ssid"])
            if not s.apply_wifi(v["ssid"], wifipw, v["country"]):
                raise SetupError("Could not join the Wi-Fi network %r. Check the name and password, then try again "
                                 "(reconnect to the Beacon's hotspot if you were using it)." % v["ssid"])
            st.mode = "lan"
            st.log("Connected")
        elif not s.has_network():
            raise SetupError("No network connection.")

        if v["ssh"]:
            st.log("Turning on SSH for user %s" % SSH_USER)
            s.enable_ssh(sshpw)

        # The key goes where installer v7 and the ADS-B feeder read it (VOYDAR_KEY in beacon.env;
        # the installer rewrites this file itself for ADS-B). The form's other choices go to a
        # separate file the installer never touches.
        s.write_env(ENV_FILE, {"VOYDAR_KEY": v["key"]},
                    "Voydar Beacon station key (read by voydar-adsb-feed). Keep it private.")
        s.write_env(SETUP_ENV_FILE, {
            "VOYDAR_MODE": v["mode"],
            "VOYDAR_PRIVATE": "1" if v["private"] else "0",
            "VOYDAR_AIS_SERIAL": v["ais_serial"],
            "VOYDAR_ADSB_SERIAL": v["adsb_serial"],
            "VOYDAR_AIS_GAIN": v["gain"] or "auto",
            "VOYDAR_LAT": v["lat"],
            "VOYDAR_LON": v["lon"],
        }, "Choices made on the Voydar Beacon setup page (no secrets).")
        # Installer v7 starts AIS-catcher with every output in this file (and voydar-outputs keeps it).
        s.write_private(OUTPUTS_FILE, outputs_text(v["outputs"]))
        if v["outputs"]:
            st.log("Also feeding %d other output%s: %s" % (
                len(v["outputs"]), "" if len(v["outputs"]) == 1 else "s", ", ".join(o[3] or o[0] for o in v["outputs"])))
        st.log("Saved settings to %s (root only)" % ETC)

        s.wait_preload(st.log)

        args, env = installer_command(v)
        st.log("Running the Voydar Beacon installer")
        rc = s.run_installer(args, env, st.log)
        if rc != 0:
            raise SetupError("The installer stopped (exit %d). The log above says why; fix it and submit the form again." % rc)

        s.mark_configured()
        s.stop_hotspot(delete=True)
        with st.lock:
            st.phase = "done"
            st.done_at = time.time()
        st.log("Setup complete. The setup page switches off now.")
        s.console("Voydar Beacon is set up and running. Setup page is off.\n"
                  "To run setup again, create an empty file named voydar-reset on the boot partition.")
    except Exception as ex:  # noqa: BLE001 - every failure must reach the page, redacted
        msg = str(ex) if isinstance(ex, SetupError) else "Unexpected error: %s" % ex
        st.log("ERROR: " + msg)
        with st.lock:
            st.phase = "failed"
            st.message = redact(msg)
        if not s.has_network() and s.has_wifi():
            st.log("Restarting the setup hotspot so you can try again")
            try:
                bring_up_hotspot(st)
            except Exception as ex2:  # noqa: BLE001
                st.log("could not restart the hotspot: %s" % ex2)


class SetupError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# HTTP


def make_handler(st):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VoydarBeaconSetup/1"
        sys_version = ""

        def log_message(self, fmt, *args):  # only method + path, never bodies or query strings
            st.sys.log("http %s %s" % (self.command, urlsplit(self.path).path))

        def _client_ok(self):
            try:
                ip = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            # Private, loopback, link-local and CGNAT ranges; never a public address.
            return not ip.is_global and not ip.is_multicast and not ip.is_unspecified

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0].lower().rstrip(".")
            return host in st.allowed_hosts()

        def _send(self, code, body="", ctype="text/html; charset=utf-8", extra=None):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            for k, val in (extra or {}).items():
                self.send_header(k, val)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _gate(self):
            if not self._client_ok():
                self._send(403, "forbidden", "text/plain")
                return False
            if not self._host_ok():
                # Captive-portal probes and DNS-rebinding attempts land here: send them to our own origin.
                self._send(302, "", extra={"Location": st.base_url()})
                return False
            return True

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._gate():
                return
            path = urlsplit(self.path).path
            with st.lock:
                phase = st.phase
            if path == "/status" or (path == "/" and phase in ("running", "done")):
                self._send(200, render_status(st))
            elif path == "/":
                if phase == "failed":
                    with st.lock:
                        st.phase = "form"
                self._send(200, render_form(st, st.new_token()))
            elif path == "/favicon.ico":
                self._send(404, "", "text/plain")
            else:
                self._send(302, "", extra={"Location": "/"})

        def do_POST(self):
            if not self._gate():
                return
            if urlsplit(self.path).path != "/setup":
                self._send(404, "not found", "text/plain")
                return
            origin = self.headers.get("Origin")
            if origin and origin != "null" and (urlsplit(origin).hostname or "").lower() not in st.allowed_hosts():
                self._send(403, "cross-origin request refused", "text/plain")
                return
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/x-www-form-urlencoded":
                self._send(415, "unsupported", "text/plain")
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_BODY:
                self._send(413, "too large", "text/plain")
                return
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=40)
            if not st.use_token((form.get("csrf", [""])[0])):
                self._send(403, page("Voydar Beacon setup",
                                     "<h1>Form expired</h1><p><a href='/'>Reload the setup page</a> and submit again.</p>"))
                return
            with st.lock:
                busy = st.phase in ("running", "done")
            if busy:
                self._send(303, "", extra={"Location": "/status"})
                return
            v, wifipw, sshpw, errors = validate(form, st)
            if errors:
                self._send(400, render_form(st, st.new_token(), errors, v))
                return
            for secret in (v["key"], wifipw, sshpw):
                add_secret(secret)
            with st.lock:
                st.phase = "running"
                st.lines = []
                st.message = ""
            st.log("Settings accepted (mode: %s)" % v["mode"])
            if st.mode == "hotspot" and v["ssid"]:
                # The phone is on our hotspot, which goes away when the Beacon joins the Wi-Fi.
                self._send(200, page("Voydar Beacon setup", """<h1>Joining your Wi-Fi</h1>
<div class=ok>The Beacon is switching to <b>%s</b> and will then install itself. This hotspot will disappear.</div>
<p>Reconnect your phone to your normal Wi-Fi. When it has finished, your station page on
<b>voydar.com</b> turns green once the first message arrives. On the same network you can follow progress at
<a href="http://%s.local/status">http://%s.local/status</a>.</p>
<p class=hint>If the Beacon cannot join, the <b>%s</b> hotspot comes back within about two minutes so you can try again.</p>"""
                                     % (e(v["ssid"]), HOSTNAME, HOSTNAME, e(st.hotspot_ssid))))
                delay = 3
            else:
                self._send(303, "", extra={"Location": "/status"})
                delay = 0

            def later():
                time.sleep(delay)
                install_job(st, v, wifipw, sshpw)

            threading.Thread(target=later, daemon=True).start()

    return Handler


# ---------------------------------------------------------------------------------------------
# Startup: decide LAN or hotspot, then serve until configured.


def bring_up_hotspot(st):
    s = st.sys
    ssid = st.hotspot_ssid
    psk = s.hotspot_psk()
    try:
        st.ssids = s.scan_ssids()  # scan first: the radio is busy once it is an access point
    except Exception:  # noqa: BLE001
        st.ssids = []
    s.start_hotspot(ssid, psk)
    st.mode = "hotspot"
    label = ("Voydar Beacon setup\n"
             "1. On your phone, join the Wi-Fi network:  %s\n"
             "   password:  %s\n"
             "2. Open  http://%s  (it may open by itself)\n"
             "3. Paste your station key from voydar.com/contribute and your home Wi-Fi.\n"
             % (ssid, psk, HOTSPOT_IP))
    ws = s.wifi_status()
    if ws and not ws["ok"] and ws["message"]:
        label += "Note: %s\n" % ws["message"]
    s.write_label(label)
    s.console(label)
    s.log("hotspot %s is up" % ssid)


def import_wifi(system):
    """--import-wifi (voydar-wifi-import.service, every boot): apply voydar-wifi.txt from the boot
    partition, then blank it. A file that only holds the empty template is left alone; a file with
    mistakes is left as it is (nothing applied) and the reason shows on the setup page and console."""
    path = system.path(WIFI_FILE)
    text = system.read_text(WIFI_FILE)
    if text is None:
        return 0
    settings, errors = parse_wifi_file(text)
    for k in ("password", "hotspot_password"):
        add_secret(settings.get(k, ""))
    if not settings and not errors:
        return 0
    if errors:
        msg = "voydar-wifi.txt was not used: %s. Fix it on the card and restart, or use this page." % "; ".join(errors)
        system.log(msg)
        system.write_wifi_status(False, msg)
        system.console("Voydar Beacon: " + msg)
        return 0
    if settings.get("hotspot_password"):
        system.set_hotspot_psk(settings["hotspot_password"])
        system.log("hotspot password taken from voydar-wifi.txt")
    # The card's copy goes first, before anything can fail: no Wi-Fi password stays on the card.
    blank_file(path, wifi_template())
    system.log("voydar-wifi.txt read and blanked")
    ssid = settings.get("ssid")
    if not ssid:
        system.write_wifi_status(True, "Hotspot password set from voydar-wifi.txt.")
        return 0
    try:
        system.save_wifi(ssid, settings.get("password", ""), settings["country"])
        ok = system.join_saved_wifi()
    except Exception as ex:  # noqa: BLE001 - the setup page and hotspot still come up
        system.log("saving Wi-Fi from voydar-wifi.txt failed: %s" % ex)
        ok = False
    if ok:
        msg = "Joined the Wi-Fi %s from voydar-wifi.txt." % ssid
    else:
        msg = ("Saved the Wi-Fi %s from voydar-wifi.txt but could not connect. Check the name and password "
               "and enter them below." % ssid)
    system.log(msg)
    system.write_wifi_status(ok, msg)
    if not ok:
        system.console("Voydar Beacon: " + msg)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Voydar Beacon first-boot setup page")
    ap.add_argument("--dry-run", action="store_true", help="touch nothing; fake hardware and network")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--state-dir", default="./voydar-setup-dry-run", help="dry-run output directory")
    ap.add_argument("--net-wait", type=int, default=45, help="seconds to wait for a network before starting the hotspot")
    ap.add_argument("--import-wifi", action="store_true",
                    help="apply and blank voydar-wifi.txt from the boot partition, then exit (runs on every boot)")
    a = ap.parse_args()

    if a.import_wifi:
        if a.dry_run:
            return import_wifi(DrySystem(os.path.abspath(a.state_dir)))
        if os.geteuid() != 0:
            sys.exit("voydar-setup --import-wifi must run as root (voydar-wifi-import.service)")
        return import_wifi(RealSystem())

    if a.dry_run:
        system = DrySystem(os.path.abspath(a.state_dir))
        bind, port = "127.0.0.1", a.port or 8080
    else:
        if os.geteuid() != 0:
            sys.exit("voydar-setup must run as root (it is started by voydar-setup.service)")
        if os.path.exists(CONFIGURED):
            print("already configured; nothing to do")
            return
        system = RealSystem()
        bind, port = "0.0.0.0", a.port or 80

    mac = system.wlan_mac().replace(":", "")
    st = State(system, "lan", None, None)
    st.hotspot_ssid = "Voydar-Beacon-" + (mac[-4:].upper() if len(mac) >= 4 else "0000")

    def allowed_hosts():
        hosts = {HOSTNAME, HOSTNAME + ".local", HOTSPOT_IP, "localhost", "127.0.0.1"}
        try:
            hosts.update(system.lan_ips())
        except Exception:  # noqa: BLE001
            pass
        try:
            hosts.add(socket.gethostname().lower())
            hosts.add(socket.gethostname().lower() + ".local")
        except OSError:
            pass
        return hosts

    def base_url():
        if st.mode == "hotspot":
            return "http://%s/" % HOTSPOT_IP
        if a.dry_run:
            return "http://127.0.0.1:%d/" % port
        return "http://%s.local/" % HOSTNAME

    st.allowed_hosts = allowed_hosts
    st.base_url = base_url

    # Wait for a network the card may already know (Ethernet, or Wi-Fi saved before a reset).
    deadline = time.time() + (0 if a.dry_run else a.net_wait)
    while not system.has_network() and time.time() < deadline:
        time.sleep(3)
    if system.has_network():
        ips = system.lan_ips()
        msg = ("Voydar Beacon setup: open  http://%s.local/  (or http://%s/) from a device on this network."
               % (HOSTNAME, ips[0] if ips else "<this device's address>"))
        system.console(msg)
        system.log("network present; serving setup on the LAN")
    elif system.has_wifi():
        bring_up_hotspot(st)
    else:
        system.console("Voydar Beacon setup: no network and no Wi-Fi. Plug in an Ethernet cable, then open http://%s.local/" % HOSTNAME)
        system.log("no network and no Wi-Fi adapter; waiting for Ethernet")

    ThreadingHTTPServer.address_family = socket.AF_INET  # IPv4 only: never reachable on a global IPv6 address
    httpd = ThreadingHTTPServer((bind, port), make_handler(st))
    httpd.daemon_threads = True
    system.log("setup page listening on %s:%d (%s mode)" % (bind, port, st.mode))

    def watchdog():
        while True:
            time.sleep(5)
            with st.lock:
                phase, done_at = st.phase, st.done_at
            if phase == "done" and time.time() - done_at > (5 if a.dry_run else DONE_GRACE_S):
                system.log("setup finished; stopping the setup page")
                httpd.shutdown()
                return
            # No network, no hotspot, nothing running: bring the hotspot (back) up.
            if (phase in ("form", "failed") and st.mode == "lan" and system.has_wifi()
                    and not system.has_network() and not system.hotspot_active()):
                try:
                    bring_up_hotspot(st)
                except Exception as ex:  # noqa: BLE001
                    system.log("hotspot start failed: %s" % ex)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
