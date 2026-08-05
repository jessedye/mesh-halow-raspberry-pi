# 26. Node-proxy transport failover + multi-path reachability matrix (GET /api/reach)

> Tier 3 - fleet-ops | Effort: medium | Impact: medium | Depends on: #25 (the nodes.json `mac` field — define it once, there)

## Problem

The web console's node proxy resolves targets from exactly one place: the
static LAN URL in `/etc/halow/nodes.json` (`node_get`, ui/halow_ui.py:869-876;
config/nodes.json.example carries `name`/`url`/`token` and nothing else). If
that URL stops answering, the Nodes tab reports "unreachable" and stops —
even when the same node is associated to the gateway's own HaLow AP and holds
a 10.117.0.x DHCP lease the gateway handed out itself. The gateway knows a
second working address for the node and never tries it.

This fails at exactly the wrong moment. The mesh-v4 transport-ladder design
makes the Pi a rung *gateway*: node-to-node traffic inside 10.117.0.0/24 is
one hop through the AP, and HaLow is "the only rung that still reaches the
LAN when the 2.4 GHz AP is gone" (mesh-v4 docs/transport-ladder-halow.md:293-297).
When the household 2.4 GHz WiFi dies — the scenario HaLow exists for — the
LAN URLs in nodes.json all go dark, and the console goes blind precisely
while the HaLow link underneath it is fine.

This is not hypothetical. The day the proxy was verified, it worked against
node1 while node2 was down on the LAN (README.md:66-68) — a second path
would have answered for node2. And the failure is expensive per-request:
`node_get` uses a single 15 s timeout (ui:875), so a dead LAN target costs
15 s per proxied call and `/api/nodes` iterates every node serially
(ui:887-896).

Separately, there is no one-call answer to the operator's first question after
an association: *the node joined — which of its addresses actually route?*
Each node has up to four addresses that mean the same machine: its LAN IP,
its HaLow lease, its LoRa-netif address, and its firmware VIP. Today checking
them means four manual Diag-tab probes. `GET /api/reach` makes it one call.
Verified absent this session: the full endpoint list of ui/halow_ui.py has no
failover logic and no `/api/reach` (grep over every `@app.get`/`@app.post`).

## Current state

Gateway repo (`mesh-halow-raspberry-pi`), all verified 2026-08-05:

- ui/halow_ui.py:869-876 — `node_get(node, path)`: one URL, one attempt,
  `timeout=15`, bearer token header, `CERT_NONE` (nodes use self-signed
  certs). No retry, no alternate address.
- ui/halow_ui.py:879-897 — `/api/nodes` live-fetches `/api/diag` from every
  node per request; on exception sets `reachable: false` and moves on.
- ui/halow_ui.py:900-913 — `/api/node/<name>/<sub>` proxy with an endpoint
  whitelist that already includes `"ip"` — the node endpoint that reports
  its own LoRa/VIP addresses (see below).
- ui/halow_ui.py:271-280 — dnsmasq lease parsing already exists
  (`/var/lib/misc/dnsmasq.leases` → expiry/mac/ip/host), read unprivileged.
  A second, cruder parse lives in the diag bundle at ui:841-846. Neither is
  shared.
- ui/halow_ui.py:624-648 — the service-check pattern to copy: HTTP HEAD with
  short timeout, where *any* HTTP status counts as "server answered — the
  path is up" (ui:642-645). The v1 diag table records why HEAD and never a
  bare TCP connect: "bare :443 connects crashed nodes"
  (docs/feature-roadmap.md:103; the Diag card at ui:1038 repeats it).
- ui/halow_ui.py:601-621 — the bounded-ping pattern to copy: `ping -c N -W 2`,
  capped n, runs unprivileged as the `halow-ui` user (systemd/halow-ui.service:10),
  parsed for loss/rtt with the "loss at small n is a sample, not a rate" note.
- ui/halow_ui.py:96-109 — `_FAILS`: precedent for small in-memory per-key
  state dicts in this app (auth throttle).
- ui/halow_ui.py:1248-1261 — the Nodes tab JS; the 15 s auto-refresh loop
  deliberately skips the Nodes tab (`if(tab!=="Nodes")render()`, ui:1261) so
  node fetches only happen when the operator selects the tab.
- scripts/deploy.sh:51,70 — `halow-ui` is a system user; `/var/lib/halow` is
  `chown halow-ui:halow-ui`, so the UI process can persist state there.
- config/sudoers-halow-ui — the UI's entire root surface. Nothing in this
  issue needs an addition to it.

Client repo (`mesh-v4`), verified this session:

- tools/vip.py:25-30,47-48 — the damping model to copy: demote after 2
  consecutive failures, promote after 3 consecutive successes; "a single lost
  probe on a busy WiFi network is not a failure, and flapping the route would
  be worse than either transport on its own."
- tools/vip.py:50-63 and 70-77 — the derived-address lesson: vip.py once
  derived the LoRa address from the node number, the constant disagreed with
  the firmware by one in the second octet, and "failover could only ever
  fail" — silently. The fix: `device_addrs()` reads
  `GET /api/ip` → `data.netif.address` (LoRa netif) and
  `data.netif.vip_address` (firmware VIP) from the node itself (vip.py:86-91);
  "Reading beats deriving whenever the source is reachable."
- tools/vip.py:99-109 — a VIP with no host route "is simply unreachable,
  which reads as the VIP being broken." Measured there: 10.42.0.1 at 100%
  loss while 10.44.162.104 on the same node answered HTTP 200.
- docs/transport-ladder-halow.md:275-279 — the addressing matrix:
  LoRa netif/VIP tunnel 10.116.0.0/16; HaLow 10.117.0.0/24 (Pi = 10.117.0.1,
  DHCP .10-.200); LAN 192.168.50.0/23.

**Spec correction.** The roadmap/spec text for this item says "firmware VIP
(10.116/16)". The code says otherwise: vip.py:62-63 has
`LORA_PREFIX_DEFAULT = 116` and `FW_VIP_PREFIX_DEFAULT = 44` — 10.116/16 is
the LoRa *netif* prefix and the firmware VIP defaults to 10.44/16. Follow the
code. In practice the point is moot at runtime because both addresses must be
read from the device's `/api/ip`, never derived from a prefix constant —
that is the whole vip.py lesson.

## Design

Two pieces, both living in ui/halow_ui.py, both fully unprivileged. No new
systemd unit, no sudoers change, no halowctl change: lease reads (ui:273),
`ping` (ui:608, file-capability binary), `ip route show`, and writes under
`/var/lib/halow` (deploy.sh:70) all already work as the `halow-ui` user.

### 1. Proxy failover

`node_get` grows a candidate list instead of a single URL:

1. **lan** — the nodes.json `url` (unchanged).
2. **halow** — `https://<lease-ip>` where lease-ip comes from matching the
   node's `mac` (new nodes.json field, shared with #25) against
   `/var/lib/misc/dnsmasq.leases`. Read from reality at call time — never
   cached long, never derived.

Per-leg timeout drops from 15 s to `NODE_TIMEOUT = 5` s, so the worst case
(both legs dead) is 10 s — *better* than today's single-leg 15 s. The leg
order is decided by vip.py-style damping, per node, in an in-memory dict
(`_NPATH`, same pattern as `_FAILS` at ui:96):

- state: `{"pref": "lan"|"halow", "fails": 0, "oks": 0, "changed": ts}`
- LAN failure: `fails += 1, oks = 0`; demote to halow after
  `DEMOTE_AFTER = 2` consecutive failures (vip.py:47).
- LAN success: `oks += 1, fails = 0`; promote back after
  `PROMOTE_AFTER = 3` consecutive successes (vip.py:48).
- While demoted, requests go halow-first. To let LAN recover without waiting
  for halow to break, at most once per `LAN_RECHECK_S = 30` a proxied request
  additionally fires one cheap LAN `HEAD` (2 s timeout) whose result feeds
  the counters. Bounded: one extra HEAD per 30 s, total.

Which path answered is reported, never guessed:

- proxy passthrough (`/api/node/<name>/<sub>`): response headers
  `X-Node-Path: lan|halow` and `X-Node-Via: <host>` (headers, so the node's
  JSON body passes through unmodified).
- `/api/nodes`: new per-entry fields:

```json
{ "name": "node1", "url": "https://192.168.50.103", "reachable": true,
  "path": "halow", "via": "10.117.0.12",
  "proxy": { "pref": "halow", "fails": 2, "oks": 0, "changed": 1770301234 },
  "diag": { "...": "node payload unchanged" } }
```

- both legs dead → 502 with per-leg errors:

```json
{ "error": "all paths failed",
  "attempts": [ { "path": "lan", "via": "192.168.50.103", "error": "timed out" },
                { "path": "halow", "error": "no lease for aa:bb:cc:11:22:33" } ] }
```

No token, passphrase, or env value appears in any of these shapes — addresses
and counters only. The bearer token goes in the header on the halow leg
exactly as on the lan leg (ui:871); `CERT_NONE` + no hostname check (ui:873-874)
already tolerate the cert not naming the lease IP.

### 2. Address knowledge (`/var/lib/halow/node-addrs.json`)

The lora/fwvip columns of the reach matrix need addresses only the node can
state authoritatively. Whenever a successful proxied `/api/ip` response
passes through `node_get`, harvest `data.netif.address` and
`data.netif.vip_address` (the exact shape vip.py:86-91 reads) into a small
cache, written atomically (tmp + `os.replace` — the storage-discipline
convention item 22 mandates):

```json
{ "node1": { "lora": "10.116.162.104", "fwvip": "10.44.162.104",
             "seen": 1770300000, "source": "device" } }
```

Persisted because "a node that is unreachable is exactly when the LoRa
address is needed and exactly when it cannot be queried" (vip.py:58-61).
Addresses only; no secrets. Unknown stays unknown — never fall back to
deriving from a node number.

### 3. GET /api/reach

Per node, probe each address family and report per-probe evidence:

- **Families**: `lan` (host parsed from nodes.json url), `halow` (lease by
  mac), `lora` and `fwvip` (from node-addrs.json).
- **Probes per address**: one ICMP (`ping -c 1 -W 1 -n`, the ui:601 pattern)
  and one HTTP HEAD. HEAD order: plain-HTTP `http://<addr>/json/report`
  first — the node answers it without spending one of its ~6 scarce TLS
  sessions (~37 KB heap each: roadmap:199, transport-ladder:220) — then
  HTTPS `/` with `CERT_NONE` if :80 refuses. Timeout 2 s each. Any HTTP
  status is "answered" (ui:642-645). **Never a bare TCP connect**
  (roadmap:103 — bare :443 connects crashed nodes).
- **Route guard for lora/fwvip**: before probing, `ip route show to match
  <addr>` filtered of the default route. No specific route → status
  `no-route`, probe skipped: the packet would exit via the upstream default
  and the guaranteed failure would read as a node fault — the exact
  vip.py:106-109 trap. (Routes to 10.116/10.44 on the Pi are out-of-band
  today; the matrix reports their absence honestly instead of faking a
  verdict.)
- **Bounds** (verifier conditions): probes run under a hard
  `REACH_BUDGET_S = 20` monotonic deadline — anything past it reports
  `skipped`. The whole matrix is cached for `REACH_TTL_S = 20` behind a
  lock, so a double-tap or two open tabs cost one probe run. The UI calls it
  from a button only — never on page render (the Nodes tab already renders
  without timers, ui:1261).
- **Input hygiene**: every probed address is matched against
  `^\d{1,3}(\.\d{1,3}){3}$` before being shelled to ping (statuses
  `bad-addr` otherwise). Lease IPs and device-reported addresses are
  external input; a hostile or corrupted node must not reach `sh()`.

Response shape:

```json
{ "generated": 1770300200, "cached": false, "age_s": 0,
  "elapsed_s": 6.4, "budget_s": 20,
  "nodes": [
    { "name": "node1", "mac": "aa:bb:cc:11:22:33",
      "paths": [
        { "family": "lan", "addr": "192.168.50.103",
          "icmp": { "ok": true, "ms": 2.1 },
          "http": { "ok": true, "status": 200, "ms": 38.0,
                    "url": "http://192.168.50.103/json/report" } },
        { "family": "halow", "addr": "10.117.0.12",
          "icmp": { "ok": true, "ms": 9.7 },
          "http": { "ok": true, "status": 401, "ms": 120.5,
                    "note": "server answered - the path is up" } },
        { "family": "lora", "addr": "10.116.162.104", "status": "no-route" },
        { "family": "fwvip", "addr": "10.44.162.104", "status": "no-route" } ] },
    { "name": "node2", "mac": null,
      "paths": [
        { "family": "lan", "addr": "192.168.51.104",
          "icmp": { "ok": false }, "http": { "ok": false, "error": "timed out" } },
        { "family": "halow", "status": "no-mac" },
        { "family": "lora", "status": "unknown" },
        { "family": "fwvip", "status": "unknown" } ] } ] }
```

Statuses: probed (icmp/http objects present) | `no-mac` | `no-lease` |
`no-route` | `unknown` | `skipped` | `bad-addr`. Absence is reported as
absence, with the reason.

### nodes.json v2 (field shared with #25 — define once)

```json
{ "nodes": [
    { "name": "node1", "url": "https://192.168.50.103",
      "mac": "aa:bb:cc:11:22:33", "token": "CHANGE-ME" } ] }
```

`mac` optional, lowercase colon-separated (dnsmasq.leases stores lowercase).
Without it, that node's halow leg and halow reach column report `no-mac` and
everything else behaves as today.

### Privilege model

All new code runs in halow-ui (unprivileged, systemd/halow-ui.service:10).
Leases: already read unprivileged (ui:273). ping: file capability, proven by
`/api/diag/ping`. `ip route show`: read-only, unprivileged.
`/var/lib/halow`: halow-ui-owned (deploy.sh:70). **No sudoers-halow-ui
change. No halow-mon change. No new unit.**

## Implementation steps

Each step is one commit; all paths repo-relative.

1. **Shared lease reader.** In ui/halow_ui.py add
   `read_leases(path="/var/lib/misc/dnsmasq.leases")` returning
   `[{expiry, mac, ip, host}]`; replace the inline parses in `api_router`
   (ui:271-280) and `api_diag_bundle` (ui:841-846). Pure refactor, no
   behavior change. The path argument exists so a bench test can feed a
   crafted file.
2. **nodes.json `mac` field.** Add `mac` to config/nodes.json.example with a
   doc line (optional; lowercase; matches dnsmasq.leases). If #25 has
   already landed this, skip — the field must exist exactly once.
3. **Address cache.** Add `NODE_ADDRS = "/var/lib/halow/node-addrs.json"`,
   `_load_addrs()`/`_save_addrs()` (tmp + `os.replace`), and the harvest
   hook in `node_get`: when the proxied path is `/api/ip` and the response
   parses, store `data.netif.address` → `lora`,
   `data.netif.vip_address` → `fwvip`, `seen`, `source: "device"`.
4. **Failover in `node_get`.** Constants `NODE_TIMEOUT=5`,
   `DEMOTE_AFTER=2`, `PROMOTE_AFTER=3`, `LAN_RECHECK_S=30`; module dict
   `_NPATH`; helper `_halow_url(node)` (mac → lease ip via `read_leases()`
   → `https://<ip>`). `node_get` returns `(data, path, via)`; tries pref
   leg then the other; updates damping on LAN outcomes; fires the
   rate-limited LAN recheck HEAD while demoted. Update both call sites:
   `api_nodes` (adds `path`/`via`/`proxy` fields) and `api_node_proxy`
   (sets `X-Node-Path`/`X-Node-Via`; 502 body becomes the per-leg
   `attempts` shape).
5. **GET /api/reach.** Probe helpers `_icmp1(addr)` (subprocess list form,
   `ping -c 1 -W 1 -n`), `_head(addr)` (plain-HTTP `/json/report` then
   HTTPS `/`, 2 s, HEAD, any status = answered), `_has_route(addr)`
   (`ip route show to match`, default filtered), the IPv4 regex gate, the
   20 s budget, the 20 s TTL cache behind a `threading.Lock`. Endpoint
   `@app.get("/api/reach")` `@authed`.
6. **UI.** Nodes tab (`nodes()` JS, ui:1248-1261): show `path`/`via` as a
   badge per node card ("via halow 10.117.0.12" when demoted); add a
   "reach matrix" button that fetches `/api/reach` on click and renders the
   per-node table with per-cell icmp/http results and statuses. No timer,
   no fetch on render.
7. **Docs.** README state-log entry (measured numbers from the acceptance
   run) and mark roadmap item 26 with evidence, matching the existing
   "DONE 2026-08-05 (…)" convention in docs/feature-roadmap.md.

## Surface changes

New/changed API endpoints:

| Endpoint | Change |
|---|---|
| `GET /api/reach` | NEW — cached per-node multi-path matrix (auth required) |
| `GET /api/nodes` | entries gain `path`, `via`, `proxy` {pref/fails/oks/changed} |
| `GET /api/node/<name>/<sub>` | gains `X-Node-Path`/`X-Node-Via` headers; 502 body becomes per-leg `attempts` |

halowctl commands: **none** (machinery is unprivileged and UI-resident).

UI elements:

| Element | Change |
|---|---|
| Nodes tab node card | path badge (lan/halow + address answering) |
| Nodes tab | "reach matrix" button + result table (fetch on click only) |

systemd units: **none** new or changed.

Config/state files:

| File | Change |
|---|---|
| `/etc/halow/nodes.json` (+ example) | optional `mac` per node (shared with #25) |
| `/var/lib/halow/node-addrs.json` | NEW — device-reported lora/fwvip cache, atomic writes, no secrets |
| `config/sudoers-halow-ui` | unchanged (explicitly) |

## Testing & acceptance criteria

All numbers reported [M] with the command that produced them; vendor or
derived values are [C] and say so.

### Testable today (pre-association)

1. **Lease reader**: `read_leases()` against a crafted temp file returns the
   4-tuple dicts; `api_router` and `api_diag` output unchanged for the
   live file (diff before/after refactor).
2. **Bounded failure**: with node2's LAN IP dark (its real bench state,
   README:66-68), `time curl -sk -u … https://10.117.0.1:8443/api/nodes`
   completes with node2 `reachable: false` in ≤ ~12 s total for the node2
   legs (two 5 s timeouts), against ≥ 15 s today. Measure both.
3. **Damping drill on LAN alone**: point node1's nodes.json entry at a
   black-hole LAN IP, restart halow-ui, issue 3 proxied requests: request 1
   fails lan → tries halow → `no-lease` (no mac joined yet) → 502 with both
   attempts; by request 2 `proxy.fails >= 2` and `pref` flips to `halow`
   in `/api/nodes`. Restore the real URL: after 3 successes `pref`
   returns to `lan`. Verify via the `proxy` block and `X-Node-Path`.
4. **Reach, LAN column**: `curl -sk … /api/reach` shows node1 lan row with
   icmp ok + HEAD answered (record ms), node2 lan row failing, halow rows
   `no-mac`/`no-lease`, lora/fwvip `no-route` or `unknown`, and
   `elapsed_s` < `budget_s`.
5. **Cache bound**: two `/api/reach` calls 5 s apart — second returns
   `cached: true, age_s ≈ 5` and near-zero elapsed. Confirm no probe ran
   (no new ping in `pgrep`/timing).
6. **Address harvest**: `curl … /api/node/node1/ip` then read
   `/var/lib/halow/node-addrs.json` — lora/fwvip match what the node itself
   reported (compare against the raw `/api/ip` body). [M] receiver-side:
   the values came from the device, not a prefix constant.
7. **Secret hygiene**: grep the bodies and headers of `/api/nodes`,
   `/api/reach`, a proxied 502, and `journalctl -u halow-ui` for the bearer
   token and `HALOW_PASSPHRASE` value. Zero hits is the pass condition
   (the PSK has leaked twice via "harmless" echoes).
8. **Injection gate**: hand-write a node-addrs.json entry of
   `"fwvip": "1.2.3.4; touch /tmp/pwn"` — reach reports `bad-addr`,
   `/tmp/pwn` does not exist.

### Needs a joined station

9. **Halow column live**: with a node associated, leased (reservation per
   item 15), and `mac` set — reach halow row shows icmp ok and HEAD
   answered *by the node* (the HTTP status is the receiver-side proof).
   Record RTT and HEAD ms [M].
10. **The blind-console drill (the point of the issue)**: kill the node's
    2.4 GHz LAN path (power off the household AP or disable node WiFi via
    its admin UI). Within 2 proxied requests the proxy demotes; the Nodes
    tab still renders full `/api/diag` with `X-Node-Path: halow`; reach
    shows lan dead + halow alive simultaneously. Restore LAN; within 3
    successful rechecks (≤ ~90 s at LAN_RECHECK_S=30) it promotes back.
    Log timings [M] in the README state log, first-contact style.
11. **fwvip through halow (exploratory)**: manually
    `sudo ip route replace <fwvip>/32 via <halow-lease-ip>` on the Pi, rerun
    reach — the fwvip row flips from `no-route` to probed. Confirms the
    matrix reports route-presence truthfully in both directions. Remove the
    route afterward (a control that drifts is the bug).

## Out of scope

- **Gateway-resident VIP route management** (a vip.py port to the Pi):
  reach *reports* missing 10.116/10.44 routes; installing/flapping them is
  root-side route mutation and its own design.
- **POST/mutating proxy**: the proxy stays GET-only, whitelist unchanged.
- **Auto-onboarding of unknown MACs** and fleet health classification —
  that is #25; this issue only consumes its `mac` field.
- **Node→node reach** (node1 probing node2 through the AP): node-side work.
- **Background probing**: no timer fires reach; on-demand + cache only.
- **Persisting damping state**: `_NPATH` is in-memory by design; a UI
  restart resets pref to lan and re-demotes within ~10 s if LAN is dead.

## Risks & gotchas

- **Scarce node TLS sessions**: each HTTPS probe costs the ESP32 one of ~6
  sessions at ~37 KB heap (roadmap:199, transport-ladder:220). Hence
  plain-HTTP `/json/report` first, HEAD not GET, 20 s cache, button-only
  trigger. If a future node build disables :80, the HTTPS fallback still
  works — just costlier; do not "optimize" the cache away.
- **Derived addresses are how this dies silently**: vip.py shipped with a
  prefix constant one off from the firmware and failover "could only ever
  fail" with no error anywhere (vip.py:54-57). Every address here comes
  from leases or the device's `/api/ip`; if you find yourself computing
  `10.x.hi.lo` from a node number, stop.
- **The no-route trap**: probing 10.44/10.116 without a specific route sends
  the probe out the upstream default and manufactures a failure that "reads
  as the VIP being broken" (vip.py:106-109). The route guard is
  load-bearing; keep it ahead of the probes.
- **Stale leases**: dnsmasq keeps a lease after a station vanishes, so the
  halow leg can point at a dead address for up to the lease lifetime. The
  5 s leg timeout bounds the cost; the reach icmp row exposes the truth.
  This also intersects the bench's "associated but answers no ARP" trap —
  item 18's per-minute station ladder is the systematic answer; reach is
  the on-demand cross-check, not a replacement.
- **Concurrency**: the app runs `threaded=True` (audit A2). `_NPATH`
  counter races are benign (same posture as `_FAILS`), but the reach cache
  must be checked and filled under its lock or two tabs double-probe —
  exactly what the verifier bounded against.
- **Half-open failure modes**: ICMP-ok + HTTP-dead (service crashed, radio
  fine) and ICMP-dead + HTTP-ok (ICMP filtered) are both real; that is why
  every cell carries both probes and the UI must render them separately,
  not collapse them into one boolean.
- **Coordination with #25**: both items key nodes by MAC on the existing
  stores (dhcp-reserve file + nodes.json). Land the `mac` field once; if
  #25's cached fleet watcher exists by then, its freshness data can feed
  `pref` later — do not build a second inventory here.
- **First-association week interactions**: reach probes are extra RF
  traffic on a link being characterized by items 16/19. Trivial load
  (2 packets + 1 HEAD per family), but keep reach out of any scripted loop
  while the link tester (item 19) is measuring, or its numbers inherit
  your noise.
