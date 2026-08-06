#!/usr/bin/env python3
"""HaLow gateway web console — mesh-v4-style UI for the Pi router.

Serves HTTPS on :8443 with HTTP Basic auth (PBKDF2 digest in
/etc/halow/ui.conf — no plaintext credential at rest, mirroring the node
firmware's model). Talks to: the morse driver via iw/ip/morse_cli, the
router layer via ip/nft/dnsmasq, and the mesh nodes' own admin APIs via
the bearer token in /etc/halow/nodes.json (never committed).
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.request
from functools import wraps

from flask import Flask, Response, jsonify, redirect, request, session

CONF_DIR = "/etc/halow"
UI_CONF = os.path.join(CONF_DIR, "ui.conf")          # AUTH_SALT/AUTH_HASH/ITER
NODES_CONF = os.path.join(CONF_DIR, "nodes.json")    # mesh node URLs + token
ENV_CONF = os.path.join(CONF_DIR, "halow.env")
PROFILES = os.path.join(CONF_DIR, "halow-profiles.json")
HALOW_IF = "halow0"

app = Flask(__name__)


def _session_secret():
    """Signed-cookie key from ui.conf; random fallback (sessions then reset
    on service restart, which fails safe)."""
    conf = {}
    try:
        with open(UI_CONF) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    conf[k] = v
    except OSError:
        pass
    sec = conf.get("SESSION_SECRET")
    return bytes.fromhex(sec) if sec else os.urandom(32)


app.secret_key = _session_secret()
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=7),
)


# ---------- trail-cam ingest sink (roadmap 29) ----------
# Terminal sink: a camera POSTs, the operator GETs, nothing else moves
# (store-and-forward stays rejected). Runs as halow-ui; /var/lib/halow is
# already service-owned. sha256-verified on receipt, both-caps bounded ring.
import re as _ire
import threading as _threading
IMAGES_DIR = "/var/lib/halow/images"
IMAGES_INDEX = os.path.join(IMAGES_DIR, "index.jsonl")
IMAGE_ID_RE = _ire.compile(
    r"^[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9-]{1,32}-[0-9]{6}-[0-9a-f]{8}\.jpg$")
_ingest_lock = _threading.Lock()
_ingest_rate = {}   # ip -> [timestamps within the 60s window]
os.makedirs(IMAGES_DIR, exist_ok=True)


def _ingest_conf():
    e = load_kv(ENV_CONF)
    def g(k, d):
        try:
            return int(e.get(k) or d)
        except ValueError:
            return d
    return {"max_bytes": g("INGEST_MAX_BYTES", 4194304),
            "ring_count": g("INGEST_RING_COUNT", 2000),
            "ring_mb": g("INGEST_RING_MB", 256),
            "min_free_mb": g("INGEST_MIN_FREE_MB", 512),
            "rate_per_min": g("INGEST_RATE_PER_MIN", 60)}


def _ring_load():
    """Index records; reconcile against the directory (a brownout
    mid-eviction leaves them disagreeing — rebuild from filenames, which
    carry all metadata by design)."""
    recs, seen = [], set()
    try:
        for line in open(IMAGES_INDEX):
            try:
                r = json.loads(line)
                if os.path.isfile(os.path.join(IMAGES_DIR, r["id"])):
                    recs.append(r)
                    seen.add(r["id"])
            except Exception:
                pass
    except OSError:
        pass
    on_disk = {f for f in os.listdir(IMAGES_DIR) if IMAGE_ID_RE.match(f)}
    if on_disk - seen:   # files with no index row — rebuild those from name
        for fid in on_disk - seen:
            parts = fid[:-4].split("-")
            ts, node, seq, _sh = parts[0], "-".join(parts[1:-2]), \
                parts[-2], parts[-1]
            try:
                st = os.stat(os.path.join(IMAGES_DIR, fid))
                recs.append({"id": fid, "node": node,
                             "seq": int(seq) if seq != "000000" else None,
                             "bytes": st.st_size, "sha256": None,
                             "received_at": int(st.st_mtime),
                             "claimed_ts": None})
            except OSError:
                pass
    recs.sort(key=lambda r: r["received_at"])
    return recs


def _ring_write_index(recs):
    tmp = IMAGES_INDEX + ".tmp"
    with open(tmp, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, IMAGES_INDEX)   # atomic — the #22 convention


def _ring_stats(recs, conf):
    total = sum(r["bytes"] for r in recs)
    vfs = os.statvfs(IMAGES_DIR)
    return {"count": len(recs), "bytes": total,
            "cap_count": conf["ring_count"], "cap_mb": conf["ring_mb"],
            "free_mb": vfs.f_bavail * vfs.f_frsize // 1048576}


def _sd_notify(msg):
    """Pure-Python sd_notify: AF_UNIX datagram to $NOTIFY_SOCKET (with the
    abstract-namespace @ prefix). No-op without the env var, so running
    'python3 halow_ui.py' by hand still works."""
    import socket as _socket
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        s.sendto(msg, "\0" + path[1:] if path.startswith("@") else path)
        s.close()
    except OSError:
        pass


def _watchdog_thread(scheme):
    """Pet systemd only after verifying we actually serve: a real local
    /healthz request each WATCHDOG_USEC/3. On probe failure send nothing —
    60s of missed pets SIGABRTs the service and Restart revives it. First
    success sends READY=1 so systemd counts us started only once serving."""
    import urllib.request
    usec = int(os.environ.get("WATCHDOG_USEC", "60000000"))
    interval = max(usec // 3 // 1_000_000, 5)
    sctx = ssl._create_unverified_context() if scheme == "https" else None
    ready = False
    while True:
        try:
            r = urllib.request.urlopen(f"{scheme}://127.0.0.1:8443/healthz",
                                       timeout=5, context=sctx)
            if r.status == 200:
                if not ready:
                    _sd_notify(b"READY=1")
                    ready = True
                _sd_notify(b"WATCHDOG=1")
        except Exception:
            pass
        time.sleep(interval)


@app.get("/healthz")
def healthz():
    # unauthenticated constant by design: the watchdog self-probe target.
    # No version, no uptime, no config — nothing for the secrets rule.
    return Response("ok", mimetype="text/plain")


def sh(cmd, timeout=10):
    """Run a command, return stdout ('' on any failure — UI shows absence)."""
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def load_kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k] = v
    except OSError:
        pass
    return out


def verify_creds(user_pass):
    conf = load_kv(UI_CONF)
    salt, digest = conf.get("AUTH_SALT"), conf.get("AUTH_HASH")
    if not (salt and digest):
        return False
    calc = hashlib.pbkdf2_hmac("sha256", user_pass.encode(),
                               bytes.fromhex(salt),
                               int(conf.get("AUTH_ITER", "100000"))).hex()
    return hmac.compare_digest(calc, digest)


# Auth failure throttle (mesh-v4 model: free tries, growing penalty,
# success clears; penalty must exceed the hash cost or it is invisible).
_FAILS = {}  # ip -> [count, penalty_until]


def _throttled(ip):
    f = _FAILS.get(ip)
    return bool(f and f[1] > time.time())


def _auth_fail(ip):
    f = _FAILS.get(ip, [0, 0.0])
    f[0] += 1
    if f[0] > 3:
        f[1] = time.time() + min(30 * (2 ** (f[0] - 4)), 300)
    _FAILS[ip] = f


def _auth_ok(ip):
    _FAILS.pop(ip, None)


def check_auth(header):
    """Basic auth and Bearer tokens stay valid for curl/scripts alongside
    browser sessions. The Bearer token is the mesh-v4 ADMIN_TOKEN; only its
    sha256 is stored (ui.conf API_TOKEN_HASH), mirroring the nodes' model."""
    ip = request.remote_addr
    if _throttled(ip):
        return False
    if header.startswith("Bearer "):
        want = load_kv(UI_CONF).get("API_TOKEN_HASH", "")
        got = hashlib.sha256(header[7:].strip().encode()).hexdigest()
        ok = bool(want) and hmac.compare_digest(got, want)
        _auth_ok(ip) if ok else _auth_fail(ip)
        return ok
    if not header.startswith("Basic "):
        return False
    try:
        user_pass = base64.b64decode(header[6:]).decode()
    except Exception:
        return False
    ok = verify_creds(user_pass)
    _auth_ok(ip) if ok else _auth_fail(ip)
    return ok


def authed(fn):
    @wraps(fn)
    def wrap(*a, **kw):
        if session.get("authed"):
            return fn(*a, **kw)
        if check_auth(request.headers.get("Authorization", "")):
            return fn(*a, **kw)
        if request.path.startswith("/api/"):
            return jsonify({"error": "auth required"}), 401
        return redirect("/login")
    return wrap


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authed"):
        return redirect("/")
    error = ""
    if request.method == "POST":
        ip = request.remote_addr
        if _throttled(ip):
            error = "too many failures — wait before retrying (retrying extends nothing here, but stop anyway)"
        else:
            user = request.form.get("username", "")
            pw = request.form.get("password", "")
            if verify_creds(f"{user}:{pw}"):
                _auth_ok(ip)
                session.permanent = True
                session["authed"] = True
                session["user"] = user
                return redirect("/")
            _auth_fail(ip)
            time.sleep(1)  # slow brute force; PBKDF2 already costs ~0.1s
            error = "wrong username or password"
    return Response(LOGIN_PAGE.replace("__ERROR__",
                    f'<p class="err">{error}</p>' if error else ""),
                    mimetype="text/html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ---------- HaLow ----------

@app.get("/api/halow")
@authed
def api_halow():
    env = load_kv(ENV_CONF)
    iface = env.get("HALOW_IF", HALOW_IF)
    up = bool(sh(f"ip link show {iface} 2>/dev/null"))
    stations = []
    cur = None
    for line in sh(f"iw dev {iface} station dump").splitlines():
        line = line.strip()
        if line.startswith("Station"):
            cur = {"mac": line.split()[1]}
            stations.append(cur)
        elif cur is not None and ":" in line:
            k, v = line.split(":", 1)
            if k.strip() in ("signal", "signal avg", "tx bitrate",
                             "rx bitrate", "connected time", "inactive time",
                             "tx packets", "rx packets", "tx retries",
                             "tx failed"):
                cur[k.strip().replace(" ", "_")] = v.strip()
    profiles = {}
    try:
        with open(PROFILES) as f:
            profiles = json.load(f)
    except Exception:
        pass
    return jsonify({
        "interface": iface,
        "present": up,
        "iw_info": sh(f"iw dev {iface} info"),
        "stations": stations,
        "ssid": env.get("HALOW_SSID"),
        "mode": env.get("HALOW_MODE", "ap"),
        "profile": env.get("HALOW_PROFILE"),
        "channel_override": env.get("HALOW_CHANNEL", ""),
        "width_override": env.get("HALOW_WIDTH", ""),
        "profiles": profiles.get("profiles", {}),
        "ap_active": sh("systemctl is-active halow-ap").strip(),
        "chip_dmesg": sh("journalctl -k --no-pager -g morse -n 5 -q"),
    })


@app.get("/api/halow/compat")
@authed
def api_halow_compat():
    """Pinned-set verdicts (halowctl check-compat). Read-only, no sudo:
    everything the check reads is readable by halow-ui."""
    import re as _re
    args = []
    p = request.args.get("profile")
    if p:
        if not _re.match(r"^[A-Za-z0-9-]+$", p):
            return jsonify({"error": "bad profile name"}), 400
        args.append(f"profile={p}")
    for k in ("channel", "width"):
        v = request.args.get(k)
        if v:
            if not v.isdigit():
                return jsonify({"error": f"{k} must be numeric"}), 400
            args.append(f"{k}={v}")
    s = request.args.get("ssid")
    if s:
        args.append(f"ssid={s}")
    try:
        r = subprocess.run(["/usr/local/bin/halowctl", "check-compat"] + args,
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return jsonify({"error": (r.stdout + r.stderr).strip()}), 502
        return Response(r.stdout, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/halow/profile")
@authed
def api_halow_profile():
    import re as _re
    name = request.form.get("name", "")
    if not _re.match(r"^[A-Za-z0-9-]+$", name):
        return jsonify({"error": "bad profile name"}), 400
    # brownout policy: only widths >=8 MHz decline — switching DOWN to
    # long-range is the remediation path and must never be blocked
    try:
        width = json.load(open(PROFILES))["profiles"].get(
            name, {}).get("width_mhz", 0)
    except Exception:
        width = 0
    if width >= 8:
        d = _decline_high_draw(f"profile apply ({width}MHz)")
        if d:
            return d
    args = ["set-profile", name]
    if request.form.get("confirm") == "1":
        args.append("confirm=1")
    ok, out = halowctl(args, timeout=30)
    if not ok:
        return jsonify({"error": out}), 400
    return jsonify({"applied": name, "output": out})


@app.post("/api/halow/probe")
@authed
def api_halow_probe():
    out = sh("sudo /usr/local/bin/halowctl probe 2>&1", timeout=40)
    return jsonify({"ok": "PROBE OK" in out, "output": out})


@app.post("/api/halow/mode")
@authed
def api_halow_mode():
    m = request.form.get("mode", "")
    if m not in ("ap", "sta"):
        return jsonify({"error": "mode must be ap or sta"}), 400
    out = sh(f"sudo /usr/local/bin/halowctl mode {m} 2>&1", timeout=40)
    return jsonify({"mode": m, "output": out})


@app.post("/api/halow/set")
@authed
def api_halow_set():
    import re as _re
    args = ["set"]
    # session knobs (roadmap 28) — halowctl validates; numeric or empty
    for k in ("inactivity", "bss_max_idle", "max_idle", "dtim"):
        v = request.form.get(k)
        if v is not None and v != "" and not v.isdigit():
            return jsonify({"error": f"{k} must be numeric or empty"}), 400
        if v is not None:
            args.append(f"{k}={v}")
    ch = request.form.get("channel")
    w = request.form.get("width")
    ssid = request.form.get("ssid")
    if ch:
        if not ch.isdigit():
            return jsonify({"error": "channel must be numeric"}), 400
        args.append(f"channel={ch}")
    if w:
        if not w.isdigit():
            return jsonify({"error": "width must be numeric"}), 400
        args.append(f"width={w}")
    if ssid:
        args.append(f"ssid={ssid}")
    if len(args) == 1:
        return jsonify({"error": "nothing to set"}), 400
    if request.form.get("confirm") == "1":
        args.append("confirm=1")
    ok, out = halowctl(args, timeout=30)
    if not ok:
        return jsonify({"error": out}), 400
    return jsonify({"output": out})


# ---------- Router ----------

@app.get("/api/router")
@authed
def api_router():
    leases = []
    try:
        with open("/var/lib/misc/dnsmasq.leases") as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    leases.append({"expiry": p[0], "mac": p[1], "ip": p[2],
                                   "host": p[3]})
    except FileNotFoundError:
        pass
    return jsonify({
        "interfaces": json.loads(sh("ip -j addr") or "[]"),
        "routes": json.loads(sh("ip -j route") or "[]"),
        "forwarding": sh("sysctl -n net.ipv4.ip_forward").strip(),
        "nft": sh("sudo /usr/sbin/nft list table ip halow 2>&1"),
        "leases": leases,
        "dnsmasq": sh("systemctl is-active dnsmasq").strip(),
        "wifi_ap": bool(sh("nmcli -t -f NAME con show --active | grep -x mesh-2g")),
    })


@app.post("/api/router/wifi-ap")
@authed
def api_router_wifi_ap():
    state = request.form.get("state", "")
    if state not in ("on", "off"):
        return jsonify({"error": "state must be on or off"}), 400
    out = sh(f"sudo /usr/local/bin/halowctl wifi-ap {state} 2>&1", timeout=30)
    return jsonify({"state": state, "output": out})


# ---------- Router configuration ----------
# Every mutation shells to halowctl (the audited sudo surface) and is
# reachable identically from the UI and from curl with Basic/Bearer auth.

def halowctl(args, stdin=None, timeout=40):
    try:
        r = subprocess.run(["sudo", "/usr/local/bin/halowctl"] + args,
                           input=stdin, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


@app.get("/api/config")
@authed
def api_config():
    env = load_kv(ENV_CONF)
    dhcp_raw = ""
    try:
        dhcp_raw = open("/etc/dnsmasq.d/halow.conf").read()
    except OSError:
        pass
    import re as _re
    rng = _re.search(r"dhcp-range=([\d.]+),([\d.]+),(\S+)", dhcp_raw)
    dns = _re.search(r"option:dns-server,(\S+)", dhcp_raw)
    forwards = []
    try:
        forwards = json.load(open("/etc/halow/forwards.json"))
    except Exception:
        pass
    reservations = []
    try:
        for line in open("/etc/dnsmasq.d/halow-reservations.conf"):
            if line.startswith("dhcp-host="):
                parts = line.strip()[10:].split(",")
                reservations.append({"mac": parts[0],
                                     "ip": parts[1] if len(parts) > 1 else "",
                                     "name": parts[2] if len(parts) > 2 else ""})
    except OSError:
        pass
    wifi = {}
    for line in sh("nmcli -t -f 802-11-wireless.ssid,802-11-wireless.channel con show mesh-2g").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            wifi[k.rsplit(".", 1)[-1]] = v
    return jsonify({
        "halow": {
            "ssid": env.get("HALOW_SSID"),
            "passphrase_set": bool(env.get("HALOW_PASSPHRASE")),
            "mode": env.get("HALOW_MODE", "ap"),
            "profile": env.get("HALOW_PROFILE"),
            "channel_override": env.get("HALOW_CHANNEL", ""),
            "width_override": env.get("HALOW_WIDTH", ""),
        },
        "wifi": {
            "ssid": wifi.get("ssid", ""),
            "channel": wifi.get("channel", ""),
            "active": bool(sh("nmcli -t -f NAME con show --active | grep -x mesh-2g")),
        },
        "dhcp": {
            "start": rng.group(1) if rng else "",
            "end": rng.group(2) if rng else "",
            "lease": rng.group(3) if rng else "",
            "dns": dns.group(1) if dns else "",
        },
        "forwards": forwards,
        "reservations": reservations,
    })


@app.post("/api/config/halow")
@authed
def api_config_halow():
    out = []
    ssid = request.form.get("ssid")
    if ssid:
        if request.form.get("confirm") != "1":
            return jsonify({"error": "needs confirm=1: STAs lose the network "
                            "until they are reconfigured with the new SSID"}), 400
        ok, o = halowctl(["set", f"ssid={ssid}"])
        out.append(o)
        if not ok:
            return jsonify({"error": o}), 500
    pw = request.form.get("passphrase")
    if pw:
        if request.form.get("confirm") != "1":
            return jsonify({"error": "needs confirm=1: STAs lose the network "
                            "until they hold the new passphrase"}), 400
        ok, o = halowctl(["set-passphrase"], stdin=pw)
        out.append("passphrase updated" if ok else o)
        if not ok:
            return jsonify({"error": o}), 500
    mode = request.form.get("mode")
    if mode:
        ok, o = halowctl(["mode", mode])
        out.append(o)
        if not ok:
            return jsonify({"error": o}), 500
    return jsonify({"ok": True, "output": "\n".join(out)})


@app.post("/api/config/wifi")
@authed
def api_config_wifi():
    args = []
    for k in ("ssid", "channel"):
        v = request.form.get(k)
        if v:
            args.append(f"{k}={v}")
    out = []
    if args:
        ok, o = halowctl(["wifi-config"] + args)
        out.append(o)
        if not ok:
            return jsonify({"error": o}), 500
    pw = request.form.get("passphrase")
    if pw:
        ok, o = halowctl(["wifi-passphrase"], stdin=pw)
        out.append("passphrase updated" if ok else o)
        if not ok:
            return jsonify({"error": o}), 500
    state = request.form.get("enabled")
    if state in ("on", "off"):
        ok, o = halowctl(["wifi-ap", state])
        out.append(o)
    return jsonify({"ok": True, "output": "\n".join(out)})


@app.post("/api/config/dhcp")
@authed
def api_config_dhcp():
    args = []
    for k in ("start", "end", "lease", "dns"):
        v = request.form.get(k)
        if v:
            args.append(f"{k}={v}")
    if not args:
        return jsonify({"error": "nothing to change"}), 400
    ok, o = halowctl(["dhcp-config"] + args)
    return (jsonify({"ok": True, "output": o}) if ok
            else (jsonify({"error": o}), 500))


@app.post("/api/config/forwards")
@authed
def api_config_forwards():
    op = request.form.get("op", "add")
    if op not in ("add", "del"):
        return jsonify({"error": "op must be add or del"}), 400
    args = ["forwards", op, f"proto={request.form.get('proto', 'tcp')}",
            f"ext={request.form.get('ext', '')}"]
    if op == "add":
        args.append(f"dest={request.form.get('dest', '')}")
    ok, o = halowctl(args)
    return (jsonify({"ok": True, "output": o}) if ok
            else (jsonify({"error": o}), 500))


@app.post("/api/config/reservations")
@authed
def api_config_reservations():
    op = request.form.get("op", "add")
    if op == "add":
        args = ["dhcp-reserve", "add",
                f"mac={request.form.get('mac', '')}",
                f"ip={request.form.get('ip', '')}"]
        if request.form.get("name"):
            args.append(f"name={request.form.get('name')}")
    elif op == "del":
        args = ["dhcp-reserve", "del", f"mac={request.form.get('mac', '')}"]
    else:
        return jsonify({"error": "op must be add or del"}), 400
    ok, o = halowctl(args)
    return (jsonify({"ok": True, "output": o}) if ok
            else (jsonify({"error": o}), 500))


@app.post("/api/diag/capture")
@authed
def api_diag_capture():
    secs = request.form.get("seconds", "10")
    # short captures stay allowed under brownout: debugging the brownout
    # may need receiver-side eyes
    if secs.isdigit() and int(secs) > 10:
        d = _decline_high_draw(f"capture {secs}s (>10s)")
        if d:
            return d
    ok, o = halowctl(["capture", secs], timeout=45)
    return (jsonify({"ok": True, "output": o, "download": "/api/diag/capture"})
            if ok else (jsonify({"error": o}), 500))


@app.get("/api/diag/capture")
@authed
def api_diag_capture_get():
    try:
        data = open("/var/lib/halow/capture.pcap", "rb").read()
    except OSError:
        return jsonify({"error": "no capture yet"}), 404
    return Response(data, mimetype="application/vnd.tcpdump.pcap",
                    headers={"Content-Disposition":
                             "attachment; filename=halow0.pcap"})


BACKUP_DIR = "/var/lib/halow/backup"


@app.post("/api/system/backup")
@authed
def api_system_backup():
    """Create an encrypted off-device backup. Passphrase (>=12 chars) via
    the form body over TLS, passed to halowctl on STDIN — never argv,
    never logged, never echoed back."""
    pw = request.form.get("passphrase", "")
    if len(pw) < 12:
        return jsonify({"error": "passphrase must be at least 12 chars"}), 400
    ok, out = halowctl(["backup"], stdin=pw, timeout=60)
    if not ok:
        return jsonify({"error": out}), 500
    # parse "file=.. bytes=.. sha256=.." — never surface the passphrase
    fields = dict(p.split("=", 1) for p in out.split() if "=" in p)
    return jsonify({"ok": True, "file": fields.get("file"),
                    "bytes": int(fields.get("bytes", 0)),
                    "sha256": fields.get("sha256"),
                    "download": "/api/system/backup"})


@app.get("/api/system/backup")
@authed
def api_system_backup_get():
    try:
        newest = max((os.path.join(BACKUP_DIR, f)
                      for f in os.listdir(BACKUP_DIR)
                      if f.endswith(".tar.gpg")), key=os.path.getmtime)
        data = open(newest, "rb").read()
    except (OSError, ValueError):
        return jsonify({"error": "no backup yet"}), 404
    return Response(data, mimetype="application/octet-stream",
                    headers={"Content-Disposition":
                             f"attachment; filename={os.path.basename(newest)}"})


@app.post("/api/ingest/image")
@authed
def api_ingest_image():
    """Raw JPEG upload, sha256-verified on receipt. Distinct codes so the
    node can classify: 400 built-wrong, 413 too big, 415 not JPEG, 422
    corrupted-in-flight (retry), 429 rate, 507 disk floor. Nothing stored
    on any non-200. NEVER logs the Authorization header."""
    import hashlib as _h
    conf = _ingest_conf()
    ip = request.remote_addr
    now = time.time()
    # rate window (429)
    win = [t for t in _ingest_rate.get(ip, []) if now - t < 60]
    if len(win) >= conf["rate_per_min"]:
        _ingest_rate[ip] = win
        return jsonify({"error": "rate limited",
                        "retry_after_s": max(1, int(60 - (now - win[0])))}), 429
    win.append(now)
    _ingest_rate[ip] = win
    # metadata: headers win, query params fall back
    def meta(hdr, qp):
        return request.headers.get(hdr) or request.args.get(qp)
    sha_hdr = (meta("X-SHA256", "sha") or "").lower()
    if not _ire.fullmatch(r"[0-9a-f]{64}", sha_hdr):
        return jsonify({"error": "missing or malformed X-SHA256 "
                        "(64 lowercase hex)"}), 400
    node = meta("X-Node-Id", "node") or "unknown"
    if not _ire.fullmatch(r"[A-Za-z0-9-]{1,32}", node):
        return jsonify({"error": "bad node id ([A-Za-z0-9-]{1,32})"}), 400
    seq_raw = meta("X-Sequence", "seq")
    seq = None
    if seq_raw is not None and seq_raw != "":
        if not seq_raw.isdigit() or int(seq_raw) > 4294967295:
            return jsonify({"error": "bad sequence (0..4294967295)"}), 400
        seq = int(seq_raw)
    ts_raw = meta("X-Timestamp", "ts")
    claimed_ts = int(ts_raw) if ts_raw and ts_raw.isdigit() else None
    # body cap: fast reject on content-length, real guard on the stream
    if request.content_length and request.content_length > conf["max_bytes"]:
        return jsonify({"error": "body exceeds cap",
                        "cap_bytes": conf["max_bytes"]}), 413
    data = request.stream.read(conf["max_bytes"] + 1)
    if len(data) > conf["max_bytes"]:
        return jsonify({"error": "body exceeds cap",
                        "cap_bytes": conf["max_bytes"]}), 413
    if not data:
        return jsonify({"error": "empty body"}), 400
    if data[:2] != b"\xff\xd8":
        return jsonify({"error": "not a JPEG (no FF D8 SOI)"}), 415
    got = _h.sha256(data).hexdigest()
    if got != sha_hdr:
        return jsonify({"error": "sha256 mismatch", "header_sha256": sha_hdr,
                        "received_sha256": got, "bytes": len(data)}), 422
    vfs = os.statvfs(IMAGES_DIR)
    free_mb = vfs.f_bavail * vfs.f_frsize // 1048576
    if free_mb < conf["min_free_mb"]:
        return jsonify({"error": "disk below floor", "free_mb": free_mb,
                        "floor_mb": conf["min_free_mb"]}), 507
    with _ingest_lock:
        recs = _ring_load()
        # idempotent retry: same node+seq+digest already stored
        for r in recs:
            if (r.get("sha256") == got and r.get("node") == node
                    and r.get("seq") == seq):
                return jsonify({"ok": True, "id": r["id"], "bytes": r["bytes"],
                                "sha256": got, "duplicate": True, "evicted": 0,
                                "ring": _ring_stats(recs, conf)})
        fid = (f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{node}-"
               f"{(seq if seq is not None else 0):06d}-{got[:8]}.jpg")
        with open(os.path.join(IMAGES_DIR, fid), "wb") as f:
            f.write(data)
        rec = {"id": fid, "node": node, "seq": seq, "bytes": len(data),
               "sha256": got, "received_at": int(now),
               "claimed_ts": claimed_ts}
        recs.append(rec)
        # evict oldest until BOTH caps hold
        evicted = 0
        while (len(recs) > conf["ring_count"]
               or sum(r["bytes"] for r in recs) > conf["ring_mb"] * 1048576):
            old = recs.pop(0)
            try:
                os.remove(os.path.join(IMAGES_DIR, old["id"]))
            except OSError:
                pass
            evicted += 1
        _ring_write_index(recs)
        return jsonify({"ok": True, "id": fid, "bytes": len(data),
                        "sha256": got, "duplicate": False, "evicted": evicted,
                        "ring": _ring_stats(recs, conf)})


@app.get("/api/ingest/images")
@authed
def api_ingest_list():
    conf = _ingest_conf()
    with _ingest_lock:
        recs = _ring_load()
    node_f = request.args.get("node")
    n = min(int(request.args.get("n", "50")), 500)
    view = [r for r in recs if not node_f or r.get("node") == node_f]
    view = list(reversed(view))[:n]
    # per-node receiver-side accounting — seq_gaps is the number the
    # sender cannot lie about
    nodes = {}
    for name in {r.get("node") for r in recs}:
        seqs = sorted(r["seq"] for r in recs
                      if r.get("node") == name and r.get("seq") is not None)
        gaps = sum(seqs[i + 1] - seqs[i] - 1 for i in range(len(seqs) - 1)) \
            if len(seqs) > 1 else 0
        nodes[name] = {"frames": sum(1 for r in recs if r.get("node") == name),
                       "last_seq": seqs[-1] if seqs else None,
                       "seq_gaps": gaps}
    return jsonify({"images": view, "ring": _ring_stats(recs, conf),
                    "nodes": nodes})


@app.get("/api/ingest/images/<img_id>")
@authed
def api_ingest_get(img_id):
    if not IMAGE_ID_RE.match(img_id):   # regex-then-join: the traversal guard
        return jsonify({"error": "bad image id"}), 400
    try:
        data = open(os.path.join(IMAGES_DIR, img_id), "rb").read()
    except OSError:
        return jsonify({"error": "no such image"}), 404
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control":
                             "private, max-age=31536000, immutable"})


@app.post("/api/ingest/purge")
@authed
def api_ingest_purge():
    if request.form.get("confirm") != "1":
        return jsonify({"error": "needs confirm=1: empties the frame ring"}), 400
    with _ingest_lock:
        for r in _ring_load():
            try:
                os.remove(os.path.join(IMAGES_DIR, r["id"]))
            except OSError:
                pass
        _ring_write_index([])
    return jsonify({"ok": True, "output": "ring purged"})


@app.post("/api/system/reboot")
@authed
def api_system_reboot():
    if request.form.get("confirm") != "1":
        return jsonify({"error": "needs confirm=1: reboots the gateway"}), 400
    subprocess.Popen(["sudo", "/usr/sbin/reboot"])
    return jsonify({"ok": True, "output": "rebooting"})


# ---------- Measurement, events, logs ----------

THROUGHPUT_LOG = "/var/lib/halow/throughput.jsonl"
EVENTS_LOG = "/var/lib/halow/station-events.log"
LOG_UNITS = ("halow-ap", "halow-net", "halow-ui", "halow-sta-events",
             "halow-join-watch", "halow-linkd", "halow-iperf3", "dnsmasq",
             "kernel")
JOIN_STATE_DIR = "/var/lib/halow/join"
DISK_LOW_MB = 512  # SD low-water; mirrored in scripts/halow-mon


@app.post("/api/halow/throughput")
@authed
def api_halow_throughput():
    d = _decline_high_draw("iperf3 run")  # sustained TX saturation:
    if d:                                 # exactly the 200-250mA burst case
        return d
    target = request.form.get("target", "")
    import re as _re
    if not _re.match(r"^[0-9.]+$", target):
        return jsonify({"error": "target must be an IPv4 address"}), 400
    secs = min(int(request.form.get("seconds", "5")), 30)
    rev = ["-R"] if request.form.get("reverse") == "1" else []
    try:
        r = subprocess.run(["iperf3", "-c", target, "-t", str(secs), "-J"]
                           + rev, capture_output=True, text=True,
                           timeout=secs + 15)
        data = json.loads(r.stdout)
        if "error" in data:
            return jsonify({"error": data["error"]}), 502
        end = data["end"]["sum_received" if not rev else "sum_sent"]
        result = {
            "target": target, "seconds": secs,
            "reverse": bool(rev),
            "mbps": round(end["bits_per_second"] / 1e6, 2),
            "bytes": end["bytes"],
            "retransmits": data["end"].get("sum_sent", {}).get("retransmits"),
            "at": subprocess.run(["date", "-Is"], capture_output=True,
                                 text=True).stdout.strip(),
        }
        with open(THROUGHPUT_LOG, "a") as f:
            f.write(json.dumps(result) + "\n")
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/halow/throughput")
@authed
def api_halow_throughput_history():
    out = []
    try:
        with open(THROUGHPUT_LOG) as f:
            out = [json.loads(x) for x in f.readlines()[-50:]]
    except OSError:
        pass
    return jsonify({"runs": out})


LINKTEST_LOG = "/var/lib/halow/linktest.jsonl"
LINKTEST_STATE = "/var/lib/halow/linktest-state.json"


@app.get("/api/halow/linktest")
@authed
def api_halow_linktest():
    """Receiver-counted UDP link-test history + responder state + a
    24h per-(src,size) aggregate with mesh-v4 n-discipline: n<3 gets raw
    samples and NO mean; relative spread >0.10 reports the range."""
    sessions = []
    try:
        with open(LINKTEST_LOG) as f:
            for line in f.readlines()[-50:]:
                try:
                    sessions.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    state = {}
    try:
        state = json.load(open(LINKTEST_STATE))
    except Exception:
        pass
    cutoff = time.time() - 86400
    groups = {}
    for s in sessions:
        if s.get("t", 0) >= cutoff:
            groups.setdefault(f"{s['src_ip']}/{s['size_bytes']}B",
                              []).append(s)
    aggregate = {}
    for key, ss in groups.items():
        ratios = [s["delivery"]["ratio"] for s in ss]
        goodputs = [s["goodput_bps"] for s in ss
                    if s.get("goodput_bps") is not None]
        entry = {"n": len(ss), "iface": ss[-1]["iface"]}
        if len(ss) < 3:
            # n-discipline: no mean below n=3, samples travel instead
            entry["samples"] = [{"delivery_ratio": r} for r in ratios]
        else:
            mean = sum(ratios) / len(ratios)
            entry["delivery_ratio"] = {"mean": round(mean, 4),
                                       "min": min(ratios),
                                       "max": max(ratios)}
            if goodputs:
                gmean = sum(goodputs) / len(goodputs)
                g = {"mean": round(gmean, 1)}
                if gmean and (max(goodputs) - min(goodputs)) / gmean > 0.10:
                    g["range"] = [min(goodputs), max(goodputs)]
                    g["note"] = "spread >10% — the range is the headline"
                entry["goodput_bps"] = g
        aggregate[key] = entry
    return jsonify({"sessions": sessions[::-1], "aggregate": aggregate,
                    "responder": state})


@app.post("/api/halow/linktest/selftest")
@authed
def api_halow_linktest_selftest():
    count = min(int(request.form.get("count", "500")), 1000)
    size = min(int(request.form.get("size", "200")), 1400)
    sid = int.from_bytes(os.urandom(4), "little") or 1
    try:
        subprocess.run(["/usr/local/bin/halow-linkd", "send",
                        "--target", "127.0.0.1", "--count", str(count),
                        "--size", str(size), "--pps", "400",
                        "--session-id", str(sid)],
                       capture_output=True, timeout=20)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                for line in open(LINKTEST_LOG):
                    try:
                        r = json.loads(line)
                        if r.get("session_id") == sid:
                            return jsonify(r)
                    except Exception:
                        pass
            except OSError:
                pass
            time.sleep(0.5)
        return jsonify({"error": "no record within 10s — is halow-linkd "
                        "running?"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/halow/events")
@authed
def api_halow_events():
    lines = []
    try:
        with open(EVENTS_LOG) as f:
            lines = [x.strip() for x in f.readlines()[-100:]]
    except OSError:
        pass
    return jsonify({"events": lines})


def _valid_mac(mac):
    import re as _re
    return bool(_re.fullmatch(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac))


@app.get("/api/halow/join-log")
@authed
def api_join_log():
    """Association-forensics summary: every MAC seen, verdict + last stage.
    Fresh bounded parse (wall-clock verdicts — state.json's precomputed
    verdicts only refresh when journal lines arrive, so silence would
    read as in_progress forever). Falls back to the watcher's state file.
    Empty history is 200, not an error."""
    hours = min(int(request.args.get("hours", "24")), 48)
    try:
        r = subprocess.run(["/usr/local/bin/halow-join-log", "--all",
                            "--since-hours", str(hours)],
                           capture_output=True, text=True, timeout=15)
        return Response(r.stdout, mimetype="application/json")
    except Exception:
        pass
    try:
        with open(os.path.join(JOIN_STATE_DIR, "state.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return jsonify({"stations": {}})
    return jsonify({"stations": st.get("verdicts", {})})


@app.get("/api/halow/join-log/<mac>")
@authed
def api_join_log_mac(mac):
    if not _valid_mac(mac):
        return jsonify({"error": "bad mac"}), 400
    hours = min(int(request.args.get("hours", "24")), 48)
    try:
        r = subprocess.run(["/usr/local/bin/halow-join-log", mac.lower(),
                            "--since-hours", str(hours)],
                           capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    d["bundle"] = f"/api/halow/join-log/{mac.lower()}/bundle"
    return jsonify(d)


@app.get("/api/halow/join-log/<mac>/bundle")
@authed
def api_join_log_bundle(mac):
    if not _valid_mac(mac):
        return jsonify({"error": "bad mac"}), 400
    path = os.path.join(JOIN_STATE_DIR,
                        mac.lower().replace(":", "-") + "-first-sight.log")
    try:
        return Response(open(path).read(), mimetype="text/plain")
    except OSError:
        return jsonify({"error": "no bundle for that mac yet"}), 404


@app.get("/api/logs")
@authed
def api_logs():
    unit = request.args.get("unit", "halow-ap")
    if unit not in LOG_UNITS:
        return jsonify({"error": f"unit must be one of {LOG_UNITS}"}), 400
    n = min(int(request.args.get("n", "150")), 1000)
    cmd = (["journalctl", "-k"] if unit == "kernel"
           else ["journalctl", "-u", unit])
    out = sh(" ".join(cmd + ["-n", str(n), "--no-pager", "-q"]), timeout=15)
    return jsonify({"unit": unit, "lines": out.splitlines()})


# ---------- Diagnostics ----------

import re as _re2

HOSTNAME_RE = _re2.compile(r"^[A-Za-z0-9.\-]{1,253}$")
THROTTLE_BITS = {
    0: "undervoltage NOW", 1: "arm freq capped NOW", 2: "throttled NOW",
    3: "soft temp limit NOW", 16: "undervoltage occurred",
    17: "arm freq capped occurred", 18: "throttled occurred",
    19: "soft temp limit occurred",
}


@app.post("/api/diag/ping")
@authed
def api_diag_ping():
    target = request.form.get("target", "")
    if not HOSTNAME_RE.match(target):
        return jsonify({"error": "bad target"}), 400
    n = min(int(request.form.get("count", "5")), 20)
    out = sh(f"ping -c {n} -i 0.3 -W 2 -n {target}", timeout=n * 3 + 10)
    times = [float(m) for m in _re2.findall(r"time=([\d.]+) ms", out)]
    stats = _re2.search(r"(\d+) packets transmitted, (\d+) received", out)
    sent = int(stats.group(1)) if stats else n
    recv = int(stats.group(2)) if stats else 0
    return jsonify({
        "target": target, "sent": sent, "received": recv,
        "loss_pct": round(100 * (sent - recv) / sent, 1) if sent else None,
        "min_ms": min(times) if times else None,
        "avg_ms": round(sum(times) / len(times), 2) if times else None,
        "max_ms": max(times) if times else None,
        "probes_ms": times,
        "note": f"n={sent}; loss at small n is a sample, not a rate",
    })


@app.post("/api/diag/tcpcheck")
@authed
def api_diag_tcpcheck():
    host = request.form.get("host", "")
    if not HOSTNAME_RE.match(host):
        return jsonify({"error": "bad host"}), 400
    port = int(request.form.get("port", "443"))
    scheme = "https" if request.form.get("tls", "1") == "1" else "http"
    url = f"{scheme}://{host}:{port}{request.form.get('path', '/')}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, method="HEAD")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=6, context=ctx) as r:
            return jsonify({"url": url, "status": r.status,
                            "ms": round((time.monotonic() - t0) * 1000, 1)})
    except urllib.error.HTTPError as e:
        return jsonify({"url": url, "status": e.code,
                        "ms": round((time.monotonic() - t0) * 1000, 1),
                        "note": "server answered — the path is up"})
    except Exception as e:
        return jsonify({"url": url, "error": str(e),
                        "ms": round((time.monotonic() - t0) * 1000, 1)}), 502


@app.post("/api/diag/dns")
@authed
def api_diag_dns():
    name = request.form.get("name", "")
    if not HOSTNAME_RE.match(name):
        return jsonify({"error": "bad name"}), 400
    server = request.form.get("server", "")
    if server and not HOSTNAME_RE.match(server):
        return jsonify({"error": "bad server"}), 400
    at = f"@{server} " if server else ""
    out = sh(f"dig +time=3 +tries=1 {at}{name} A +noall +answer +stats",
             timeout=10)
    answers = _re2.findall(r"\sA\s+([\d.]+)", out)
    qtime = _re2.search(r"Query time: (\d+) msec", out)
    return jsonify({"name": name, "server": server or "(system)",
                    "answers": answers,
                    "ms": int(qtime.group(1)) if qtime else None,
                    "ok": bool(answers)})


@app.get("/api/diag/neigh")
@authed
def api_diag_neigh():
    try:
        neigh = json.loads(sh("ip -j neigh") or "[]")
    except Exception:
        neigh = []
    return jsonify({"neighbors": [
        {"ip": x.get("dst"), "dev": x.get("dev"),
         "mac": x.get("lladdr", ""), "state": (x.get("state") or [""])[0]}
        for x in neigh]})


@app.get("/api/diag/survey")
@authed
def api_diag_survey():
    out = sh("iw dev halow0 survey dump")
    surveys = []
    cur = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("frequency:"):
            cur = {"frequency": line.split(":", 1)[1].strip()}
            surveys.append(cur)
        elif cur is not None and ":" in line:
            k, v = line.split(":", 1)
            cur[k.strip().replace(" ", "_")] = v.strip()
    in_use = next((s for s in surveys if "[in use]" in s.get("frequency", "")), None)
    util = None
    if in_use:
        try:
            active = int(in_use.get("channel_active_time", "0 ms").split()[0])
            busy = int(in_use.get("channel_busy_time", "0 ms").split()[0])
            util = round(100 * busy / active, 1) if active else None
        except Exception:
            pass
    return jsonify({"surveys": surveys, "utilization_pct": util})


@app.get("/api/diag/power")
@authed
def api_diag_power():
    raw = sh("vcgencmd get_throttled").strip()
    m = _re2.search(r"0x([0-9a-fA-F]+)", raw)
    val = int(m.group(1), 16) if m else None
    flags = ([THROTTLE_BITS[b] for b in THROTTLE_BITS if val & (1 << b)]
             if val is not None else ["vcgencmd unavailable"])
    uv_count = 0
    try:
        uv_count = json.load(open("/var/lib/halow/mon-state.json")
                             ).get("uv_count", 0)
    except Exception:
        pass
    return jsonify({
        "throttled_raw": raw or None,
        "flags": flags or ["clean — no undervoltage or throttling recorded"],
        "undervolt_now": bool(val & 0x1) if val is not None else False,
        "brownout_count": uv_count,
        "temp_c": round(int(open("/sys/class/thermal/thermal_zone0/temp")
                            .read()) / 1000, 1),
        "volts_core": sh("vcgencmd measure_volts core").strip() or None,
    })


def _undervolt_now():
    """Live get_throttled bit 0 — never bit 16, which latches until
    reboot and would refuse forever. Empty output fails OPEN: absence of
    evidence is not evidence, and the watchdog covers a dead monitor."""
    out = sh("vcgencmd get_throttled")
    try:
        return bool(int(out.split("=")[-1], 16) & 0x1)
    except (ValueError, IndexError):
        return False


def _decline_high_draw(op):
    """409 while the 3V3 rail sags. The error is a FIXED string: no env
    values, no command output. SSH/halowctl stay ungated root paths — a
    policy with a web-side bypass is theater, so there is no force=1."""
    if not _undervolt_now():
        return None
    return (jsonify({
        "error": "undervoltage active (get_throttled bit 0): refusing "
                 f"{op}. MM6108 TX bursts ~200-250 mA on the Pi 3V3 "
                 "header pin (docs/wiring.md) and this bench has browned "
                 "out two boards. Fix power first; history at "
                 "/api/diag/brownouts.",
        "declined": op, "policy": "brownout-high-draw"}), 409)


@app.get("/api/diag/brownouts")
@authed
def api_diag_brownouts():
    st = {}
    try:
        st = json.load(open("/var/lib/halow/mon-state.json"))
    except Exception:
        pass
    events = []
    try:
        for line in open("/var/lib/halow/brownout.jsonl").readlines()[-50:]:
            try:
                events.append(json.loads(line))
            except Exception:
                pass
    except OSError:
        pass
    return jsonify({
        "active": bool(st.get("uv_active")),
        "since": st.get("uv_since") or None,
        "count": st.get("uv_count", 0),
        "last_event": events[-1] if events else None,
        "events": events,
        "live": {"throttled_raw": sh("vcgencmd get_throttled").strip(),
                 "undervolt_now": _undervolt_now()},
        "policy": {"declines_while_active":
                   ["profile apply width>=8MHz", "capture>10s",
                    "throughput runs"]}})


@app.get("/api/diag/flows")
@authed
def api_diag_flows():
    out = sh("sudo /usr/sbin/conntrack -L -o extended 2>/dev/null | head -200",
             timeout=15)
    return jsonify({"flows": out.splitlines(), "capped_at": 200})


@app.get("/api/diag/chip")
@authed
def api_diag_chip():
    ok, out = halowctl(["chip"], timeout=20)
    return jsonify({"ok": ok, "output": out})


@app.get("/api/halow/link")
@app.get("/api/halow/link/<mac>")
@authed
def api_halow_link(mac=None):
    """Per-station link telemetry — rate and delivery from measurement,
    shaped for deriving a TRANSPORT_HALOW rung cost."""
    samples = []
    try:
        with open("/var/lib/halow/stations.jsonl") as f:
            for line in f:
                try:
                    s = json.loads(line)
                    if mac is None or s["mac"].lower() == mac.lower():
                        samples.append(s)
                except Exception:
                    pass
    except OSError:
        pass
    samples = samples[-720:]
    by_mac = {}
    for s in samples:
        by_mac.setdefault(s["mac"], []).append(s)
    out = {}
    for m, ss in by_mac.items():
        last = ss[-1]
        tx_pkts = last.get("tx_packets", 0)
        retries = last.get("tx_retries", 0)
        failed = last.get("tx_failed", 0)
        rates = [x["tx_mbps"] for x in ss if "tx_mbps" in x]
        sigs = [x["signal_dbm"] for x in ss if "signal_dbm" in x]
        out[m] = {
            "now": last,
            "n_samples": len(ss),
            "tx_mbps": {"min": min(rates), "avg": round(sum(rates)/len(rates), 2),
                        "max": max(rates)} if rates else None,
            "signal_dbm": {"min": min(sigs), "avg": round(sum(sigs)/len(sigs), 1),
                           "max": max(sigs)} if sigs else None,
            "delivery_pct": round(100 * (tx_pkts - failed) / tx_pkts, 2)
            if tx_pkts else None,
            "retry_pct": round(100 * retries / tx_pkts, 2) if tx_pkts else None,
        }
        # idle/kick gauge (roadmap 28): receiver-side "how close to being
        # deauthed for inactivity, and what idle period was granted".
        if "inactive_ms" in last:
            import math as _math
            tu = last.get("max_idle_tu")
            env = load_kv(ENV_CONF)
            if tu:
                limit_s = _math.ceil(tu * 1024 / 1000)  # sta_info.c:594
                src = "sta-negotiated"
            elif env.get("HALOW_AP_MAX_INACTIVITY"):
                limit_s = int(env["HALOW_AP_MAX_INACTIVITY"])
                src = "ap-config"
            else:
                limit_s = 300
                src = "ap-default"
            inact = last["inactive_ms"] / 1000.0
            out[m]["idle"] = {
                "inactive_s": round(inact, 1), "limit_s": limit_s,
                "limit_source": src,
                "used_pct": round(100 * inact / limit_s, 1) if limit_s else None,
                "listen_interval": last.get("listen_int"),
                "timeout_next": last.get("timeout_next"),
                "pending_poll": last.get("pending_poll")}
    # health ladder merge (station-health.json, halow-mon roadmap 18):
    # absent file -> health: null, shape otherwise unchanged. Health-only
    # MACs (e.g. reserved never-seen nodes) appear with null telemetry so
    # they are visible pre-association.
    health = {}
    try:
        health = json.load(open("/var/lib/halow/station-health.json")
                           ).get("stations", {})
    except Exception:
        pass
    for m in out:
        out[m]["health"] = health.get(m.lower())
    for hm, h in health.items():
        if mac is not None and hm != mac.lower():
            continue
        if hm not in out and not any(k.lower() == hm for k in out):
            out[hm] = {"now": None, "n_samples": 0, "health": h}
    if mac is not None:
        return jsonify(out.get(mac.lower(), out.get(mac.upper(),
                       {"error": f"no samples for {mac}", "stations": list(out)})))
    return jsonify({"stations": out})


RUNGCOST_STATE = "/var/lib/halow/rungcost-state.json"
RUNGCOST_STALE_S = 180  # three missed monitor runs


@app.get("/api/halow/rungcost")
@app.get("/api/halow/rungcost/<mac>")
@authed
def api_halow_rungcost(mac=None):
    """Measured TRANSPORT_HALOW rung cost — the node implementer's
    reference (mesh-v4 halowPeriodicCheck consumer contract).

    cost = clamp(round(100 - 49.3*log10(goodput_kbps_ewma/9.0)), 6, 99)
    anchored on the ladder's two MEASURED rates: LoRa best 9.0 kbps [M]
    -> 100, ESP-NOW healthy 604 kbps [M] -> 10. Windowed iw counter
    deltas (never lifetime-cumulative), vip.py hysteresis (demote after
    2 bad windows, promote after 3 good), EWMA alpha 0.3. healthy=false
    -> cost=null: no fabricated numbers for an unmeasurable link.

    Consumer contract: a node fetches its own MAC once per
    halow_check_min (default 30 min, floor 5); on healthy && !stale it
    MAY write cost into metric_halow. Gateway healthy is AP-side
    evidence; the node ANDs it with its local halowRungHealthy() — the
    two can legitimately disagree. Staleness (state older than 180s =
    three missed monitor runs) fail-safes to healthy:false, cost:null:
    a gateway whose monitor died must not advertise a healthy rung."""
    try:
        st = json.load(open(RUNGCOST_STATE))
    except Exception:
        st = {"t": 0, "stations": {}}
    t = st.get("t", 0)
    age = max(0, int(time.time() - t))   # clamp: an NTP step must not
    stale = age > RUNGCOST_STALE_S       # fabricate staleness

    def shape(m, e):
        out = {"mac": m,
               "healthy": bool(e.get("healthy")) and not stale,
               "cost": None if stale else e.get("cost"),
               "stale": stale, "age_s": age,
               "goodput_kbps_ewma": e.get("goodput_kbps_ewma"),
               "window": e.get("window"),
               "streaks": {"oks": e.get("oks", 0),
                           "fails": e.get("fails", 0)},
               "windows": e.get("windows", {}),
               "source": "windowed iw counter deltas, AP-side, "
                         "MAC-ack confirmed"}
        if stale:
            out["reason"] = "monitor stale"
        return out

    if mac is not None:
        m = mac.lower()
        e = st.get("stations", {}).get(m)
        if e is None:
            return jsonify({"error": f"no rungcost state for {mac}",
                            "stations": list(st.get("stations", {}))})
        return jsonify(shape(m, e))
    return jsonify({"t": t, "stale": stale,
                    "stations": {m: shape(m, e)
                                 for m, e in st.get("stations",
                                                    {}).items()}})


@app.get("/api/metrics")
@authed
def api_metrics():
    minutes = min(int(request.args.get("minutes", "60")), 2880)
    samples = []
    try:
        import time as _t
        cutoff = _t.time() - minutes * 60
        with open("/var/lib/halow/metrics.jsonl") as f:
            for line in f:
                try:
                    s = json.loads(line)
                    if s["t"] >= cutoff:
                        samples.append(s)
                except Exception:
                    pass
    except OSError:
        pass
    mon = {}
    try:
        mon = json.load(open("/var/lib/halow/mon-state.json"))
    except Exception:
        pass
    summary = {}
    if samples:
        for k in ("temp_c", "load1", "mem_avail_kb", "stations",
                  "disk_free_mb", "sta_reachable"):
            vals = [s[k] for s in samples if s.get(k) is not None]
            if vals:
                summary[k] = {"min": min(vals), "max": max(vals),
                              "now": vals[-1]}
        # low-water mark is the value that means something (bench lesson)
        summary["mem_low_water_kb"] = min(
            s["mem_avail_kb"] for s in samples)
        disk_vals = [s["disk_free_mb"] for s in samples
                     if "disk_free_mb" in s]
        if disk_vals:
            summary["disk_free_low_water_mb"] = min(disk_vals)
        summary["uptime_pct"] = {}
        for k in ("ap", "dnsmasq", "upstream", "time_synced"):
            # denominator = samples that carry the key, so a field added
            # mid-history doesn't read as downtime before it existed
            have = [s for s in samples if k in s]
            if have:
                summary["uptime_pct"][k] = round(
                    100 * sum(1 for s in have if s[k]) / len(have), 1)
    return jsonify({"minutes": minutes, "n": len(samples),
                    "summary": summary, "monitor": mon,
                    "samples": samples[-360:]})


@app.get("/api/diag")
@authed
def api_diag_bundle():
    """One-shot bundle — the gateway's answer to the nodes' /api/diag."""
    services = {}
    for u in ("halow-ap", "halow-net", "halow-ui", "dnsmasq",
              "halow-iperf3", "halow-sta-events", "halow-join-watch",
              "halow-linkd"):
        services[u] = {
            "active": sh(f"systemctl is-active {u}").strip(),
            "restarts": sh(f"systemctl show -p NRestarts --value {u}").strip(),
        }
    leases = []
    try:
        with open("/var/lib/misc/dnsmasq.leases") as f:
            leases = [x.split() for x in f]
    except OSError:
        pass
    events = []
    try:
        with open(EVENTS_LOG) as f:
            events = [x.strip() for x in f.readlines()[-10:]]
    except OSError:
        pass
    return jsonify({
        "system": json.loads(api_system().get_data()),
        "power": json.loads(api_diag_power().get_data()),
        "interfaces": json.loads(sh("ip -j addr") or "[]"),
        "routes": json.loads(sh("ip -j route") or "[]"),
        "neighbors": json.loads(api_diag_neigh().get_data())["neighbors"],
        "halow": json.loads(api_halow().get_data()),
        "services": services,
        "leases": leases,
        "recent_events": events,
        "survey": json.loads(api_diag_survey().get_data()),
    })


# ---------- Mesh nodes ----------

# Proxy path failover (roadmap 26): per-node leg preference with
# vip.py-style damping — demote LAN after 2 consecutive failures,
# promote back after 3 successes; while demoted, one cheap LAN HEAD per
# 30s lets it recover without waiting for halow to break.
_NPATH = {}   # name -> {"pref","fails","oks","changed","last_recheck"}
NODE_TIMEOUT = 5
DEMOTE_AFTER, PROMOTE_AFTER = 2, 3   # mesh-v4 tools/vip.py:47-48
LAN_RECHECK_S = 30
NODE_ADDRS = "/var/lib/halow/node-addrs.json"


def _node_ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # nodes use self-signed certs
    return ctx


def _lease_ip_for_mac(mac):
    """Read from reality at call time — never derived (vip.py's one-octet
    derived-address failure)."""
    if not mac:
        return None
    try:
        for line in open("/var/lib/misc/dnsmasq.leases"):
            p = line.split()
            if len(p) >= 3 and p[1].lower() == mac:
                return p[2]
    except OSError:
        pass
    return None


def _fetch_leg(url, token, path, timeout=NODE_TIMEOUT):
    req = urllib.request.Request(url.rstrip("/") + path)
    req.add_header("Authorization", "Bearer " + (token or ""))
    with urllib.request.urlopen(req, timeout=timeout,
                                context=_node_ssl()) as r:
        return json.load(r)


def _harvest_addrs(name, path, data):
    """Persist device-stated lora/vip addresses from /api/ip responses:
    an unreachable node is exactly when the LoRa address is needed and
    exactly when it cannot be queried (vip.py:58-61). Addresses only."""
    if path != "/api/ip" or not isinstance(data, dict):
        return
    netif = data.get("data", data).get("netif", {})
    lora, vip = netif.get("address"), netif.get("vip_address")
    if not (lora or vip):
        return
    try:
        cache = json.load(open(NODE_ADDRS))
    except Exception:
        cache = {}
    cache[name] = {"lora": lora, "fwvip": vip, "seen": int(time.time()),
                   "source": "device"}
    tmp = NODE_ADDRS + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(cache, indent=1))
    os.replace(tmp, NODE_ADDRS)


def node_get(node, path):
    """Two-leg fetch: LAN url, then the node's live HaLow lease by MAC.
    Returns (data, path_name, via_host); raises with per-leg detail when
    both fail. Which path answered is reported, never guessed."""
    name = node.get("name", "?")
    st = _NPATH.setdefault(name, {"pref": "lan", "fails": 0, "oks": 0,
                                  "changed": 0, "last_recheck": 0})
    lan_host = node.get("url", "").split("://", 1)[-1].split("/", 1)[0]
    legs = [("lan", node.get("url"))]
    hip = _lease_ip_for_mac((node.get("mac") or "").lower())
    if hip:
        legs.append(("halow", f"https://{hip}"))
    if st["pref"] == "halow":
        legs.reverse()
    attempts = []
    result = None
    for leg, url in legs:
        if not url:
            continue
        try:
            data = _fetch_leg(url, node.get("token"), path)
            result = (data, leg,
                      url.split("://", 1)[-1].split("/", 1)[0])
            break
        except Exception as e:
            attempts.append({"path": leg,
                             "via": url.split("://", 1)[-1].split("/", 1)[0],
                             "error": str(e)[:120]})
    now = time.time()
    lan_tried = any(a["path"] == "lan" for a in attempts) or \
        (result and result[1] == "lan")
    lan_ok = bool(result and result[1] == "lan")
    if lan_tried:
        if lan_ok:
            st["oks"] += 1
            st["fails"] = 0
            if st["pref"] == "halow" and st["oks"] >= PROMOTE_AFTER:
                st["pref"], st["changed"] = "lan", int(now)
        else:
            st["fails"] += 1
            st["oks"] = 0
            if st["pref"] == "lan" and st["fails"] >= DEMOTE_AFTER:
                st["pref"], st["changed"] = "halow", int(now)
    elif st["pref"] == "halow" and now - st["last_recheck"] > LAN_RECHECK_S:
        # demoted: one bounded LAN recheck per 30s feeds the counters
        st["last_recheck"] = now
        try:
            req = urllib.request.Request(node["url"], method="HEAD")
            urllib.request.urlopen(req, timeout=2, context=_node_ssl())
            st["oks"] += 1
            st["fails"] = 0
            if st["oks"] >= PROMOTE_AFTER:
                st["pref"], st["changed"] = "lan", int(now)
        except Exception:
            st["oks"] = 0
    if result is None:
        raise RuntimeError(json.dumps({"error": "all paths failed",
                                       "attempts": attempts}))
    _harvest_addrs(name, path, result[0])
    return result


@app.get("/api/nodes")
@authed
def api_nodes():
    """Cached fleet view from halow-watch — a file read, zero node
    contact (each live fetch costs a node 5-6s and one of ~6 TLS
    sessions [M]; two sessions once reset a node). ?live=1 keeps the
    old serial fetch as a deliberate operator action. watcher_stale is
    computed HERE from the cache's own t — a dead watcher must never
    look like a healthy fleet."""
    if request.args.get("live") != "1":
        try:
            cache = json.load(open("/var/lib/halow/nodewatch.json"))
        except Exception:
            return jsonify({"nodes": [], "cached": True,
                            "watcher_stale": True,
                            "error": "no watcher cache yet — is "
                            "halow-watch running?"})
        age = max(0, int(time.time() - cache.get("t", 0)))
        cache["cached"] = True
        cache["age_s"] = age
        cache["watcher_stale"] = age > 3 * cache.get("interval_s", 120)
        return jsonify(cache)
    try:
        with open(NODES_CONF) as f:
            nodes = json.load(f)["nodes"]
    except Exception:
        return jsonify({"nodes": [], "error": "no nodes.json configured"})
    out = []
    for n in nodes:
        entry = {"name": n["name"], "url": n["url"]}
        try:
            data, path, via = node_get(n, "/api/diag")
            entry["diag"] = data
            entry["reachable"] = True
            entry["path"], entry["via"] = path, via
        except Exception as e:
            entry["reachable"] = False
            entry["error"] = str(e)[:300]
        entry["proxy"] = dict(_NPATH.get(n["name"], {}))
        entry["proxy"].pop("last_recheck", None)
        out.append(entry)
    return jsonify({"nodes": out, "cached": False})


@app.post("/api/config/nodes")
@authed
def api_config_nodes():
    op = request.form.get("op", "add")
    if op == "add":
        args = ["node", "add", f"name={request.form.get('name', '')}"]
        for k in ("mac", "url", "interval"):
            if request.form.get(k):
                args.append(f"{k}={request.form.get(k)}")
        # token write-only via stdin — never argv, never echoed
        ok, o = halowctl(args, stdin=request.form.get("token", ""))
    elif op == "del":
        sel = (f"name={request.form.get('name')}" if request.form.get("name")
               else f"mac={request.form.get('mac', '')}")
        ok, o = halowctl(["node", "del", sel])
    else:
        return jsonify({"error": "op must be add or del"}), 400
    return (jsonify({"ok": True, "output": o}) if ok
            else (jsonify({"error": o}), 500))


@app.post("/api/nodes/adopt")
@authed
def api_nodes_adopt():
    """One click: unknown associated MAC -> dnsmasq reservation +
    nodes.json entry. Token defaults to the one existing entries share
    (the bench runs one operator credential)."""
    import re as _re
    mac = request.form.get("mac", "").lower()
    ip = request.form.get("ip", "")
    name = request.form.get("name", "")
    if not _re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        return jsonify({"error": "bad mac"}), 400
    if not _re.fullmatch(r"10\.117\.0\.\d{1,3}", ip):
        return jsonify({"error": "ip must be 10.117.0.x"}), 400
    if not _re.fullmatch(r"[A-Za-z0-9-]+", name):
        return jsonify({"error": "bad name"}), 400
    ok1, o1 = halowctl(["dhcp-reserve", "add", f"mac={mac}", f"ip={ip}",
                        f"name={name}"])
    url = request.form.get("url", f"https://{ip}")
    ok2, o2 = halowctl(["node", "add", f"name={name}", f"mac={mac}",
                        f"url={url}"], stdin=request.form.get("token", ""))
    if ok1 and ok2:
        return jsonify({"ok": True, "output": f"{o1}\n{o2}"})
    return jsonify({"error": f"reserve: {o1}\nnode: {o2}"}), 500


@app.get("/api/nodes/<name>/<path:sub>")
@authed
def api_node_proxy(name, sub):
    if sub not in ("diag", "mesh", "settings", "metrics", "routes", "ip",
                   "espnow", "env", "queue", "power"):
        return jsonify({"error": "endpoint not allowed"}), 400
    try:
        with open(NODES_CONF) as f:
            nodes = {n["name"]: n for n in json.load(f)["nodes"]}
        data, path, via = node_get(nodes[name], "/api/" + sub)
        resp = jsonify(data)
        resp.headers["X-Node-Path"] = path   # headers: the node's JSON
        resp.headers["X-Node-Via"] = via     # body passes through untouched
        return resp
    except KeyError:
        return jsonify({"error": f"unknown node {name}"}), 404
    except RuntimeError as e:
        try:
            return jsonify(json.loads(str(e))), 502
        except Exception:
            return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


REACH_BUDGET_S = 20
REACH_TTL_S = 20
_REACH_CACHE = {"t": 0, "data": None}
_REACH_LOCK = None  # created lazily (threading import stays in __main__)
_ADDR_RE = None


def _valid_addr(a):
    """Lease IPs and device-reported addresses are external input; a
    hostile or corrupted node must not reach sh()."""
    import re as _re
    return bool(a and _re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", a))


def _has_route(addr):
    """A probe without a specific route exits via the default and the
    guaranteed failure would read as a node fault (the vip.py trap)."""
    out = sh(f"ip route show to match {addr}")
    return any(not l.startswith("default") for l in out.splitlines() if l)


def _probe(addr, deadline):
    if time.monotonic() > deadline:
        return {"status": "skipped"}
    r = {}
    t0 = time.monotonic()
    p = subprocess.run(["ping", "-c", "1", "-W", "1", "-n", addr],
                       capture_output=True)
    r["icmp"] = {"ok": p.returncode == 0}
    if p.returncode == 0:
        r["icmp"]["ms"] = round((time.monotonic() - t0) * 1000, 1)
    # HTTP HEAD, never a bare TCP connect (bare :443 connects crashed
    # nodes). Plain-HTTP report first: answers without costing the node
    # one of its ~6 TLS sessions.
    for url, ctx in ((f"http://{addr}/json/report", None),
                     (f"https://{addr}/", _node_ssl())):
        if time.monotonic() > deadline:
            r["http"] = {"ok": False, "error": "budget exhausted"}
            break
        try:
            t1 = time.monotonic()
            req = urllib.request.Request(url, method="HEAD")
            resp = urllib.request.urlopen(req, timeout=2, context=ctx)
            r["http"] = {"ok": True, "status": resp.status, "url": url,
                         "ms": round((time.monotonic() - t1) * 1000, 1)}
            break
        except urllib.error.HTTPError as e:
            r["http"] = {"ok": True, "status": e.code, "url": url,
                         "note": "server answered - the path is up"}
            break
        except Exception as e:
            r["http"] = {"ok": False, "error": str(e)[:80]}
    return r


@app.get("/api/topology")
@authed
def api_topology():
    """Unified mesh graph built by halow-watch from every evidence
    source (gateway-side halow/wifi/ethernet + each node's LoRa/ESP-NOW
    peer graph). A file read — costs the nodes nothing."""
    try:
        topo = json.load(open("/var/lib/halow/topology.json"))
    except Exception:
        return jsonify({"nodes": [], "edges": [], "error":
                        "no topology yet — is halow-watch running?"})
    topo["age_s"] = max(0, int(time.time() - topo.get("t", 0)))
    return jsonify(topo)


@app.get("/api/role")
@authed
def api_role():
    """Current role + the validated bridge/gateway config check."""
    env = load_kv(ENV_CONF)
    out = {"role": env.get("HALOW_ROLE", "gateway"),
           "uplink": env.get("HALOW_BRIDGE_UPLINK", "halow"),
           "eth": env.get("HALOW_BRIDGE_ETH", "mgmt"),
           "rollback_armed":
           sh("systemctl is-active halow-role-rollback.timer").strip()
           == "active",
           "ap_active": sh("systemctl is-active halow-ap").strip(),
           "sta_active": sh("systemctl is-active halow-sta").strip()}
    if request.args.get("check") == "1":
        out["check"] = sh("sudo /usr/local/bin/halowctl role check",
                          timeout=15)
    return jsonify(out)


@app.post("/api/config/role")
@authed
def api_config_role():
    """Role switch. Bridge set arms a dead-man rollback (see halowctl
    role). Identity/network change -> confirm=1 required for anything
    other than the safe 'commit'."""
    op = request.form.get("op", "")
    if op == "commit":
        ok, o = halowctl(["role", "commit"])
    elif op == "gateway":
        if request.form.get("confirm") != "1":
            return jsonify({"error": "needs confirm=1: reconfigures NAT/"
                            "DHCP"}), 400
        ok, o = halowctl(["role", "set-gateway"], timeout=40)
    elif op == "bridge":
        if request.form.get("confirm") != "1":
            return jsonify({"error": "needs confirm=1: switches the Pi to a "
                            "downstream bridge; arms a rollback"}), 400
        import re as _re
        args = ["role", "set-bridge"]
        up = request.form.get("uplink", "halow")
        if up not in ("halow", "wifi"):
            return jsonify({"error": "uplink must be halow or wifi"}), 400
        args.append(f"uplink={up}")
        eth = request.form.get("eth")
        if eth in ("mgmt", "downlink"):
            args.append(f"eth={eth}")
        hold = request.form.get("hold", "300")
        if not hold.isdigit():
            return jsonify({"error": "hold must be numeric"}), 400
        args.append(f"hold={hold}")
        ok, o = halowctl(args, timeout=60)
    else:
        return jsonify({"error": "op must be gateway|bridge|commit"}), 400
    return (jsonify({"ok": True, "output": o}) if ok
            else (jsonify({"error": o}), 500))


@app.get("/api/reach")
@authed
def api_reach():
    """Per-node reachability matrix across every address family: LAN url,
    live HaLow lease (by mac), device-stated LoRa/VIP addresses. The
    one-call answer to 'which of this node's addresses actually route?'
    Bounded (20s budget, 20s cache) and button-driven — never on render."""
    global _REACH_LOCK
    if _REACH_LOCK is None:
        import threading
        _REACH_LOCK = threading.Lock()
    now = time.time()
    with _REACH_LOCK:
        if _REACH_CACHE["data"] and now - _REACH_CACHE["t"] < REACH_TTL_S:
            out = dict(_REACH_CACHE["data"])
            out["cached"] = True
            out["age_s"] = int(now - _REACH_CACHE["t"])
            return jsonify(out)
        deadline = time.monotonic() + REACH_BUDGET_S
        t_start = time.monotonic()
        try:
            nodes = json.load(open(NODES_CONF)).get("nodes", [])
        except Exception:
            nodes = []
        try:
            addrs = json.load(open(NODE_ADDRS))
        except Exception:
            addrs = {}
        result = []
        for n in nodes:
            name = n.get("name", "?")
            mac = (n.get("mac") or "").lower()
            paths = []
            lan = n.get("url", "").split("://", 1)[-1].split("/", 1)[0]\
                .split(":", 1)[0]
            if _valid_addr(lan):
                paths.append({"family": "lan", "addr": lan,
                              **_probe(lan, deadline)})
            else:
                paths.append({"family": "lan", "status": "bad-addr"})
            if not mac:
                paths.append({"family": "halow", "status": "no-mac"})
            else:
                hip = _lease_ip_for_mac(mac)
                if not hip:
                    paths.append({"family": "halow", "status": "no-lease"})
                elif not _valid_addr(hip):
                    paths.append({"family": "halow", "status": "bad-addr"})
                else:
                    paths.append({"family": "halow", "addr": hip,
                                  **_probe(hip, deadline)})
            known = addrs.get(name, {})
            for fam in ("lora", "fwvip"):
                a = known.get(fam)
                if not a:
                    paths.append({"family": fam, "status": "unknown"})
                elif not _valid_addr(a):
                    paths.append({"family": fam, "status": "bad-addr"})
                elif not _has_route(a):
                    paths.append({"family": fam, "addr": a,
                                  "status": "no-route"})
                else:
                    paths.append({"family": fam, "addr": a,
                                  **_probe(a, deadline)})
            result.append({"name": name, "mac": mac or None,
                           "paths": paths})
        data = {"generated": int(now), "cached": False, "age_s": 0,
                "elapsed_s": round(time.monotonic() - t_start, 1),
                "budget_s": REACH_BUDGET_S, "nodes": result}
        _REACH_CACHE["t"] = now
        _REACH_CACHE["data"] = data
        return jsonify(data)


# ---------- System ----------

def _kernel_guard():
    """kernel-guard.json passthrough; missing file is a warning, never
    silently ok — a box without the guard deployed should say so."""
    try:
        return json.load(open("/var/lib/halow/kernel-guard.json"))
    except Exception:
        return {"state": "unknown"}


@app.post("/api/system/driver-rebuild")
@authed
def api_driver_rebuild():
    subprocess.Popen(["sudo", "/usr/local/bin/halowctl", "driver",
                      "rebuild"])
    return jsonify({"ok": True, "output": "rebuild dispatched — watch "
                    "kernel_guard.state (rebuilding -> ok/held)"})


@app.post("/api/system/driver-hold")
@authed
def api_driver_hold():
    if request.form.get("confirm") != "1":
        return jsonify({"error": "needs confirm=1: changes apt kernel "
                        "policy"}), 400
    op = request.form.get("op", "hold")
    if op not in ("hold", "unhold"):
        return jsonify({"error": "op must be hold or unhold"}), 400
    ok, out = halowctl(["driver", op])
    return (jsonify({"ok": True, "output": out}) if ok
            else (jsonify({"error": out}), 500))


def time_sync():
    """chrony tracking -> synced|holdover|unknown + the raw fields.
    Raw values are always exposed beside the classification (detect the
    event, classify separately — the operator is never hostage to our
    classifier). ref_id 7F7F0101 = 127.127.1.1, chrony's local
    reference = serving holdover thanks to 'local stratum 10'."""
    out = sh("chronyc -c tracking")
    if not out or "," not in out:
        return {"state": "unknown"}
    try:
        f = out.strip().split(",")
        ref_id, stratum, leap = f[0].upper(), int(f[2]), f[-1]
        state = ("holdover" if ref_id == "7F7F0101"
                 else "synced" if leap == "Normal" else "holdover")
        ref_time = float(f[3])
        # drilled 2026-08-05: ref_time FREEZES at holdover entry (0.0s
        # advance over 30s wall), so an age derived from it is honest —
        # under holdover it approximates time since the last real sync
        return {"state": state, "stratum": stratum, "ref_id": ref_id,
                "leap": leap, "ref_time": round(ref_time, 1),
                "ref_age_s": max(0, int(time.time() - ref_time)),
                "offset_ms": round(float(f[4]) * 1000, 3)}
    except Exception:
        return {"state": "unknown"}


@app.get("/api/system")
@authed
def api_system():
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = v.strip()
    # DISK_LOW_MB mirrored in scripts/halow-mon (the sampler's copy)
    vfs = os.statvfs("/")
    total_mb = vfs.f_blocks * vfs.f_frsize // 1048576
    free_mb = vfs.f_bavail * vfs.f_frsize // 1048576
    return jsonify({
        "uptime": sh("uptime -p").strip(),
        "load": open("/proc/loadavg").read().split()[:3],
        "mem": mem,
        "temp": sh("cat /sys/class/thermal/thermal_zone0/temp").strip(),
        "kernel": sh("uname -r").strip(),
        "kernel_guard": _kernel_guard(),
        "time_sync": time_sync(),
        "disk": {"total_mb": total_mb, "free_mb": free_mb,
                 "used_pct": round(100 * (1 - free_mb / total_mb), 1)
                 if total_mb else 0,
                 "low": free_mb < DISK_LOW_MB, "low_water_mb": DISK_LOW_MB},
    })


@app.get("/")
@authed
def index():
    return Response(PAGE, mimetype="text/html")


LOGIN_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>halow-gw login</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#c9d1d9;--dim:#8b949e;
--acc:#58a6ff;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,Segoe UI,Roboto,monospace;display:flex;
align-items:center;justify-content:center;min-height:100vh}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:32px 36px;width:320px}
h1{font-size:18px;margin:0 0 4px;color:var(--acc)}
p.sub{color:var(--dim);margin:0 0 20px;font-size:12px}
label{display:block;color:var(--dim);font-size:12px;margin:12px 0 4px}
input{width:100%;background:#0d1117;border:1px solid var(--line);
color:var(--fg);border-radius:6px;padding:9px 10px;font:inherit}
input:focus{outline:none;border-color:var(--acc)}
button{width:100%;margin-top:20px;background:var(--acc);color:#0d1117;
border:none;border-radius:6px;padding:10px;font:inherit;font-weight:600;
cursor:pointer}button:hover{filter:brightness(1.1)}
.err{color:var(--bad);font-size:13px;margin:12px 0 0}
</style></head><body>
<form class="card" method="post" action="/login">
<h1>halow-gw</h1>
<p class="sub">mesh gateway · 192.168.51.202</p>
<label for="u">username</label>
<input id="u" name="username" autocomplete="username" autofocus>
<label for="p">password</label>
<input id="p" name="password" type="password" autocomplete="current-password">
__ERROR__
<button type="submit">sign in</button>
</form></body></html>"""


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>halow-gw</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#c9d1d9;--dim:#8b949e;
--acc:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,Segoe UI,Roboto,monospace}
header{display:flex;align-items:center;gap:16px;padding:10px 16px;
border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0;color:var(--acc)}
nav{display:flex;gap:2px}nav button{background:none;border:none;color:var(--dim);
padding:8px 14px;cursor:pointer;font:inherit;border-bottom:2px solid transparent}
nav button.on{color:var(--fg);border-color:var(--acc)}
main{padding:16px;max-width:1100px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;
padding:12px 16px;margin-bottom:14px}
.card h2{font-size:13px;margin:0 0 8px;color:var(--dim);text-transform:uppercase;
letter-spacing:.06em}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:3px 10px 3px 0;
border-bottom:1px solid var(--line);font-size:13px}th{color:var(--dim)}
pre{white-space:pre-wrap;font-size:12px;color:var(--dim);margin:4px 0;max-height:260px;overflow:auto}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
button.act{background:#21262d;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:5px 12px;cursor:pointer;font:inherit}
button.act:hover{border-color:var(--acc)}button.act:disabled{opacity:.4;cursor:default}
input{background:#0d1117;border:1px solid var(--line);color:var(--fg);
border-radius:6px;padding:5px 8px;font:inherit;width:110px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}
.stat{background:#0d1117;border:1px solid var(--line);border-radius:6px;padding:8px 12px}
.stat .v{font-size:18px}.stat .k{color:var(--dim);font-size:12px}
</style></head><body>
<header><h1>halow-gw</h1><nav id="nav"></nav>
<span id="clock" style="margin-left:auto;color:var(--dim)"></span>
<a href="/logout" style="color:var(--dim);margin-left:14px;text-decoration:none">logout</a></header>
<main id="main"></main>
<script>
const TABS=["Overview","HaLow","Router","Config","Nodes","Map","Camera","Diag","Debug"];let tab="Overview";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function j(u,opt){const r=await fetch(u,opt);if(!r.ok)throw new Error(r.status);return r.json()}
function nav(){$("#nav").innerHTML=TABS.map(t=>
 `<button class="${t===tab?'on':''}" onclick="tab='${t}';render()">${t}</button>`).join("")}
async function render(){nav();const m=$("#main");m.innerHTML="<p class='warn'>loading…</p>";
 try{ if(tab==="Overview")m.innerHTML=await ovw();
 else if(tab==="HaLow")m.innerHTML=await halow();
 else if(tab==="Router")m.innerHTML=await router();
 else if(tab==="Config")m.innerHTML=await config();
 else if(tab==="Debug")m.innerHTML=await debug();
 else if(tab==="Diag")m.innerHTML=await diag();
 else if(tab==="Map"){m.innerHTML=await meshmap();drawMesh()}
 else if(tab==="Camera")m.innerHTML=await camera();
 else m.innerHTML=await nodes();}catch(e){m.innerHTML=`<p class="bad">${esc(e)}</p>`}}
async function diag(){const nb=await j("/api/diag/neigh"),sv=await j("/api/diag/survey"),pw=await j("/api/diag/power");
 let bo={count:0,active:false};try{bo=await j("/api/diag/brownouts")}catch(e){}
 const neigh=nb.neighbors.length?nb.neighbors.map(x=>{
  const c=x.state==="REACHABLE"?"ok":(x.state==="FAILED"?"bad":"warn");
  return `<tr><td>${esc(x.ip)}</td><td>${esc(x.dev)}</td><td>${esc(x.mac)}</td><td class="${c}">${esc(x.state)}</td></tr>`}).join("")
  :`<tr><td colspan=4 class="warn">none</td></tr>`;
 return `<div class="card"><h2>ping</h2>
 <p>target <input id="p-t" placeholder="10.117.0.50 or 192.168.50.1">
 count <input id="p-n" value="5" style="width:50px">
 <button class="act" onclick="dPing(this)">run</button></p><pre id="p-out"></pre></div>
 <div class="card"><h2>service check (HTTP HEAD — never bare connects)</h2>
 <p>host <input id="c-h" placeholder="192.168.50.103"> port <input id="c-p" value="443" style="width:60px">
 <label><input type="checkbox" id="c-tls" checked style="width:auto">TLS</label>
 <button class="act" onclick="dTcp(this)">check</button></p><pre id="c-out"></pre></div>
 <div class="card"><h2>DNS check</h2>
 <p>name <input id="d-n" placeholder="example.com"> server <input id="d-s" placeholder="(system)">
 <button class="act" onclick="dDns(this)">resolve</button></p><pre id="d-out"></pre></div>
 <div class="card"><h2>neighbors (ARP/NDP)</h2>
 <table><tr><th>ip</th><th>dev</th><th>mac</th><th>state</th></tr>${neigh}</table>
 <p style="color:var(--dim)">FAILED with a known mac = the "associated but answers no ARP" trap —
 auto-classified each minute as <code>leased-no-arp</code> by the health ladder (HaLow tab)
 and as <code>dhcp_no_reach</code> at join time in the Debug tab's forensics. This manual
 view stays useful when the timer itself is suspect.</p></div>
 <div class="card"><h2>power / throttling</h2>
 <p>${pw.temp_c}°C · ${esc(pw.volts_core||"")} · ${pw.flags.map(f=>
  `<span class="${f.includes("NOW")?"bad":(f.includes("occurred")?"warn":"ok")}">${esc(f)}</span>`).join(" · ")}</p>
 <p>brownouts: ${bo.count}${bo.active?' <span class="bad">UNDERVOLTAGE ACTIVE — high-draw ops declined</span>':''}${bo.last_event?` (last: ${esc(bo.last_event.event)} ${new Date(bo.last_event.t*1000).toISOString().slice(5,16).replace("T"," ")})`:''}</p></div>
 <div class="card"><h2>channel survey</h2>
 <p>utilization: ${sv.utilization_pct!=null?sv.utilization_pct+"%":"n/a"}</p>
 <pre>${esc(sv.surveys.map(s=>JSON.stringify(s)).join("\n").slice(0,1500))}</pre></div>
 <div class="card"><h2>packet capture (halow0)</h2>
 <p>seconds <input id="cap-s" value="10" style="width:50px">
 <button class="act" onclick="capRun(this)">capture</button>
 <a class="act" style="text-decoration:none;padding:6px 12px" href="/api/diag/capture">download last .pcap</a></p>
 <pre id="cap-out"></pre></div>
 <div class="card"><h2>more</h2>
 <p><button class="act" onclick="dLoad('/api/diag/flows','flows',d=>d.flows.join("\n")||"(none)")">NAT flows</button>
 <button class="act" onclick="dLoad('/api/diag/chip','chip',d=>d.output)">chip counters</button>
 <a class="act" style="text-decoration:none;padding:6px 12px" href="/api/diag" target="_blank">full diag bundle (JSON)</a></p>
 <pre id="m-out"></pre></div>`}
async function capRun(b){b.disabled=true;b.textContent="capturing…";
 try{const fd=new FormData();fd.append("seconds",$("#cap-s").value);
 const d=await(await fetch("/api/diag/capture",{method:"POST",body:fd})).json();
 $("#cap-out").textContent=d.error||d.output}finally{b.disabled=false;b.textContent="capture"}}
async function dPing(b){b.disabled=true;try{const fd=new FormData();
 fd.append("target",$("#p-t").value);fd.append("count",$("#p-n").value);
 const d=await(await fetch("/api/diag/ping",{method:"POST",body:fd})).json();
 $("#p-out").textContent=d.error||`${d.received}/${d.sent} received, loss ${d.loss_pct}%  rtt ${d.min_ms}/${d.avg_ms}/${d.max_ms} ms\nprobes: ${d.probes_ms.join(", ")}\n${d.note}`}finally{b.disabled=false}}
async function dTcp(b){b.disabled=true;try{const fd=new FormData();
 fd.append("host",$("#c-h").value);fd.append("port",$("#c-p").value);
 fd.append("tls",$("#c-tls").checked?"1":"0");
 const d=await(await fetch("/api/diag/tcpcheck",{method:"POST",body:fd})).json();
 $("#c-out").textContent=d.error?`${d.url}: ${d.error} (${d.ms} ms)`:`${d.url}: HTTP ${d.status} in ${d.ms} ms ${d.note||""}`}finally{b.disabled=false}}
async function dDns(b){b.disabled=true;try{const fd=new FormData();
 fd.append("name",$("#d-n").value);if($("#d-s").value)fd.append("server",$("#d-s").value);
 const d=await(await fetch("/api/diag/dns",{method:"POST",body:fd})).json();
 $("#d-out").textContent=d.error||`${d.name} via ${d.server}: ${d.ok?d.answers.join(", "):"NO ANSWER"} (${d.ms} ms)`}finally{b.disabled=false}}
async function dLoad(url,label,fmt){const d=await j(url);
 $("#m-out").textContent=`== ${label}\n`+fmt(d)}
async function debug(){const ev=await j("/api/halow/events"),tp=await j("/api/halow/throughput");
 let jl={stations:{}};try{jl=await j("/api/halow/join-log")}catch(e){}
 let lt={sessions:[],responder:{}};try{lt=await j("/api/halow/linktest")}catch(e){}
 const ltrows=lt.sessions.slice(0,8).map(s=>
  `<tr><td>${new Date(s.t*1000).toISOString().slice(5,19).replace("T"," ")}</td>
   <td>${esc(s.src_ip)}</td><td>${esc(s.iface)}</td><td>${s.size_bytes}B</td>
   <td>${s.received_unique}/${s.delivery.sent}</td>
   <td class="${s.delivery.ratio>=0.95?'ok':s.delivery.ratio>=0.5?'warn':'bad'}">${(100*s.delivery.ratio).toFixed(1)}%</td>
   <td>${s.goodput_bps?(s.goodput_bps/1000).toFixed(1)+" kbps":"—"}</td>
   <td>${s.phy_rate_bps?(s.phy_rate_bps/1e6).toFixed(1)+" Mbps":"—"}</td></tr>`).join("")
  ||`<tr><td colspan=8 class="warn">no sessions recorded</td></tr>`;
 const drops=lt.responder.drops?Object.entries(lt.responder.drops).map(([k,v])=>`${k}=${v}`).join(" · "):"—";
 const jrows=Object.entries(jl.stations).map(([m,s])=>
  `<tr><td><a href="#" onclick="joinDetail('${m}');return false">${esc(m)}</a></td>
   <td>${s.attempts||0}</td><td>${esc(s.last_stage||"—")}</td>
   <td class="${s.verdict==='complete'?'ok':(s.verdict==='in_progress'||s.verdict==='probe_pending')?'warn':'bad'}">${esc(s.verdict||"")}</td>
   <td>${esc(s.last_stage_at||"")}</td></tr>`).join("")
  ||`<tr><td colspan=5 class="warn">no join attempts witnessed</td></tr>`;
 const runs=tp.runs.length?tp.runs.slice(-10).reverse().map(r=>
  `<tr><td>${esc(r.at)}</td><td>${esc(r.target)}</td><td>${r.reverse?"AP→STA":"STA→AP"}</td><td>${r.mbps} Mbps</td></tr>`).join("")
  :`<tr><td colspan=4 class="warn">no runs recorded</td></tr>`;
 return `<div class="card"><h2>join forensics</h2>
 <table><tr><th>mac</th><th>attempts</th><th>last stage</th><th>verdict</th><th>at</th></tr>${jrows}</table>
 <p style="color:var(--dim)">Verdicts: silence_after_commit = RF died mid-handshake ·
 sae_confirm_failed = wrong PSK · dhcp_no_reach = the "associated but answers no ARP"
 trap, auto-detected. Per-MAC bundle: first-sight evidence snapshot.</p>
 <pre id="jl-out" style="max-height:400px"></pre></div>
 <div class="card"><h2>UDP link test (receiver-counted)</h2>
 <table><tr><th>when</th><th>source</th><th>iface</th><th>size</th><th>rcvd/decl</th><th>delivery</th><th>goodput</th><th>PHY</th></tr>${ltrows}</table>
 <p style="color:var(--dim)">drops: ${drops} · sessions ${lt.responder.active_sessions??0} active</p>
 <p><button class="act" onclick="ltSelf(this)">loopback self-test</button>
 <span style="color:var(--dim)">— sink :5202 / echo :5203, wire format v1: docs/udp-linktest-protocol.md.
 A record without iface=halow0 is a bench number, not a HaLow result.</span></p>
 <pre id="lt-out"></pre></div>
 <div class="card"><h2>throughput test (iperf3)</h2>
 <p>target IP <input id="t-ip" placeholder="10.117.0.50">
 seconds <input id="t-sec" value="5" style="width:50px">
 <label><input type="checkbox" id="t-rev" style="width:auto"> AP→STA (reverse)</label>
 <button class="act" onclick="tpRun(this)">run</button></p>
 <table><tr><th>when</th><th>target</th><th>direction</th><th>rate</th></tr>${runs}</table>
 <p style="color:var(--dim)">The gateway also runs an iperf3 server — any HaLow client can test against 10.117.0.1.</p></div>
 <div class="card"><h2>station events</h2>
 <pre>${esc(ev.events.slice(-25).join("\n")||"none recorded")}</pre></div>
 <div class="card"><h2>service logs</h2>
 <p><select id="l-unit">${["halow-ap","halow-net","halow-ui","halow-sta-events","halow-join-watch","halow-iperf3","dnsmasq","kernel"].map(u=>`<option>${u}</option>`).join("")}</select>
 <button class="act" onclick="logsLoad()">load</button></p>
 <pre id="l-out" style="max-height:400px"></pre></div>`}
async function tpRun(btn){btn.disabled=true;btn.textContent="running…";
 try{const fd=new FormData();fd.append("target",$("#t-ip").value);
 fd.append("seconds",$("#t-sec").value);if($("#t-rev").checked)fd.append("reverse","1");
 const d=await(await fetch("/api/halow/throughput",{method:"POST",body:fd})).json();
 alert(d.error||`${d.mbps} Mbps`);render()}finally{btn.disabled=false;btn.textContent="run"}}
async function logsLoad(){const u=$("#l-unit").value;
 const d=await j(`/api/logs?unit=${u}&n=200`);
 $("#l-out").textContent=d.lines.join("\n")||"(empty)"}
async function joinDetail(m){const d=await j("/api/halow/join-log/"+m);
 $("#jl-out").textContent=JSON.stringify(d,null,1)}
async function drvRebuild(btn){btn.disabled=true;
 try{const r=await(await fetch("/api/system/driver-rebuild",{method:"POST"})).json();
 const el=document.getElementById("drv-out");if(el)el.textContent=r.error||r.output;
 setTimeout(render,5000)}finally{btn.disabled=false}}
async function ltSelf(btn){btn.disabled=true;btn.textContent="running…";
 try{const r=await(await fetch("/api/halow/linktest/selftest",{method:"POST"})).json();
 $("#lt-out").textContent=JSON.stringify(r,null,1)}
 finally{btn.disabled=false;btn.textContent="loopback self-test";setTimeout(render,800)}}
async function config(){const c=await j("/api/config");
 const fwds=c.forwards.length?c.forwards.map(f=>
  `<tr><td>${esc(f.proto)}</td><td>${f.ext}</td><td>${esc(f.dest)}</td>
   <td><button class="act" onclick="fwdDel('${esc(f.proto)}',${f.ext})">delete</button></td></tr>`).join("")
  :`<tr><td colspan=4 class="warn">no port forwards</td></tr>`;
 return `<div class="card"><h2>HaLow network identity</h2>
 <p>SSID <input id="h-ssid" value="${esc(c.halow.ssid)}">
 passphrase <input id="h-pass" type="password" placeholder="${c.halow.passphrase_set?"(set — write only)":"(unset)"}">
 <button class="act" onclick="cfgHalow()">apply</button></p>
 <p style="color:var(--dim)">Changing either disconnects every station until it is reconfigured — you will be asked to confirm.</p></div>
 <div class="card"><h2>2.4 GHz AP (mesh-2g) — ${c.wifi.active?"<span class='ok'>on</span>":"<span class='warn'>off</span>"}</h2>
 <p>SSID <input id="w-ssid" value="${esc(c.wifi.ssid)}">
 channel <input id="w-chan" value="${esc(c.wifi.channel)}" style="width:60px">
 passphrase <input id="w-pass" type="password" placeholder="(write only)">
 <button class="act" onclick="cfgWifi()">apply</button>
 <button class="act" onclick="wifiAp('${c.wifi.active?"off":"on"}')">turn ${c.wifi.active?"off":"on"}</button></p></div>
 <div class="card"><h2>DHCP — HaLow net (10.117.0.0/24)</h2>
 <p>range <input id="d-start" value="${esc(c.dhcp.start)}"> – <input id="d-end" value="${esc(c.dhcp.end)}">
 lease <input id="d-lease" value="${esc(c.dhcp.lease)}" style="width:70px">
 DNS <input id="d-dns" value="${esc(c.dhcp.dns)}">
 <button class="act" onclick="cfgDhcp()">apply</button></p></div>
 <div class="card"><h2>DHCP reservations (pin node MACs to fixed addresses)</h2>
 <table><tr><th>mac</th><th>ip</th><th>name</th><th></th></tr>
 ${c.reservations.length?c.reservations.map(r=>
  `<tr><td>${esc(r.mac)}</td><td>${esc(r.ip)}</td><td>${esc(r.name)}</td>
   <td><button class="act" onclick="resDel('${esc(r.mac)}')">delete</button></td></tr>`).join("")
  :`<tr><td colspan=4 class="warn">none</td></tr>`}</table>
 <p>mac <input id="r-mac" placeholder="aa:bb:cc:dd:ee:ff"> ip <input id="r-ip" placeholder="10.117.0.50">
 name <input id="r-name" placeholder="node1"> <button class="act" onclick="resAdd()">reserve</button></p></div>
 <div class="card"><h2>Port forwards (LAN → HaLow net)</h2>
 <table><tr><th>proto</th><th>ext port</th><th>destination</th><th></th></tr>${fwds}</table>
 <p><select id="f-proto"><option>tcp</option><option>udp</option></select>
 ext <input id="f-ext" style="width:70px" placeholder="8080">
 → <input id="f-dest" placeholder="10.117.0.50:80">
 <button class="act" onclick="fwdAdd()">add</button></p></div>
 <div class="card"><h2>System</h2>
 <p><button class="act" onclick="reboot()">reboot gateway</button></p>
 <p style="border-top:1px solid #21262d;padding-top:8px">encrypted backup —
 passphrase <input id="bk-pw" type="password" placeholder="≥12 chars" style="width:150px">
 <button class="act" onclick="doBackup(this)">create</button>
 <a class="act" href="/api/system/backup" style="text-decoration:none">download latest</a>
 <span style="color:var(--dim)">identity + operator state, AES256; store the passphrase off-device (a lost passphrase = a dead backup)</span></p>
 <pre id="bk-out"></pre>
 <p style="color:var(--dim)">API: every change here is also scriptable —
 same endpoints with <code>curl -u user:pass</code> or
 <code>-H "Authorization: Bearer &lt;ADMIN_TOKEN&gt;"</code>.
 See /api/config, /api/config/{halow,wifi,dhcp,forwards}, /api/system/reboot.</p></div>
 <pre id="cfg-out"></pre>`}
async function post(u,data){const fd=new FormData();
 for(const[k,v]of Object.entries(data))if(v!==""&&v!=null)fd.append(k,v);
 const r=await fetch(u,{method:"POST",body:fd});const d=await r.json();
 const el=document.getElementById("cfg-out");
 if(el)el.textContent=d.error||d.output||"ok";
 if(!d.error)setTimeout(render,1200);return d}
async function cfgHalow(){const ssid=$("#h-ssid").value,pass=$("#h-pass").value;
 if(!ssid&&!pass)return;
 if(!confirm("Changing the HaLow identity disconnects every station. Continue?"))return;
 await post("/api/config/halow",{ssid,passphrase:pass,confirm:1})}
async function cfgWifi(){await post("/api/config/wifi",
 {ssid:$("#w-ssid").value,channel:$("#w-chan").value,passphrase:$("#w-pass").value})}
async function cfgDhcp(){await post("/api/config/dhcp",
 {start:$("#d-start").value,end:$("#d-end").value,lease:$("#d-lease").value,dns:$("#d-dns").value})}
async function fwdAdd(){await post("/api/config/forwards",
 {op:"add",proto:$("#f-proto").value,ext:$("#f-ext").value,dest:$("#f-dest").value})}
async function fwdDel(p,e){await post("/api/config/forwards",{op:"del",proto:p,ext:e})}
async function resAdd(){await post("/api/config/reservations",
 {op:"add",mac:$("#r-mac").value,ip:$("#r-ip").value,name:$("#r-name").value})}
async function resDel(m){await post("/api/config/reservations",{op:"del",mac:m})}
async function reboot(){if(confirm("Reboot the gateway now?"))
 await post("/api/system/reboot",{confirm:1})}
async function doBackup(btn){const pw=$("#bk-pw").value;
 if(pw.length<12){$("#bk-out").textContent="passphrase must be ≥12 chars";return}
 btn.disabled=true;btn.textContent="encrypting…";
 try{const fd=new FormData();fd.append("passphrase",pw);
 const r=await(await fetch("/api/system/backup",{method:"POST",body:fd})).json();
 $("#bk-pw").value="";
 $("#bk-out").textContent=r.error||`created ${r.file} · ${r.bytes} bytes · sha256 ${r.sha256}\ndownload it off-device now (the SD card is the disaster this survives)`}
 finally{btn.disabled=false;btn.textContent="create"}}
async function ovw(){const s=await j("/api/system"),h=await j("/api/halow");
 const t=(parseInt(s.temp)/1000).toFixed(1);
 return `<div class="card"><h2>gateway</h2><div class="grid">
 <div class="stat"><div class="v">${esc(s.uptime.replace("up ",""))}</div><div class="k">uptime</div></div>
 <div class="stat"><div class="v">${t}°C</div><div class="k">SoC temp</div></div>
 <div class="stat"><div class="v">${esc(s.load.join(" "))}</div><div class="k">load</div></div>
 <div class="stat"><div class="v">${esc(s.mem.MemAvailable||"?")}</div><div class="k">mem avail</div></div>
 <div class="stat"><div class="v ${h.present?'ok':'bad'}">${h.present?"present":"ABSENT"}</div><div class="k">halow0</div></div>
 <div class="stat"><div class="v ${h.ap_active==='active'?'ok':'warn'}">${esc(h.ap_active)}</div><div class="k">AP service</div></div>
 <div class="stat"><div class="v ${s.time_sync.state==='synced'?'ok':s.time_sync.state==='holdover'?'warn':'bad'}">${s.time_sync.state==='synced'?('s'+s.time_sync.stratum):s.time_sync.state==='holdover'?'HOLDOVER':'no time'}</div><div class="k">NTP</div></div>
 </div></div>
 ${s.kernel_guard.state!=="ok"?`<div class="card"><h2 class="${s.kernel_guard.state==="unknown"?"warn":"bad"}">kernel guard: ${esc(s.kernel_guard.state)}</h2>
 <p>${s.kernel_guard.state==="unknown"?"guard state file missing — guard not deployed or never ran":
 `next-boot kernel ${esc(s.kernel_guard.next_boot_kernel||"?")} module=${s.kernel_guard.next_boot_has_module} · running ${esc(s.kernel_guard.running_kernel||"?")} module=${s.kernel_guard.running_has_module} — a reboot in this state kills the AP`}</p>
 <p><button class="act" onclick="drvRebuild(this)">rebuild driver</button></p><pre id="drv-out"></pre></div>`:""}
 <div class="card"><h2>kernel / disk</h2><pre>${esc(s.kernel)}  root <span class="${s.disk.low?'bad':'ok'}">${s.disk.free_mb} MB free</span> of ${s.disk.total_mb} MB (${s.disk.used_pct}% used)${s.disk.low?' — BELOW '+s.disk.low_water_mb+' MB LOW-WATER':''}</pre></div>
 ${await monCard()}`}
async function monCard(){try{const m=await j("/api/metrics?minutes=1440");
 if(!m.n)return `<div class="card"><h2>monitor (24h)</h2><p class="warn">no samples yet</p></div>`;
 const s=m.summary,mo=m.monitor;
 return `<div class="card"><h2>monitor — last 24h (${m.n} samples)</h2>
 <div class="grid">
 <div class="stat"><div class="v">${s.temp_c.now}°C</div><div class="k">temp (${s.temp_c.min}–${s.temp_c.max})</div></div>
 <div class="stat"><div class="v">${Math.round(s.mem_low_water_kb/1024)} MB</div><div class="k">mem low-water</div></div>
 <div class="stat"><div class="v">${s.disk_free_low_water_mb!=null?s.disk_free_low_water_mb+" MB":"—"}</div><div class="k">disk low-water</div></div>
 <div class="stat"><div class="v">${s.uptime_pct.ap}%</div><div class="k">AP beaconing</div></div>
 <div class="stat"><div class="v">${s.uptime_pct.upstream}%</div><div class="k">upstream reachable</div></div>
 <div class="stat"><div class="v">${(mo.ap_restarts||0)+(mo.dnsmasq_restarts||0)+(mo.eth0_bounces||0)}</div><div class="k">heal actions (ap ${mo.ap_restarts||0} / dns ${mo.dnsmasq_restarts||0} / eth0 ${mo.eth0_bounces||0})</div></div>
 <div class="stat"><div class="v">${s.stations.now}</div><div class="k">stations (max ${s.stations.max})</div></div>
 </div>${(mo.actions&&mo.actions.length)?`<pre>${esc(mo.actions.slice(-5).map(a=>a.at+" "+a.action).join("\n"))}</pre>`:""}</div>`}
 catch(e){return `<div class="card"><h2>monitor</h2><p class="bad">${esc(e)}</p></div>`}}
async function halow(){const h=await j("/api/halow");
 let cc=null;try{cc=await j("/api/halow/compat")}catch(e){}
 let lk={stations:{}};try{lk=await j("/api/halow/link")}catch(e){}
 let rcx={stations:{}};try{rcx=await j("/api/halow/rungcost")}catch(e){}
 const hcls=v=>v==="reachable"?"ok":(v==="quiet"||v==="never-seen"||v==="assoc-no-lease")?"warn":"bad";
 const ago=t=>t?new Date(t*1000).toISOString().replace("T"," ").slice(0,19):"—";
 const profs=Object.entries(h.profiles).map(([n,p])=>{
  const v=cc&&cc.profiles&&cc.profiles[n];
  const badge=v&&v.compatible===false?'<span class="warn">strands pinned nodes</span>':"";
  return `<tr><td>${n===h.profile?"▶":""}</td><td>${esc(n)}</td><td>${p.width_mhz} MHz</td>
   <td>ch ${p.channel}</td><td>op ${p.op_class}</td><td>${badge}</td>
   <td><button class="act" ${n===h.profile?"disabled":""}
     onclick="setProf('${n}')">apply</button></td></tr>`}).join("");
 const stas=h.stations.length?h.stations.map(s=>{
  const m=(s.mac||"").toLowerCase();
  const hh=(lk.stations[m]||{}).health;
  const rr=rcx.stations[m];
  const rung=rr?(rr.stale?`<span class="bad">stale</span>`:
   rr.healthy?`<span class="ok">${rr.cost}</span>`:
   `<span class="warn">unhealthy</span>`):"—";
  return `<tr><td>${esc(s.mac)}</td><td>${esc(s.signal||"")}</td><td>${esc(s.tx_bitrate||"")}</td>
   <td>${esc(s.rx_bitrate||"")}</td><td>${esc(s.connected_time||"")}</td>
   <td class="${hh?hcls(hh.state):''}">${esc(hh?hh.state:"—")}</td><td>${rung}</td></tr>`}).join("")
  :`<tr><td colspan=7 class="warn">no stations associated</td></tr>`;
 const known=Object.entries(lk.stations).filter(([m,v])=>v.health).map(([m,v])=>
  `<tr><td>${esc(m)}</td><td class="${hcls(v.health.state)}">${esc(v.health.state||"")}</td>
   <td>${esc(v.health.ip||"—")}</td><td>${v.health.expected_interval_s||"—"}s</td>
   <td>${ago(v.health.last_seen_assoc)}</td></tr>`).join("")
  ||`<tr><td colspan=5 class="warn">no health records yet</td></tr>`;
 return `<div class="card"><h2>radio — ${esc(h.ssid||"?")} · mode ${esc(h.mode)} (${h.present?"interface up":"<span class='bad'>interface absent</span>"})</h2>
 <pre>${esc(h.iw_info||h.chip_dmesg)}</pre>
 <p><button class="act" onclick="probe(this)">Probe chip</button>
 <button class="act" onclick="setMode('${h.mode==="ap"?"sta":"ap"}')">Switch to ${h.mode==="ap"?"STA (join mesh)":"AP (broadcast mesh)"}</button></p>
 <pre id="probe-out"></pre></div>
 <div class="card"><h2>profiles — range ⟷ rate</h2>
 <table><tr><th></th><th>profile</th><th>width</th><th>channel</th><th>op class</th><th>node compat</th><th></th></tr>${profs}</table>
 <p style="color:var(--dim)">override: ch <input id="ch" placeholder="${esc(h.channel_override||'auto')}">
 width <input id="w" placeholder="${esc(h.width_override||'auto')}">
 <button class="act" onclick="setOvr()">apply</button>
 — valid US: 1MHz odd 1-51 · 2MHz 2,6..50 · 4MHz 8,16..48 · 8MHz 12,28,44</p>
 <pre id="halow-out"></pre></div>
 <div class="card"><h2>stations</h2>
 <table><tr><th>mac</th><th>signal</th><th>tx</th><th>rx</th><th>connected</th><th>health</th><th>rung cost</th></tr>${stas}</table></div>
 <div class="card"><h2>known stations (health ladder)</h2>
 <table><tr><th>mac</th><th>state</th><th>ip</th><th>interval</th><th>last assoc</th></tr>${known}</table>
 <p style="color:var(--dim)">assoc→lease→ARP→ICMP walked each minute; absent stations judged by
 sleep-aware freshness (3× their own interval) and never probed — quiet is healthy, lost is not.</p></div>`}
async function hpost(u,body){const fd=new FormData();
 for(const[k,v]of Object.entries(body))if(v!==""&&v!=null)fd.append(k,v);
 const r=await fetch(u,{method:"POST",body:fd});const d=await r.json();
 const el=document.getElementById("halow-out");
 if(el)el.textContent=d.error||d.output||"ok";
 if(!d.error)setTimeout(render,1200);return d}
async function preflight(qs){try{const c=await j("/api/halow/compat?"+qs);
 const cd=c.candidate;if(!cd||cd.compatible!==false)return {};
 const p=c.presence||{};
 const armed=p.guard_armed?` — ${p.stations} station(s) / ${p.reservations} reservation(s) will be stranded.`:"";
 if(!confirm(`${cd.reason||"outside the node pinned scan set"}${armed} Apply anyway?`))return null;
 return {confirm:1}}catch(e){return {}}}
async function setProf(n){
 const extra=await preflight("profile="+encodeURIComponent(n));
 if(extra===null)return;
 await hpost("/api/halow/profile",Object.assign({name:n},extra))}
async function probe(btn){btn.disabled=true;btn.textContent="probing…";
 try{const r=await(await fetch("/api/halow/probe",{method:"POST"})).json();
 const el=document.getElementById("probe-out");
 el.textContent=r.output;el.className=r.ok?"ok":"bad"}finally{btn.disabled=false;btn.textContent="Probe chip"}}
async function setMode(m){const fd=new FormData();fd.append("mode",m);
 await fetch("/api/halow/mode",{method:"POST",body:fd});render()}
async function wifiAp(s){const fd=new FormData();fd.append("state",s);
 await fetch("/api/router/wifi-ap",{method:"POST",body:fd});setTimeout(render,1500)}
async function setOvr(){
 const ch=$("#ch").value,w=$("#w").value;
 if(!ch&&!w)return;
 const qs=[];if(ch)qs.push("channel="+encodeURIComponent(ch));
 if(w)qs.push("width="+encodeURIComponent(w));
 const extra=await preflight(qs.join("&"));
 if(extra===null)return;
 await hpost("/api/halow/set",Object.assign({channel:ch,width:w},extra))}
async function router(){const r=await j("/api/router");
 let rl={role:"gateway"};try{rl=await j("/api/role")}catch(e){}
 const roleCard=`<div class="card"><h2>role — <span class="${rl.role==="gateway"?"ok":"warn"}">${esc(rl.role)}</span>${rl.role==="bridge"?` (uplink ${esc(rl.uplink)}, eth0 ${esc(rl.eth)})`:""}${rl.rollback_armed?' · <span class="bad">ROLLBACK ARMED</span>':""}</h2>
 <p style="color:var(--dim)">gateway = HaLow AP + eth0 upstream NAT (today). bridge = the Pi joins
 another network as a client (uplink) and serves local devices (downlink), NAT toward the uplink.
 A live switch reconfigures the interface you manage through, so bridge arms a dead-man rollback.</p>
 <p>${rl.rollback_armed
  ?`<button class="act" onclick="roleOp('commit')">commit (keep bridge)</button>
    <button class="act" onclick="roleOp('gateway',1)">revert to gateway now</button>`
  :rl.role==="gateway"
  ?`uplink <select id="rl-up"><option>halow</option><option>wifi</option></select>
    eth0 <select id="rl-eth"><option>mgmt</option><option>downlink</option></select>
    <button class="act" onclick="roleBridge()">switch to bridge</button>
    <button class="act" onclick="roleCheck(this)">validate configs</button>`
  :`<button class="act" onclick="roleOp('gateway',1)">back to gateway</button>`}</p>
 <pre id="rl-out"></pre></div>`;
 const ifs=r.interfaces.map(i=>{const a=(i.addr_info||[]).map(x=>x.local+"/"+x.prefixlen).join(" ");
  return `<tr><td>${esc(i.ifname)}</td><td class="${i.operstate==='UP'?'ok':'warn'}">${esc(i.operstate)}</td><td>${esc(a)}</td></tr>`}).join("");
 const ls=r.leases.length?r.leases.map(l=>
  `<tr><td>${esc(l.host)}</td><td>${esc(l.ip)}</td><td>${esc(l.mac)}</td></tr>`).join("")
  :`<tr><td colspan=3 class="warn">no leases</td></tr>`;
 return `${roleCard}<div class="card"><h2>interfaces — forwarding ${r.forwarding==="1"?"<span class='ok'>on</span>":"<span class='bad'>OFF</span>"} · dnsmasq ${esc(r.dnsmasq)}
 · 2.4G AP mesh-2g ${r.wifi_ap?"<span class='ok'>on</span>":"<span class='warn'>off</span>"}
 <button class="act" style="float:right" onclick="wifiAp('${r.wifi_ap?"off":"on"}')">${r.wifi_ap?"turn off":"turn on"}</button></h2>
 <table><tr><th>if</th><th>state</th><th>addrs</th></tr>${ifs}</table></div>
 <div class="card"><h2>DHCP leases (halow net)</h2>
 <table><tr><th>host</th><th>ip</th><th>mac</th></tr>${ls}</table></div>
 <div class="card"><h2>firewall / NAT</h2><pre>${esc(r.nft)}</pre></div>
 <div class="card"><h2>routes</h2><pre>${esc(r.routes.map(x=>`${x.dst||"default"} via ${x.gateway||"-"} dev ${x.dev}`).join("\n"))}</pre></div>`}
const TCOLOR={halow:"#4ea1ff",wifi:"#39d353",ethernet:"#c9a227",lora:"#e5534b",espnow:"#a371f7"};
let _mesh=null;
async function meshmap(){_mesh=await j("/api/topology");
 const leg=Object.entries(TCOLOR).map(([k,c])=>
  `<span style="color:${c}">■</span> ${k}`).join(" &nbsp; ");
 return `<div class="card"><h2>mesh map — ${_mesh.nodes?_mesh.nodes.length:0} nodes,
  ${_mesh.edges?_mesh.edges.length:0} links${_mesh.age_s!=null?` · ${_mesh.age_s}s old`:""}
  ${_mesh.error?`<span class="warn">${esc(_mesh.error)}</span>`:""}</h2>
 <p style="color:var(--dim)">${leg} — solid = the router's own view (HaLow assoc / WiFi lease /
  wired); dashed = a node's reported peer (LoRa/ESP-NOW mesh). Each link is labeled with its
  transport; <b>click a node</b> for its full config. API: <code>/api/topology</code>.</p>
 <div style="display:flex;gap:12px;flex-wrap:wrap">
 <svg id="mesh-svg" viewBox="0 0 900 520" style="flex:1;min-width:420px;height:520px;background:#0d1117;border-radius:8px"></svg>
 <div id="mesh-detail" style="flex:0 0 320px;min-width:260px;background:#0d1117;border-radius:8px;padding:12px;overflow:auto;max-height:520px">
  <p style="color:var(--dim)">click a node for its configuration, addresses, radios and links</p></div>
 </div></div>`}
function nodeDetail(v){
 const rows=[];
 const add=(k,x)=>{if(x!=null&&x!=="")rows.push(`<tr><td style="color:var(--dim);padding-right:10px">${esc(k)}</td><td>${x}</td></tr>`)};
 add("kind",esc(v.kind)+(v.role?` (${esc(v.role)})`:""));
 add("state",v.state?`<span class="${v.state==='healthy'?'ok':v.state==='down'?'bad':'warn'}">${esc(v.state)}</span>`:null);
 add("long name",esc(v.long_name));
 add("mesh id",esc(v.mesh_id));
 if(v.battery!=null)add("battery",v.battery+"%");
 if(v.reboot_count!=null)add("reboots",v.reboot_count);
 if(v.signal_dbm!=null)add("HaLow signal",v.signal_dbm+" dBm");
 if(v.espnow_ready!=null)add("ESP-NOW",v.espnow_ready?"ready":"not ready");
 add("transports",(v.transports||[]).map(t=>`<span style="color:${TCOLOR[t]||'#8b949e'}">${esc(t)}</span>`).join(", "));
 if(v.addrs){for(const[k,a]of Object.entries(v.addrs))if(a)add(k+(v.kind==='gateway'?" (iface)":" addr"),esc(a))}
 const links=(_mesh.edges||[]).filter(e=>e.from===v.id||e.to===v.id).map(e=>{
  const other=e.from===v.id?e.to:e.from;
  return `<tr><td style="color:${TCOLOR[e.transport]||'#8b949e'};padding-right:10px">${esc(e.transport)}</td><td>→ ${esc(other)}<br><span style="color:var(--dim)">${esc(e.detail||'')} · ${esc(e.evidence||'')}</span></td></tr>`}).join("");
 document.getElementById("mesh-detail").innerHTML=
  `<h3 style="margin:0 0 8px">${esc(v.label||v.id)}</h3>
   <table style="font-size:13px">${rows.join("")}</table>
   ${links?`<h4 style="margin:12px 0 4px;color:var(--dim)">links</h4><table style="font-size:13px">${links}</table>`:""}`}
function drawMesh(){const svg=$("#mesh-svg");if(!svg||!_mesh||!_mesh.nodes)return;
 const W=900,H=520,V=_mesh.nodes,E=_mesh.edges||[];
 const idx={};V.forEach((v,i)=>idx[v.id]=i);
 const P=V.map((v,i)=>v.id==="router"?{x:W/2,y:H/2}:
  {x:W/2+260*Math.cos(2*Math.PI*i/V.length),y:H/2+200*Math.sin(2*Math.PI*i/V.length)});
 for(let it=0;it<220;it++){
  const F=P.map(()=>({x:0,y:0}));
  for(let a=0;a<V.length;a++)for(let b=a+1;b<V.length;b++){
   let dx=P[a].x-P[b].x,dy=P[a].y-P[b].y,d=Math.hypot(dx,dy)||1,f=9000/(d*d);
   F[a].x+=dx/d*f;F[a].y+=dy/d*f;F[b].x-=dx/d*f;F[b].y-=dy/d*f}
  E.forEach(e=>{const a=idx[e.from],b=idx[e.to];if(a==null||b==null)return;
   let dx=P[b].x-P[a].x,dy=P[b].y-P[a].y,d=Math.hypot(dx,dy)||1,f=(d-150)*0.02;
   F[a].x+=dx/d*f;F[a].y+=dy/d*f;F[b].x-=dx/d*f;F[b].y-=dy/d*f});
  for(let i=0;i<V.length;i++){if(V[i].id==="router"){P[i].x=W/2;P[i].y=H/2;continue}
   P[i].x=Math.max(40,Math.min(W-40,P[i].x+F[i].x*0.05));
   P[i].y=Math.max(40,Math.min(H-40,P[i].y+F[i].y*0.05))}}
 const scls=s=>s==="healthy"?"#39d353":s==="down"?"#e5534b":s?"#d29922":"#8b949e";
 let s="";
 E.forEach((e,i)=>{const a=P[idx[e.from]],b=P[idx[e.to]];if(!a||!b)return;
  const rev=(e.evidence||"").includes("/api/mesh");
  const mx=(a.x+b.x)/2,my=(a.y+b.y)/2,col=TCOLOR[e.transport]||'#8b949e';
  s+=`<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${col}"
   stroke-width="2.5"${rev?' stroke-dasharray="6 4"':''} opacity="0.8"
   onmouseover="this.setAttribute('stroke-width','4.5')" onmouseout="this.setAttribute('stroke-width','2.5')"><title>${esc(e.from)} — ${esc(e.to)} via ${esc(e.transport)}: ${esc(e.detail||'')} [${esc(e.evidence||'')}]</title></line>`;
  // transport label on the edge, on a small dark plate so it stays legible
  s+=`<rect x="${mx-esc(e.transport).length*3.5-3}" y="${my-8}" width="${esc(e.transport).length*7+6}" height="15" rx="3" fill="#0d1117" opacity="0.85"/>
   <text x="${mx}" y="${my+3}" fill="${col}" font-size="11" text-anchor="middle">${esc(e.transport)}</text>`});
 V.forEach((v,i)=>{const p=P[i],r=v.kind==="gateway"?26:v.kind==="heard"?12:18;
  const fill=v.kind==="gateway"?"#1f6feb":v.kind==="heard"?"#30363d":"#21262d";
  const batt=v.battery!=null?` ${v.battery}%`:"";
  s+=`<circle cx="${p.x}" cy="${p.y}" r="${r}" fill="${fill}" stroke="${scls(v.state)}" stroke-width="2.5"
   style="cursor:pointer" onclick="nodeDetail(_mesh.nodes[${i}])"><title>click for detail</title></circle>
   <text x="${p.x}" y="${p.y+r+14}" fill="#c9d1d9" font-size="12" text-anchor="middle" style="cursor:pointer" onclick="nodeDetail(_mesh.nodes[${i}])">${esc(v.label||v.id)}${esc(batt)}</text>`});
 svg.innerHTML=s}
async function camera(){const d=await j("/api/ingest/images?n=24");
 const R=d.ring||{};
 const nrows=Object.entries(d.nodes||{}).map(([n,s])=>
  `<tr><td>${esc(n)}</td><td>${s.frames}</td><td>${s.last_seq??"—"}</td>
   <td class="${s.seq_gaps>0?'warn':'ok'}">${s.seq_gaps}</td></tr>`).join("")
  ||`<tr><td colspan=4 class="warn">no frames yet</td></tr>`;
 const grid=(d.images||[]).map(im=>
  `<div style="display:inline-block;margin:4px;text-align:center;vertical-align:top">
   <img src="/api/ingest/images/${esc(im.id)}" loading="lazy" style="max-width:180px;max-height:180px;border-radius:4px;background:#0d1117">
   <div style="color:var(--dim);font-size:11px">${esc(im.node)} #${im.seq??"—"}<br>${new Date(im.received_at*1000).toISOString().slice(5,19).replace("T"," ")} · ${im.bytes}B</div></div>`).join("")
  ||`<p class="warn">no frames — POST a JPEG to /api/ingest/image (contract: docs/feature-roadmap.md item 29)</p>`;
 return `<div class="card"><h2>trail-cam ingest — ${R.count??0}/${R.cap_count??"?"} frames ·
  ${Math.round((R.bytes||0)/1048576)} of ${R.cap_mb??"?"} MB · ${R.free_mb??"?"} MB free</h2>
 <table><tr><th>node</th><th>frames</th><th>last seq</th><th>seq gaps (rx-side)</th></tr>${nrows}</table>
 <p><button class="act" onclick="camPurge()">purge ring</button>
 <span style="color:var(--dim)">terminal sink — sha256-verified on receipt, oldest evicted at the caps.
 seq_gaps is measured receiver-side loss (the sender cannot lie about it).</span></p></div>
 <div class="card"><h2>latest ${(d.images||[]).length} frames</h2>${grid}</div>`}
async function camPurge(){if(!confirm("Empty the entire frame ring?"))return;
 const fd=new FormData();fd.append("confirm","1");
 await fetch("/api/ingest/purge",{method:"POST",body:fd});render()}
async function roleOp(op,confirm){const fd=new FormData();fd.append("op",op);
 if(confirm)fd.append("confirm","1");
 const r=await(await fetch("/api/config/role",{method:"POST",body:fd})).json();
 const el=document.getElementById("rl-out");if(el)el.textContent=r.error||r.output;
 setTimeout(render,1500)}
async function roleBridge(){
 if(!confirm("Switch the Pi to a downstream BRIDGE? It joins another network as a client and serves local devices. This reconfigures NAT/DHCP and arms a 5-min dead-man rollback — run 'commit' to keep it."))return;
 const fd=new FormData();fd.append("op","bridge");fd.append("confirm","1");
 fd.append("uplink",$("#rl-up").value);fd.append("eth",$("#rl-eth").value);
 const r=await(await fetch("/api/config/role",{method:"POST",body:fd})).json();
 document.getElementById("rl-out").textContent=r.error||r.output;setTimeout(render,2000)}
async function roleCheck(btn){btn.disabled=true;
 try{const d=await j("/api/role?check=1");
 document.getElementById("rl-out").textContent=d.check||"(no output)"}
 finally{btn.disabled=false}}
async function nodes(){const d=await j("/api/nodes");
 if(d.error&&!d.nodes.length)return `<p class="warn">${esc(d.error)}</p>`;
 const scls=s=>s==="healthy"?"ok":(s==="stale"||s==="unknown"||s==="wifi-down")?"warn":"bad";
 const banner=d.watcher_stale?`<div class="card"><p class="bad">WATCHER STALE —
  cache is ${d.age_s??"?"}s old (3× the poll interval). A dead watcher must not look
  like a healthy fleet: check /api/logs?unit=halow-watch.</p></div>`:"";
 const sigc=v=>v==null?"":v>=-65?"ok":v>=-80?"warn":"bad";
 const rows=(d.nodes||[]).map(n=>{
  const L=n.learned||{},A=n.assoc||{},R=n.report||{};
  const sig=A.signal_dbm!=null
   ?`<span class="${sigc(A.signal_dbm)}">${A.signal_dbm} dBm</span> <span style="color:var(--dim)">halow</span>`
   :R.rssi!=null
   ?`<span class="${sigc(R.rssi)}">${R.rssi} dBm</span> <span style="color:var(--dim)">wifi</span>`
   :"—";
  return `<tr><td>${esc(n.name)}</td>
   <td class="${scls(n.state)}">${esc(n.state)}</td>
   <td>${esc(n.mac||"—")}</td>
   <td>${A.associated?'<span class="ok">yes</span>':(A.reservation_ip?'reserved':'—')}</td>
   <td>${sig}</td>
   <td>${L.battery_pct??"—"}</td><td>${L.reboot_count??"—"}</td>
   <td>${n.rtt_ms!=null?n.rtt_ms+"ms":"—"}</td>
   <td>${n.checked_at?Math.max(0,Math.round(Date.now()/1000-n.checked_at))+"s":"—"}</td>
   <td>${esc(n.detail||"")}${(n.alerts||[]).map(a=>` · <span class="warn">${esc(a)}</span>`).join("")}</td>
   <td><button class="act" onclick="nodeLive('${esc(n.name)}')">live</button></td></tr>`}).join("")
  ||`<tr><td colspan=10 class="warn">no nodes configured</td></tr>`;
 const adopt=(d.unknown_stations||[]).map(u=>
  `<div class="card"><h2>unknown station ${esc(u.mac)}</h2>
   <p>${u.associated?"associated on halow0":"seen in leases"} · lease ${esc(u.lease_ip||"none")}
   (${esc(u.lease_host||"?")}). Adopt as
   name <input id="ad-n-${esc(u.mac.replaceAll(":",""))}" value="${esc(u.proposed.name)}" style="width:80px">
   ip <input id="ad-i-${esc(u.mac.replaceAll(":",""))}" value="${esc(u.proposed.reservation_ip||"")}" style="width:110px">
   <button class="act" onclick="adopt('${esc(u.mac)}')">adopt</button></p></div>`).join("");
 return `${banner}<div class="card"><h2>fleet — cached by halow-watch
  (age ${d.age_s??"?"}s · poll every ${d.interval_s??"?"}s · viewers never tax the nodes)</h2>
 <table><tr><th>node</th><th>state</th><th>halow mac</th><th>assoc</th><th>signal</th><th>batt</th>
  <th>reboots</th><th>rtt</th><th>checked</th><th>detail</th><th></th></tr>${rows}</table>
 <p style="color:var(--dim)">states: healthy · stale (rebooted / silent past 3× its own interval)
  · wifi-down (a peer still hears it on LoRa) · down · unknown (alive but odd — auth/rate-limit).
  Live buttons poke the node directly and cost it a TLS session; the table never does.</p></div>
 ${adopt}<div class="card"><h2>reachability matrix</h2>
 <p><button class="act" onclick="reachRun(this)">probe all paths</button>
 <span style="color:var(--dim)">— ICMP + HTTP HEAD per address family (LAN / HaLow lease /
 LoRa / VIP); never bare connects; 20s budget; button-only by design.</span></p>
 <div id="reach-out"></div></div><pre id="nv-out"></pre>`}
async function reachRun(btn){btn.disabled=true;btn.textContent="probing…";
 try{const d=await j("/api/reach");
 const cell=p=>{if(p.status)return `<span class="warn">${esc(p.status)}</span>`;
  const i=p.icmp&&p.icmp.ok?`icmp ${p.icmp.ms??"?"}ms`:`<span class="bad">icmp ✗</span>`;
  const h=p.http&&p.http.ok?`http ${p.http.status}`:`<span class="bad">http ✗</span>`;
  return `${esc(p.addr||"")}<br>${i} · ${h}`};
 document.getElementById("reach-out").innerHTML=
  `<table><tr><th>node</th><th>lan</th><th>halow</th><th>lora</th><th>fwvip</th></tr>`+
  d.nodes.map(n=>{const by={};n.paths.forEach(p=>by[p.family]=p);
   return `<tr><td>${esc(n.name)}</td>${["lan","halow","lora","fwvip"].map(f=>
    `<td>${by[f]?cell(by[f]):"—"}</td>`).join("")}</tr>`}).join("")+
  `</table><p style="color:var(--dim)">${d.cached?`cached ${d.age_s}s ago`:`fresh, ${d.elapsed_s}s`}</p>`}
 finally{btn.disabled=false;btn.textContent="probe all paths"}}
async function nodeLive(n){const el=document.getElementById("nv-out");
 el.textContent="poking "+n+" (costs the node a TLS session)…";
 try{const d=await j(`/api/nodes/${n}/diag`);
 el.textContent=n+" /api/diag:\n"+JSON.stringify(d,null,1).slice(0,3000)}
 catch(e){el.textContent="error: "+e}}
async function adopt(mac){const k=mac.replaceAll(":","");
 const fd=new FormData();fd.append("mac",mac);
 fd.append("name",$("#ad-n-"+k).value);fd.append("ip",$("#ad-i-"+k).value);
 const r=await(await fetch("/api/nodes/adopt",{method:"POST",body:fd})).json();
 const el=document.getElementById("nv-out");el.textContent=r.error||r.output;
 if(!r.error)setTimeout(render,1500)}
setInterval(()=>{$("#clock").textContent=new Date().toLocaleTimeString()},1000);
render();setInterval(render,15000); // Nodes joined the cycle: it is a file read now
</script></body></html>"""


if __name__ == "__main__":
    cert = os.path.join(CONF_DIR, "ui-cert.pem")
    key = os.path.join(CONF_DIR, "ui-key.pem")
    ctx = None
    if os.path.exists(cert) and os.path.exists(key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    # threaded: a slow node-proxy call must not stall the whole console
    import threading
    threading.Thread(target=_watchdog_thread,
                     args=("https" if ctx else "http",), daemon=True).start()
    app.run(host="0.0.0.0", port=8443, ssl_context=ctx, threaded=True)
