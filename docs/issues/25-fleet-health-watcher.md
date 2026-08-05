# 25. Gateway-resident fleet health watcher (cached nodewatch on the existing MAC stores)
> Tier 3 - fleet-ops | Effort: medium | Impact: high | Depends on: none — 26 and 30 depend on this one (it defines the nodes.json `mac` + interval schema; land the schema here, once)

## Problem

`/api/nodes` live-fetches every configured mesh node on every request. Each fetch is
HTTPS + Bearer against an ESP32 whose web server needs **5.0 s per authenticated request
on a cache hit, 6.0 s on a miss [M]** (mesh-v4 `firmware/ADMIN-API.md:183-189` — nearly
all of it is the stock Meshtastic server, at 3.7 s even without TLS or auth). Worse than
the latency is the session cost: each HTTPS hit transiently occupies one of the node's
**~6 concurrent TLS sessions [M]** (mesh-v4 `HANDOVER.md:8-36` — the ceiling was 2 until
the mbedtls-in-PSRAM fix, and "exactly Chrome's per-host max" means one browser talking
directly to a node can consume all of them). This fleet has already *reset a node* by
holding two TLS sessions when the ceiling was 2 (mesh-v4 `tests/README.md:19`: "node
resets under load — two TLS sessions exhausting DMA-capable heap"). A monitoring page
that costs the monitored device sessions and seconds per viewer-render is the mesh-v4
`/api/metrics` mistake re-implemented at fleet scale: the measurement perturbs the thing
measured (`HANDOVER.md:56-58` — `/api/metrics` reported `internal_free 39812` while port
80 simultaneously said `101260` on the same node).

The gateway UI already half-knows this: the 15-second auto-refresh deliberately skips
the Nodes tab (`ui/halow_ui.py:1261`, `if(tab!=="Nodes")render()`). But every tab
switch, page load, and additional viewer still runs the full serial live-fetch, and
nothing serializes or backs off. Two viewers double every cost. Meanwhile mesh-v4
already solved fleet health correctly in `tools/nodewatch.py` — dead-vs-quiet
discipline, staleness at 3x the node's own reported interval, HTTP errors counted as
*alive*, reboot-loop detection — but it runs on a PC in the client repo. The gateway,
which is on 24/7 and sits at the network junction, watches nothing.

This matters now because first HaLow association is imminent (blocked only on a
decoupling capacitor + antenna confirmation). The moment a station joins, the operator
needs one page that answers "which nodes are alive, which are quiet-by-design, which are
rebooting in a loop" without taxing the nodes to ask — and needs a way to turn an
unknown just-associated MAC into a named, pinned, monitored fleet member in one click.

This issue came out of the client-codebase audit (roadmap v2 item 25, adversarially
verified). The verifier's constraints are binding: **no fourth parallel MAC table**
(extend the reservations store and nodes.json the gateway already has), and the poller
must be **radio-respectful** — mesh-v4 learned that an unguarded second connection to a
busy node took 180 s where the bare operation takes 2 s (`HANDOVER.md:244-245`, codified
as `_radio_guard()` in `tools/webui/server.py:255-267`).

## Current state

Verified this session, both repos.

**Gateway — `ui/halow_ui.py`:**
- `node_get()` (:869-876): HTTPS GET with `Authorization: Bearer <token>`, self-signed
  certs accepted, 15 s timeout. Every call costs the node a TLS session.
- `/api/nodes` (:879-897): loads `/etc/halow/nodes.json`, then serially live-fetches
  `/api/diag` from every node per request. No cache, no serialization across concurrent
  requests (the server is `threaded=True`, :1273 — two simultaneous viewers produce two
  concurrent fetch loops against the same nodes).
- Node proxy `/api/nodes/<name>/<sub>` (:900-913): whitelisted live pass-through; stays
  as the operator's deliberate "poke the node now" path.
- Nodes tab JS (:1248-1256) renders the live fetch; auto-refresh exempts the tab (:1261).
- Reservations are read by parsing `/etc/dnsmasq.d/halow-reservations.conf` (:333-341,
  `dhcp-host=mac,ip,name` lines); leases by parsing `/var/lib/misc/dnsmasq.leases`
  (:271-279, MAC at field 2). Both stores are MAC-keyed already.
- The UI process (user `halow-ui`) already writes `/var/lib/halow` (:544-545,
  `throughput.jsonl`), and `deploy.sh:70` chowns that directory `halow-ui:halow-ui`.

**Gateway — config and stores:**
- `config/nodes.json.example:3-6`: entries are `{name, url, token}` only. **No `mac`
  field exists today** — nodes.json cannot be joined to reservations, leases, or
  `iw station dump`.
- `scripts/halowctl` `dhcp-reserve` (:137-161): validates MAC by regex and IP as
  10.117.0.x (:146-147), rewrites the reservations conf, restarts dnsmasq. The
  audited-sudo-surface pattern to extend.
- `config/sudoers-halow-ui:18` grants the UI `halowctl dhcp-reserve *`. There is no
  sudoers path for editing nodes.json — and none is possible today: `deploy.sh:58`
  installs it `-m640 -o root -g halow-ui`, so the UI can read it but not write it.
- **Found this session:** `deploy.sh:41,58` regenerates and reinstalls nodes.json
  *unconditionally on every deploy* (unlike `halow.env`, which is guarded at :55 with
  `[ -f ... ] ||`). Any entry added at runtime is silently destroyed by the next deploy.
  Onboarding is amnesiac until this is fixed — step 1 below.

**Client — `mesh-v4/tools/nodewatch.py` (the semantics to port):**
- Two orthogonal questions, reachability vs freshness (:19-27 docstring);
  `HOLD_MULTIPLIER = 3` (:52) judges staleness at 3x the node's *own reported* interval,
  because firmware silently clamps configured intervals (:27-29) — never hardcode.
- `expected_interval()` (:167-180): min of `telemetry_interval`,
  `position_broadcast_secs`, `neighbor_info_interval` from the node's `/api/settings`;
  fall back to 3600 (under-alerting beats crying wolf).
- HTTP 401/404/429 mean **alive** with a distinct alert, never "down" (:215-233 —
  "calling this down would send someone to check power when the node is fine").
- Empty body / malformed JSON mean **unknown**, not down (:126-146, :238-242 — nodes
  return empty bodies under load).
- `wifi-down`: unreachable over IP but another node heard it on LoRa (:199-213, peer
  `/api/mesh` `last_heard`) — a different repair job from "node dead".
- Reboot loops: a node rebooting in a loop answers every poll and looks healthy; only
  the `reboot_count` delta betrays it (:285-290).
- **Spec correction (code beats docstring):** nodewatch *computes and reports*
  `stale_after_s` (:252-254) but never actually transitions to `stale` on the 3x rule —
  the only `stale` assignment in the file is the reboot delta (:290). The gateway port
  makes the 3x rule operative (design below) and this issue says so rather than
  pretending to port behavior that does not exist.
- State file saved atomically, tmp + `os.replace` (:160-164) — adopt the same here
  (coordinates with issue 22's storage-discipline convention).

**Client — identity and session economics:**
- `mesh-v4/config/resolve.sh:12-18`: mDNS hostnames are hardcoded to "Meshtastic" in
  firmware, so with two nodes the hostname collides and only the TXT id differs; "the
  MAC never changes, so this is the most durable fallback of the lot". MAC is the key.
- Plain-HTTP `http://<node>/json/report` costs **no TLS session** and cannot perturb
  what it measures (`HANDOVER.md:56-58`,
  `results/tls-session-ceiling-is-configuration-20260801.md:7-9`). It is unauthenticated
  stock Meshtastic (position block needs patch 0001, `firmware/README.md:18` — already
  deployed on this fleet).

## Design

One new unprivileged long-running daemon, `halow-watch`, polls the fleet so viewers
never do. `/api/nodes` becomes a file read. Identity is the HaLow-side MAC, stored in
the two places the gateway already keeps MACs — the dnsmasq reservations file and (new
field) `/etc/halow/nodes.json`. No new inventory file, nothing cryptographic
(operational data only: names, IPs, URLs, intervals — the PKI-nodedb rejection stands;
consuming the nodes' plain-HTTP endpoint does not conflict with the gateway's own
cleartext-API rejection, which is about the gateway *offering* one).

### Where each piece lives, and privilege

| Piece | Home | Runs as | Why |
|---|---|---|---|
| Poller daemon | `scripts/halow-watch` → `/usr/local/bin/halow-watch`, unit `halow-watch.service` | `halow-ui` (unprivileged, `NoNewPrivileges=yes`) | Needs only: read nodes.json (640 root:halow-ui — group grants it), read leases/reservations (world/group-readable), run `iw dev halow0 station dump` (works unprivileged — the UI already does it at :195), write `/var/lib/halow` (owned halow-ui). Root buys nothing; a bearer token does not belong in a root oneshot. Not folded into halow-mon: that is a 60 s root oneshot and a poll cycle can legitimately take longer than its slot; not folded into halow-ui: cache must survive UI restarts and the UI stays request-driven. |
| nodes.json editing | new `halowctl node` subcommand | root via sudo (one new sudoers line) | nodes.json is root-owned by design; every UI mutation routes through halowctl, the audited sudo surface. |
| Cached fleet view | `GET /api/nodes` in `ui/halow_ui.py` | halow-ui | Serves `/var/lib/halow/nodewatch.json`; zero node contact. |
| Onboarding | `POST /api/nodes/adopt` in `ui/halow_ui.py` | halow-ui → sudo halowctl | Composes the two existing/new halowctl commands; no third store. |

### nodes.json schema v2 (the change 26 and 30 reuse — design it once, here)

```json
{
  "_doc": "Copy to /etc/halow/nodes.json with the real bearer token. Never commit the real file. mac = the node's HaLow (MM6108) station MAC as seen by this gateway — NOT the ESP32's 2.4 GHz WiFi MAC from mesh-v4 device.env. Empty string until first association teaches it to us. expected_interval_s = optional override; null means learn it from the node's own /api/settings (firmware silently clamps intervals — nodewatch lesson).",
  "nodes": [
    { "name": "node1", "url": "https://192.168.50.103", "token": "CHANGE-ME",
      "mac": "", "expected_interval_s": null },
    { "name": "node2", "url": "https://192.168.51.104", "token": "CHANGE-ME",
      "mac": "", "expected_interval_s": null }
  ]
}
```

All readers use `.get()` — files without the new keys stay valid. `mac` is
lowercase-normalized on write. The plain-HTTP report URL is *derived* (scheme swapped to
`http://` on the same host as `url`), not a new field.

### Watcher loop (bounded, serialized, radio-respectful)

Config from `/etc/halow/halow.env`: `HALOW_WATCH_INTERVAL` (seconds between cycle
starts; default 120, clamped 60-3600), `HALOW_WATCH_DIAG_EVERY` (HTTPS deep-poll every
Nth cycle per node; default 10, clamped >=1). Per-request timeout 10 s. One cycle:

1. Load stores: nodes.json; reservations conf; leases; `iw dev halow0 station dump`.
2. For each configured node, **strictly one at a time** (single thread — the watcher can
   never hold two connections to the fleet, let alone to one node; the `_radio_guard`
   lesson): skip if in backoff, else
   `GET http://<host>/json/report` (no auth, no TLS session). Record RTT and a
   *whitelisted* field subset (heap, wifi rssi, airtime — never the whole body).
3. Every `HALOW_WATCH_DIAG_EVERY`-th successful cycle per node (and on first sight):
   `GET https://<url>/api/diag` + `/api/settings` with the bearer token — two sequential
   requests, at most one TLS session held at a time. Learn and cache: node id (needed
   for peer `last_heard` matching — learned, not configured, so the schema stays
   minimal), `reboot_count`, battery, applied intervals.
4. On report failure, classify in nodewatch order: try `https /api/diag` once; an HTTP
   status (401/404/429/other) → `unknown` + the nodewatch alert text ("auth failure,
   not a dead node", etc.); else ask each *currently-alive* peer's `/api/mesh` (HTTPS,
   serialized, only on failure — nodewatch's own economy, :202-213) whether it heard the
   target's learned id on LoRa → `wifi-down`; empty/malformed body → `unknown`; else
   `down`.
5. Freshness (the 3x rule, made operative): `stale_after_s = 3 * interval` where
   interval = `expected_interval_s` override, else learned from `/api/settings`, else
   3600. A node that is reachable but whose reboot_count jumped, or whose last
   radio-side evidence (LoRa `last_heard` when known; once HaLow-associated, station
   activity and lease renewal) exceeds `stale_after_s`, is `stale`.
6. Cross-check gateway evidence: a MAC associated on halow0 with a lease but
   IP-unreachable gets the "associated but answers no ARP" alert (the bench trap).
7. MACs present in station dump or halow0 leases but in neither nodes.json nor the
   reservations file → `unknown_stations` with a proposed reservation (current lease IP)
   for one-click adoption.
8. Write `/var/lib/halow/nodewatch.json` atomically (tmp + `os.replace`); append one
   summary line per node to `/var/lib/halow/nodewatch-history.jsonl`, ring-pruned like
   halow-mon's METRICS (halow-mon:135-142 pattern, 2880-line cap).
9. Sleep until the next interval boundary. Cycles never overlap.

Backoff: per-node consecutive-failure count `n` skips `2^min(n,3)` cycles (max 8x = 16
min at defaults), reset on success. A dead node costs the fleet one timeout per 16
minutes, not one per cycle.

### Cache shape (`/var/lib/halow/nodewatch.json`, served by `/api/nodes`)

```json
{
  "t": 1754448000, "interval_s": 120,
  "nodes": [{
    "name": "node1", "mac": "aa:bb:cc:dd:ee:ff", "host": "192.168.50.103",
    "state": "healthy", "detail": "reachable (/json/report, 3.9s)",
    "checked_at": 1754447995, "rtt_ms": 3900, "alerts": [],
    "expected_interval_s": 1800, "stale_after_s": 5400,
    "consecutive_failures": 0,
    "assoc": {"associated": false, "lease_ip": null, "reservation_ip": null},
    "learned": {"id": "!abcd1234", "reboot_count": 7, "battery_pct": 84,
                "last_diag_at": 1754447400},
    "report": {"heap_free": 101260, "wifi_rssi": -63}
  }],
  "unknown_stations": [{
    "mac": "22:33:44:55:66:77", "first_seen": 1754447000,
    "lease_ip": "10.117.0.53", "lease_host": "espressif", "associated": true,
    "proposed": {"reservation_ip": "10.117.0.53", "name": "node3"}
  }]
}
```

States: `healthy` / `stale` / `wifi-down` / `down` / `unknown` — nodewatch's exact
vocabulary, because the operator already knows it. **No token, ever, in this file, the
history file, the journal, or any API response.** The SAE PSK has leaked twice through
"harmless" echoes; the bearer token gets the same paranoia: it exists in nodes.json
(640) and in the memory of halow-ui and halow-watch, nowhere else.

### API contracts

`GET /api/nodes` → the cache verbatim plus `{"cached": true, "age_s": 42}`. If the
cache file is absent or older than `3 * interval_s`, still serve it but set
`"watcher_stale": true` (the UI banners it — a dead watcher must not look like a healthy
fleet). `GET /api/nodes?live=1` keeps the old serial live-fetch as a deliberate operator
action, response tagged `"cached": false` (same shape as today's :879-897 output).

`POST /api/config/nodes` — `op=add|del`, fields `name`, `mac`, `url`,
`expected_interval_s`, `token` (write-only: passed to halowctl on **stdin**, the
set-passphrase pattern at halowctl:244-257 — never argv, never echoed). `add` upserts by
name. Shells to `sudo halowctl node ...`.

`POST /api/nodes/adopt` — `mac`, `ip`, `name`, optional `url` (default
`https://<ip>`), optional `token` (default: reuse the token the existing entries share —
deploy.sh:5-6, the bench runs one operator credential). Runs `halowctl dhcp-reserve add`
then `halowctl node add`. Not an identity/destructive change → no `confirm=1`.

### halowctl subcommand

```
halowctl node list                                  # name, url, mac, interval, token: (set)/(unset) — never the token
halowctl node add name=X mac=A url=U [interval=N]   # token on stdin (empty keeps/reuses existing); upserts by name
halowctl node del name=X | mac=A
```

Validation mirrors dhcp-reserve (:146-148): MAC regex, `name` `[A-Za-z0-9-]+`, url
`^https?://`. Writes via tmp + `mv`, restores `-m640 root:halow-ui`. One sudoers line:
`halow-ui ALL=(root) NOPASSWD: /usr/local/bin/halowctl node *`.

## Implementation steps

1. **Schema + deploy guard.** Update `config/nodes.json.example` to schema v2 (above).
   In `scripts/deploy.sh`, guard the nodes.json install (:58) with
   `[ -f /etc/halow/nodes.json ] ||` exactly like halow.env (:55) — without this, every
   deploy erases adopted nodes. Commit message states the clobber bug.
2. **`halowctl node` subcommand.** Add the `node)` case to `scripts/halowctl`:
   list/add/del per the contract above; token via `head -c 256` stdin; atomic write;
   perms restored; `list` proves it never prints tokens.
3. **Sudoers.** Append the `halowctl node *` line to `config/sudoers-halow-ui`.
4. **Watcher daemon.** New `scripts/halow-watch` (python3, stdlib only, halow-mon
   style): env parse with clamps, store loaders (reservations parser matching
   ui:333-341, leases matching ui:271-279, station-dump matching ui:195-206),
   serialized poll loop, nodewatch classification incl. 401/404/429-alive and
   empty-body-unknown, learned-id peer check on failure only, reboot-delta, 3x
   freshness, backoff, atomic cache write, ring-pruned history.
5. **Unit + install.** New `systemd/halow-watch.service`: `Type=simple`,
   `User=halow-ui`, `Restart=on-failure`, `RestartSec=10`, `NoNewPrivileges=yes`,
   `ProtectSystem=strict`, `ReadWritePaths=/var/lib/halow`, `PrivateTmp=yes`. Add to
   `deploy.sh` (install script + unit, enable/start) and `verify.sh` (unit active,
   cache file fresher than `3 * HALOW_WATCH_INTERVAL`).
6. **UI read path.** In `ui/halow_ui.py`: rewrite `/api/nodes` to serve the cache with
   `age_s`/`watcher_stale`; move today's live loop behind `?live=1` unchanged.
7. **UI write path.** Add `POST /api/config/nodes` and `POST /api/nodes/adopt`
   (both shell to halowctl via the existing `halowctl()` helper; token via `stdin=`).
8. **Nodes tab.** Rewrite `nodes()` JS: fleet table (state color-coded ok/warn/bad/dim,
   battery, reboots, check age, detail, alerts), watcher-stale banner, adopt card per
   unknown station (one button → `/api/nodes/adopt`), per-node "live" button retained
   via the untouched proxy (:900-913). Flip the auto-refresh guard at :1261 so the
   Nodes tab joins the 15 s cycle — it is now a file read.
9. **Docs.** Mark roadmap item 25 in `docs/feature-roadmap.md` (done-style annotation),
   note the schema in `docs/software-stack.md`, and record measured before/after
   `/api/nodes` latency.

## Surface changes

| API endpoint | Change |
|---|---|
| `GET /api/nodes` | Now cached (file read, `cached:true`, `age_s`, `watcher_stale`); `?live=1` preserves today's live fetch |
| `GET /api/nodes/<name>/<sub>` | Unchanged (deliberate live proxy) |
| `POST /api/config/nodes` | NEW — add/del nodes.json entries via halowctl; token write-only via stdin |
| `POST /api/nodes/adopt` | NEW — one-click reservation + nodes.json entry for an unknown associated MAC |

| halowctl | Change |
|---|---|
| `halowctl node list\|add\|del` | NEW — the only writer of /etc/halow/nodes.json; never prints tokens |
| `halowctl dhcp-reserve` | Unchanged (reused by adopt) |

| UI | Change |
|---|---|
| Nodes tab | Cache-driven fleet table + states + alerts; adopt card; joins 15 s auto-refresh; live buttons kept |

| systemd | Change |
|---|---|
| `halow-watch.service` | NEW — long-running, `User=halow-ui`, no sudo, restart-on-failure |

| Config / files | Change |
|---|---|
| `config/nodes.json.example` | +`mac`, +`expected_interval_s` (schema shared with issues 26/30) |
| `/etc/halow/halow.env` | +`HALOW_WATCH_INTERVAL` (default 120), +`HALOW_WATCH_DIAG_EVERY` (default 10) |
| `config/sudoers-halow-ui` | +`halowctl node *` |
| `/var/lib/halow/nodewatch.json`, `nodewatch-history.jsonl` | NEW — cache + bounded ring; no secrets |
| `scripts/deploy.sh` | nodes.json never-clobber guard; install/enable halow-watch |

## Testing & acceptance criteria

### Testable today (pre-association — both real nodes are reachable on the LAN now)

- **Cache speed [M]:** `curl -sk -u ... -w '%{time_total}' https://gw:8443/api/nodes`
  < 0.5 s with the watcher running; `?live=1` on the same fleet measures 10-12 s
  (2 nodes x 5-6 s) — record both numbers in the commit.
- **Session frugality, confirmed at the receiver [M]:** with the watcher at defaults for
  30 min, sample `http://<node>/json/report` heap before/during — no session-shaped
  heap dips outside the every-Nth diag windows. Bounded capture on the gateway
  (`timeout 300 tcpdump -c 500 'dst <node> and tcp dst port 443'`): SYN count == the
  expected deep-poll count, and **zero** additional 443 SYNs while two browser tabs sit
  on the Nodes tab for 10 minutes.
- **Classification without a corpse:** stop node1's web server / power it off → `down`
  within one cycle + backoff visible as widening `checked_at` gaps in
  `nodewatch-history.jsonl`, capped at 8x. Break the token (one wrong char in
  nodes.json) → `unknown` with the "auth failure, not a dead node" alert — **must not**
  report `down`. Restore → `healthy` next cycle, backoff cleared.
- **wifi-down:** disable node1's WiFi while node2 still hears it on LoRa (standard
  mesh-v4 bench op) → `wifi-down` naming node2 and the heard-age.
- **Reboot delta:** reboot node1 between cycles → `stale` + "rebooted since last poll
  (n -> n+1)" alert on the next deep poll.
- **Secrets:** after a 1 h soak, `grep -r "$ADMIN_TOKEN" /var/lib/halow/ && echo LEAK`
  finds nothing; `journalctl -u halow-watch | grep -c "$ADMIN_TOKEN"` is 0;
  `halowctl node list` shows `(set)`, not the token.
- **Deploy amnesia fixed:** `halowctl node add` a third entry, run `deploy.sh`, entry
  survives.
- **Store parsers:** point the watcher at fixture copies of a leases file and
  reservations conf containing a MAC absent from nodes.json → `unknown_stations` entry
  with the correct proposed IP (full end-to-end adopt needs a joined station, below).
- **Watcher death is visible:** `systemctl stop halow-watch`, wait `3 * interval` →
  `/api/nodes` sets `watcher_stale:true` and the UI banners it.

### Needs a joined station

- First real HaLow association: the MM6108 STA MAC appears in `iw station dump` + halow0
  leases → `unknown_stations` within one cycle; one click on the adopt card creates the
  dnsmasq reservation (verify `halowctl dhcp-reserve list`) and the nodes.json entry
  with the *HaLow* MAC; the node is polled and classified on the next cycle over
  `https://10.117.0.x`.
- `assoc` block truthful against `iw dev halow0 station dump` run by hand [M].
- Freshness against the 3x rule using station-side evidence: with the node associated
  and its report interval learned, silence beyond `3 * interval` at the radio flags
  `stale` — confirm the timestamps arithmetic against the recorded station activity.

## Out of scope

- **LAN/HaLow path failover and the reach matrix** — issue 26 (it consumes this issue's
  `mac` field; the watcher here polls the configured `url` host only).
- **Check-in contract, adherence ledger, `POST /api/checkin`** — issue 30 (consumes
  `expected_interval_s`).
- **Rung-cost windows** (issue 20) and the **radio-side per-station ICMP ladder in
  halow-mon** (issue 18) — 18 watches the *radio/DHCP/ARP* layer as root each minute;
  this issue watches the *node API* layer; they meet only in the cache's `assoc` block.
- **Alerting/notifications** beyond cache + UI states. No email, no push.
- **PKI, keys, certs in any store** — the v1 rejection stands; this is operational data
  only.
- **Serializing the UI's live node proxy against the watcher** — noted as a risk below,
  not solved here.

## Risks & gotchas

- **Two MAC universes.** The `mac` field is the node's HaLow (MM6108) station MAC — the
  identity on *this gateway's* network and stores. mesh-v4 `device.env` MAC is the
  ESP32's 2.4 GHz WiFi MAC and will not match. Copying it in "to save time" creates a
  join that never matches anything. The field stays empty until first association
  teaches the real one.
- **Concurrent connections to one node.** The watcher is internally serialized, but the
  live proxy (`/api/nodes/<name>/<sub>`) and `?live=1` can still coincide with a watcher
  poll. Budget: 1 watcher connection + operator actions, against a measured ceiling of
  ~6 sessions [M] — acceptable, but remember the 180 s-vs-2 s lesson if the node is
  busy; the proxy is for deliberate pokes, not dashboards.
- **`/json/report` field set is unverified for reboot/battery.** Reboot detection rides
  the low-cadence HTTPS `/api/diag` (where nodewatch reads `reboot_count`). At the
  bench, capture one full `/json/report` body; if reboot/battery are present, promote
  them to the cheap path and shorten detection latency. Do not assume — measure.
- **nodewatch's 3x rule was aspirational in the source** (reported, never enforced —
  `nodewatch.py:252-254` vs :290). This port enforces it; expect to tune against real
  station cadence, and keep the fallback interval conservative (under-alerting beats
  crying wolf, nodewatch's own rule).
- **`iw` "inactive time" is optimistic.** hostapd polls stations before deauth (issue 28
  territory), refreshing activity without node intent. Treat station activity as
  an upper bound on freshness, never proof of application liveness.
- **halow-mon writes the same directory as root** and chmods its own files; halow-watch
  writes as halow-ui. Distinct filenames, no contention — but if issue 22's atomic-write
  helper lands first, use it instead of a private copy (the README build-order note).
- **Node clocks.** LoRa `last_heard` ages are computed as now-minus-epoch; a node
  without time sync skews them. Gateway NTP (roadmap 12) plus issue 21's holdover fix
  bound this; until a node demonstrably syncs, prefer ages over absolutes in details.
- **Cache-serving failure mode.** A wedged watcher plus a fresh-looking UI is the worst
  outcome — hence `watcher_stale` is computed by the *UI* from file mtime/`t`, not
  self-reported by the watcher.
- **Threaded Flask readers** see old-or-new cache, never torn, because writes are
  tmp + `os.replace` on the same filesystem. Do not "optimize" into in-place writes —
  that is exactly the halow-mon defect issue 22 exists to fix.
