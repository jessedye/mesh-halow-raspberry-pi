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


def check_auth(header):
    """Basic auth and Bearer tokens stay valid for curl/scripts alongside
    browser sessions. The Bearer token is the mesh-v4 ADMIN_TOKEN; only its
    sha256 is stored (ui.conf API_TOKEN_HASH), mirroring the nodes' model."""
    if header.startswith("Bearer "):
        want = load_kv(UI_CONF).get("API_TOKEN_HASH", "")
        got = hashlib.sha256(header[7:].strip().encode()).hexdigest()
        return bool(want) and hmac.compare_digest(got, want)
    if not header.startswith("Basic "):
        return False
    try:
        user_pass = base64.b64decode(header[6:]).decode()
    except Exception:
        return False
    return verify_creds(user_pass)


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
        user = request.form.get("username", "")
        pw = request.form.get("password", "")
        if verify_creds(f"{user}:{pw}"):
            session.permanent = True
            session["authed"] = True
            session["user"] = user
            return redirect("/")
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


@app.post("/api/halow/profile")
@authed
def api_halow_profile():
    name = request.form.get("name", "")
    ok = sh(f"sudo /usr/local/bin/halowctl set-profile {name} 2>&1", timeout=30)
    return jsonify({"applied": name, "output": ok})


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
    args = []
    for k in ("channel", "width", "ssid"):
        v = request.form.get(k)
        if v:
            args.append(f"{k}={v}")
    out = sh("sudo /usr/local/bin/halowctl set " + " ".join(args), timeout=30)
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


@app.post("/api/system/reboot")
@authed
def api_system_reboot():
    if request.form.get("confirm") != "1":
        return jsonify({"error": "needs confirm=1: reboots the gateway"}), 400
    subprocess.Popen(["sudo", "/usr/sbin/reboot"])
    return jsonify({"ok": True, "output": "rebooting"})


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
    return jsonify({
        "uptime": sh("uptime -p").strip(),
        "load": open("/proc/loadavg").read().split()[:3],
        "mem": mem,
        "temp": sh("cat /sys/class/thermal/thermal_zone0/temp").strip(),
        "kernel": sh("uname -r").strip(),
        "disk": sh("df -h / | tail -1").split(),
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
const TABS=["Overview","HaLow","Router","Config","Nodes"];let tab="Overview";
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
 else m.innerHTML=await nodes();}catch(e){m.innerHTML=`<p class="bad">${esc(e)}</p>`}}
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
 <div class="card"><h2>kernel / disk</h2><pre>${esc(s.kernel)}  root ${esc((s.disk[3]||"?"))} free</pre></div>`}
async function halow(){const h=await j("/api/halow");
 const profs=Object.entries(h.profiles).map(([n,p])=>
  `<tr><td>${n===h.profile?"▶":""}</td><td>${esc(n)}</td><td>${p.width_mhz} MHz</td>
   <td>ch ${p.channel}</td><td>op ${p.op_class}</td>
   <td><button class="act" ${n===h.profile?"disabled":""}
     onclick="setProf('${n}')">apply</button></td></tr>`).join("");
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
 <table><tr><th></th><th>profile</th><th>width</th><th>channel</th><th>op class</th><th></th></tr>${profs}</table>
 <p style="color:var(--dim)">override: ch <input id="ch" placeholder="${esc(h.channel_override||'auto')}">
 width <input id="w" placeholder="${esc(h.width_override||'auto')}">
 <button class="act" onclick="setOvr()">apply</button>
 — valid US: 1MHz odd 1-51 · 2MHz 2,6..50 · 4MHz 8,16..48 · 8MHz 12,28,44</p></div>
 <div class="card"><h2>stations</h2>
 <table><tr><th>mac</th><th>signal</th><th>tx</th><th>rx</th><th>connected</th></tr>${stas}</table></div>`}
async function setProf(n){const fd=new FormData();fd.append("name",n);
 await fetch("/api/halow/profile",{method:"POST",body:fd});render()}
async function probe(btn){btn.disabled=true;btn.textContent="probing…";
 try{const r=await(await fetch("/api/halow/probe",{method:"POST"})).json();
 const el=document.getElementById("probe-out");
 el.textContent=r.output;el.className=r.ok?"ok":"bad"}finally{btn.disabled=false;btn.textContent="Probe chip"}}
async function setMode(m){const fd=new FormData();fd.append("mode",m);
 await fetch("/api/halow/mode",{method:"POST",body:fd});render()}
async function wifiAp(s){const fd=new FormData();fd.append("state",s);
 await fetch("/api/router/wifi-ap",{method:"POST",body:fd});setTimeout(render,1500)}
async function setOvr(){const fd=new FormData();
 if($("#ch").value)fd.append("channel",$("#ch").value);
 if($("#w").value)fd.append("width",$("#w").value);
 await fetch("/api/halow/set",{method:"POST",body:fd});render()}
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
