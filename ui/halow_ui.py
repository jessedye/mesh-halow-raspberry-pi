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
    args = ["set"]
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
             "halow-join-watch", "halow-iperf3", "dnsmasq", "kernel")
JOIN_STATE_DIR = "/var/lib/halow/join"
DISK_LOW_MB = 512  # SD low-water; mirrored in scripts/halow-mon


@app.post("/api/halow/throughput")
@authed
def api_halow_throughput():
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
    return jsonify({
        "throttled_raw": raw or None,
        "flags": flags or ["clean — no undervoltage or throttling recorded"],
        "temp_c": round(int(open("/sys/class/thermal/thermal_zone0/temp")
                            .read()) / 1000, 1),
        "volts_core": sh("vcgencmd measure_volts core").strip() or None,
    })


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
    if mac is not None:
        return jsonify(out.get(mac.lower(), out.get(mac.upper(),
                       {"error": f"no samples for {mac}", "stations": list(out)})))
    return jsonify({"stations": out})


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
                  "disk_free_mb"):
            vals = [s[k] for s in samples if k in s]
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
        summary["uptime_pct"] = {
            k: round(100 * sum(1 for s in samples if s.get(k)) /
                     len(samples), 1)
            for k in ("ap", "dnsmasq", "upstream")}
    return jsonify({"minutes": minutes, "n": len(samples),
                    "summary": summary, "monitor": mon,
                    "samples": samples[-360:]})


@app.get("/api/diag")
@authed
def api_diag_bundle():
    """One-shot bundle — the gateway's answer to the nodes' /api/diag."""
    services = {}
    for u in ("halow-ap", "halow-net", "halow-ui", "dnsmasq",
              "halow-iperf3", "halow-sta-events", "halow-join-watch"):
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

def node_get(node, path):
    req = urllib.request.Request(node["url"].rstrip("/") + path)
    req.add_header("Authorization", "Bearer " + node.get("token", ""))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # nodes use self-signed certs
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return json.load(r)


@app.get("/api/nodes")
@authed
def api_nodes():
    try:
        with open(NODES_CONF) as f:
            nodes = json.load(f)["nodes"]
    except Exception:
        return jsonify({"nodes": [], "error": "no nodes.json configured"})
    out = []
    for n in nodes:
        entry = {"name": n["name"], "url": n["url"]}
        try:
            entry["diag"] = node_get(n, "/api/diag")
            entry["reachable"] = True
        except Exception as e:
            entry["reachable"] = False
            entry["error"] = str(e)
        out.append(entry)
    return jsonify({"nodes": out})


@app.get("/api/nodes/<name>/<path:sub>")
@authed
def api_node_proxy(name, sub):
    if sub not in ("diag", "mesh", "settings", "metrics", "routes", "ip",
                   "espnow", "env", "queue", "power"):
        return jsonify({"error": "endpoint not allowed"}), 400
    try:
        with open(NODES_CONF) as f:
            nodes = {n["name"]: n for n in json.load(f)["nodes"]}
        return jsonify(node_get(nodes[name], "/api/" + sub))
    except KeyError:
        return jsonify({"error": f"unknown node {name}"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ---------- System ----------

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
const TABS=["Overview","HaLow","Router","Config","Nodes","Diag","Debug"];let tab="Overview";
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
 else m.innerHTML=await nodes();}catch(e){m.innerHTML=`<p class="bad">${esc(e)}</p>`}}
async function diag(){const nb=await j("/api/diag/neigh"),sv=await j("/api/diag/survey"),pw=await j("/api/diag/power");
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
 now auto-detected as <code>dhcp_no_reach</code> in the Debug tab's join forensics.</p></div>
 <div class="card"><h2>power / throttling</h2>
 <p>${pw.temp_c}°C · ${esc(pw.volts_core||"")} · ${pw.flags.map(f=>
  `<span class="${f.includes("NOW")?"bad":(f.includes("occurred")?"warn":"ok")}">${esc(f)}</span>`).join(" · ")}</p></div>
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
async function ovw(){const s=await j("/api/system"),h=await j("/api/halow");
 const t=(parseInt(s.temp)/1000).toFixed(1);
 return `<div class="card"><h2>gateway</h2><div class="grid">
 <div class="stat"><div class="v">${esc(s.uptime.replace("up ",""))}</div><div class="k">uptime</div></div>
 <div class="stat"><div class="v">${t}°C</div><div class="k">SoC temp</div></div>
 <div class="stat"><div class="v">${esc(s.load.join(" "))}</div><div class="k">load</div></div>
 <div class="stat"><div class="v">${esc(s.mem.MemAvailable||"?")}</div><div class="k">mem avail</div></div>
 <div class="stat"><div class="v ${h.present?'ok':'bad'}">${h.present?"present":"ABSENT"}</div><div class="k">halow0</div></div>
 <div class="stat"><div class="v ${h.ap_active==='active'?'ok':'warn'}">${esc(h.ap_active)}</div><div class="k">AP service</div></div>
 </div></div>
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
 const profs=Object.entries(h.profiles).map(([n,p])=>{
  const v=cc&&cc.profiles&&cc.profiles[n];
  const badge=v&&v.compatible===false?'<span class="warn">strands pinned nodes</span>':"";
  return `<tr><td>${n===h.profile?"▶":""}</td><td>${esc(n)}</td><td>${p.width_mhz} MHz</td>
   <td>ch ${p.channel}</td><td>op ${p.op_class}</td><td>${badge}</td>
   <td><button class="act" ${n===h.profile?"disabled":""}
     onclick="setProf('${n}')">apply</button></td></tr>`}).join("");
 const stas=h.stations.length?h.stations.map(s=>
  `<tr><td>${esc(s.mac)}</td><td>${esc(s.signal||"")}</td><td>${esc(s.tx_bitrate||"")}</td>
   <td>${esc(s.rx_bitrate||"")}</td><td>${esc(s.connected_time||"")}</td></tr>`).join("")
  :`<tr><td colspan=5 class="warn">no stations associated</td></tr>`;
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
 <table><tr><th>mac</th><th>signal</th><th>tx</th><th>rx</th><th>connected</th></tr>${stas}</table></div>`}
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
 const ifs=r.interfaces.map(i=>{const a=(i.addr_info||[]).map(x=>x.local+"/"+x.prefixlen).join(" ");
  return `<tr><td>${esc(i.ifname)}</td><td class="${i.operstate==='UP'?'ok':'warn'}">${esc(i.operstate)}</td><td>${esc(a)}</td></tr>`}).join("");
 const ls=r.leases.length?r.leases.map(l=>
  `<tr><td>${esc(l.host)}</td><td>${esc(l.ip)}</td><td>${esc(l.mac)}</td></tr>`).join("")
  :`<tr><td colspan=3 class="warn">no leases</td></tr>`;
 return `<div class="card"><h2>interfaces — forwarding ${r.forwarding==="1"?"<span class='ok'>on</span>":"<span class='bad'>OFF</span>"} · dnsmasq ${esc(r.dnsmasq)}
 · 2.4G AP mesh-2g ${r.wifi_ap?"<span class='ok'>on</span>":"<span class='warn'>off</span>"}
 <button class="act" style="float:right" onclick="wifiAp('${r.wifi_ap?"off":"on"}')">${r.wifi_ap?"turn off":"turn on"}</button></h2>
 <table><tr><th>if</th><th>state</th><th>addrs</th></tr>${ifs}</table></div>
 <div class="card"><h2>DHCP leases (halow net)</h2>
 <table><tr><th>host</th><th>ip</th><th>mac</th></tr>${ls}</table></div>
 <div class="card"><h2>firewall / NAT</h2><pre>${esc(r.nft)}</pre></div>
 <div class="card"><h2>routes</h2><pre>${esc(r.routes.map(x=>`${x.dst||"default"} via ${x.gateway||"-"} dev ${x.dev}`).join("\n"))}</pre></div>`}
async function nodes(){const d=await j("/api/nodes");
 if(d.error)return `<p class="warn">${esc(d.error)}</p>`;
 return d.nodes.map(n=>{if(!n.reachable)return `<div class="card"><h2>${esc(n.name)}</h2>
  <p class="bad">unreachable: ${esc(n.error)}</p></div>`;
  const dg=n.diag;return `<div class="card"><h2>${esc(n.name)} — <span class="ok">reachable</span></h2>
  <pre>${esc(JSON.stringify(dg,null,1).slice(0,2000))}</pre>
  <p>${["mesh","metrics","routes","power"].map(p=>
   `<button class="act" onclick="nodeView('${esc(n.name)}','${p}')">${p}</button>`).join(" ")}</p>
  <pre id="nv-${esc(n.name)}"></pre></div>`}).join("")}
async function nodeView(n,p){const el=document.getElementById("nv-"+n);
 el.textContent="loading…";try{const d=await j(`/api/nodes/${n}/${p}`);
 el.textContent=JSON.stringify(d,null,1).slice(0,4000)}catch(e){el.textContent="error: "+e}}
setInterval(()=>{$("#clock").textContent=new Date().toLocaleTimeString()},1000);
render();setInterval(()=>{if(tab!=="Nodes")render()},15000);
</script></body></html>"""


if __name__ == "__main__":
    cert = os.path.join(CONF_DIR, "ui-cert.pem")
    key = os.path.join(CONF_DIR, "ui-key.pem")
    ctx = None
    if os.path.exists(cert) and os.path.exists(key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    # threaded: a slow node-proxy call must not stall the whole console
    app.run(host="0.0.0.0", port=8443, ssl_context=ctx, threaded=True)
