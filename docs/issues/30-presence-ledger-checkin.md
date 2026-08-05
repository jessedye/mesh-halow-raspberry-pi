# 30. Station presence ledger: expected-interval adherence + push check-in contract
> Tier 4 - product-path | Effort: medium | Impact: medium | Depends on: #25 (nodes.json `mac` + `expected_interval` schema); coordinate with #22 (atomic writes in halow-mon) and #18 (halow-mon per-station ladder)

## Problem

The gateway records every raw ingredient of station presence — join/leave
timestamps in `/var/lib/halow/station-events.log`, per-minute `iw station dump`
samples in `stations.jsonl`, DHCP leases in `/var/lib/misc/dnsmasq.leases` —
and computes nothing from them. There is no notion of "this node is expected to
be heard every N seconds", no missed-window detection, and no way for a node to
*tell* the gateway it is alive. On a quiet-by-design network that is a real
gap: mesh-v4's nodewatch exists precisely because "no news carries no
information at all" when quiet is the normal state
(`mesh-v4/tools/nodewatch.py:11-16`). A HaLow sensor node that died and one
behaving exactly as configured look identical to this gateway today.

The client side already settled how this must work, twice over. First, the
freshness rule: nodewatch judges a node against *its own* configured interval
at 3x, the adjacency hold-timer convention (`HOLD_MULTIPLIER = 3`,
`mesh-v4/tools/nodewatch.py:52`), and refuses to conflate "unreachable" with
"dead" — an HTTP error means alive-but-broken, an empty body means unknown,
and only total silence at every layer means down (`nodewatch.py:215-245`).
Second, the transport: on-demand polling of remote nodes failed on the bench —
`--request-telemetry` and `--request-position` both timed out at 240 s, and the
roadmap's verdict is explicit: "do not build anything on polling a remote
node. Have nodes push on a schedule instead" (`mesh-v4/ROADMAP.md:547-549`).
The gateway needs a push receiver and an adherence ledger, not another poller.

Why now: first association is imminent (blocked only on the bulk decoupling
capacitor + antenna confirmation), and the first *product* consumers of this
gateway are mains/solar HaLow sensor nodes — the trail cam profile above all.
Publishing the check-in contract now means the ESP32 client gets written
against a live, testable target instead of a guess, the same reason issue #19
publishes its UDP wire format ahead of the node client.

Scope honesty, verifier-corrected and binding: this issue serves mains/solar
HaLow nodes. It does **not** unblock asset-tag deep-sleep validation. ROADMAP
phase 15 assigns that watching to the LoRa base node ("Watch from the base
node for check-ins", `mesh-v4/ROADMAP.md:1026-1031`), the gateway has no LoRa
radio, and a ~5 mAh/day tag (`ROADMAP.md:310`: 4.97 mAh/day, ~1.7 years) will
never pay for an MM6108 association plus a TLS POST per wake. If tag check-ins
are ever to reach this gateway, a base node must push a relay report — that is
node-side work and out of scope here.

## Current state

Verified in both repos this session. No check-in or ledger machinery exists
anywhere in the gateway repo (grep for checkin/presence: only the roadmap
entry).

Gateway (`mesh-halow-raspberry-pi`):

- `scripts/halow-sta-events:5-7` — hostapd_cli action script appends
  `<date -Is> <event> <mac>` lines to `/var/lib/halow/station-events.log`.
  Join history exists; nothing consumes it beyond display.
- `ui/halow_ui.py:563-572` — `GET /api/halow/events` returns the last 100 raw
  lines of that log. No parsing, no classification.
- `scripts/halow-mon:104-133` — root, once a minute via `halow-mon.timer`
  (`systemd/halow-mon.timer`), parses `iw dev halow0 station dump` into
  `stations.jsonl`: `t`, `mac`, `signal_dbm`, `tx_mbps`, `rx_mbps`,
  `tx_packets`, `tx_retries`, `tx_failed`. It does **not** record
  `inactive time`, so the samples prove association, not recent frames. No
  battery field exists anywhere in the dump — battery data can only arrive via
  a pushed payload.
- `ui/halow_ui.py:271-279` — lease parse: `dnsmasq.leases` fields are
  `expiry mac ip host`. Expiry only; last-renewal must be approximated.
- `ui/halow_ui.py:116-137` — auth: Basic (PBKDF2) or `Bearer` where only
  `sha256(token)` is stored as `API_TOKEN_HASH` in `/etc/halow/ui.conf`;
  per-IP failure throttle at `ui/halow_ui.py:96-113`. This is the model the
  check-in token must copy.
- `scripts/deploy.sh:30-40` — ui.conf is synthesized on the PC from
  `secrets.env` values and installed **unconditionally on every deploy**
  (`deploy.sh:58`). Any credential minted on the Pi dies at the next deploy —
  this dictates where the check-in token is born.
- `scripts/deploy.sh:70` — `/var/lib/halow` is owned `halow-ui:halow-ui`; the
  UI already appends there (`THROUGHPUT_LOG`, `ui/halow_ui.py:512,544-545`),
  so the check-in receiver needs no new privilege.
- `config/nodes.json.example:3-6` — per-node entries are `name`/`url`/`token`
  only. Issue #25 adds `mac` and `expected_interval`; this issue consumes
  them (index rule: "Do not add fields piecemeal", `docs/issues/README.md:55-56`).
- `config/sudoers-halow-ui` — the UI's entire privileged surface. Nothing
  here needs to grow for this issue (see privilege model below).

Client (`mesh-v4`):

- `tools/nodewatch.py:31-37` — the state vocabulary to adapt: healthy / stale
  / wifi-down / down / unknown, each answering a different question.
- `tools/nodewatch.py:167-180` — `expected_interval()` reads the node's *own*
  applied cadence because firmware silently clamps intervals (ask for 300 s,
  get 1800 s — `ROADMAP.md:566-575`). Lesson: the configured expectation must
  match reality, and reality is confirmed at the receiver.
- `tools/nodewatch.py:294-299` — battery judged as a trend over >=5 samples,
  never a single reading.
- `tools/nodewatch.py:160-164` — state saved via tmp + `os.replace`. The
  gateway's halow-mon currently does bare `open(w)` writes
  (`scripts/halow-mon:144`), which issue #22 fixes; new state files here must
  be atomic from day one.

## Design

Three pieces. Judgment lives in root's halow-mon (it already owns the
per-minute sampling loop and can read everything locally); receipt lives in
the unprivileged Flask UI (it already owns `/var/lib/halow`); configuration
lives in `/etc/halow/nodes.json` (already read by both).

### 1. Per-node contract in nodes.json (schema from #25)

```json
{
  "nodes": [
    { "name": "trailcam1", "url": "https://192.168.50.105",
      "token": "CHANGE-ME",
      "mac": "aa:bb:cc:dd:ee:01",
      "expected_interval": 900 }
  ]
}
```

`expected_interval` is seconds between expected proofs of life (check-in,
lease renewal, or frames on air). A node without the field is *not on the
contract*: it is displayed but never alarmed. No default interval is invented
— nodewatch's 3600 s fallback (`nodewatch.py:180`) is acceptable there only
because it reads the node's own settings first; the gateway cannot, so absence
of the field means absence of judgment. The value must be what the node
*actually does*, not what was asked of it — the firmware-clamp lesson
(`ROADMAP.md:566-575`); acceptance below verifies the configured value against
observed inter-arrival times.

### 2. Push check-in: `POST /api/checkin`

Push-on-schedule is the proven pattern; the endpoint is deliberately cheap for
the node (verifier requirement): one small POST, bearer auth, 2xx fast-path,
no subprocess and no sudo in the handler.

Request:

```
POST /api/checkin HTTP/1.1
Authorization: Bearer <CHECKIN_TOKEN>
Content-Type: application/json

{"node": "trailcam1", "uptime_s": 86400, "batt_mv": 4012,
 "pos": {"lat": 45.5231, "lon": -122.6765, "alt_m": 30}}
```

- `node` required (string, <=32 chars). `uptime_s` (int), `batt_mv` (int),
  `pos` (`lat`/`lon`/`alt_m` numbers) optional. **Unknown fields are dropped,
  not stored** — a node bug can never push junk or secrets into the ledger.
- No client timestamp field, deliberately: nodes may lack a clock; receipt
  time is authoritative (receiver-side discipline).
- Body cap 2048 bytes -> 413. Every operation bounded, per repo rule.
- Response 200: `{"ok": true, "t": 1754429100, "registered": true}` —
  server epoch is a free coarse time cross-check for a clockless node (the
  gateway already serves NTP, roadmap v1 item 12). `registered: false` when
  `node` matches no nodes.json entry; the check-in is still recorded
  (evidence is evidence) and surfaces as unlisted.
- Errors: 400 (bad JSON / missing `node`), 401 (bad token — counts toward the
  existing per-IP throttle, `ui/halow_ui.py:104-109`), 413 (oversize),
  503 (`CHECKIN_TOKEN_HASH` not configured).

Auth: a **separate** check-in token, not the admin bearer. Nodes must be able
to prove liveness without holding a credential that can rewrite the SSID.
Stored as `CHECKIN_TOKEN_HASH=sha256(token)` in `/etc/halow/ui.conf`, exactly
the `API_TOKEN_HASH` model (`ui/halow_ui.py:123-128`). The admin
bearer/Basic/session also work on this endpoint (superset, for curl testing).
Because deploy.sh overwrites ui.conf on every deploy (`deploy.sh:58`), the
token originates in the PC-side repo-root `secrets.env` (gitignored) as
`CHECKIN_TOKEN=...`, hashed into ui.conf by deploy.sh alongside line 38's
`API_TOKEN_HASH`. If unset, the endpoint answers 503 and nothing else changes
— fully backward compatible. The plaintext token is never echoed, logged,
committed, or served by any API (the SAE PSK has leaked twice via "harmless"
echoes; this repo does not do harmless echoes).

Receipt record appended by the UI to `/var/lib/halow/checkins.jsonl`
(halow-ui already owns the directory, `deploy.sh:70`):

```json
{"t": 1754429100, "node": "trailcam1", "src": "10.117.0.50",
 "uptime_s": 86400, "batt_mv": 4012,
 "pos": {"lat": 45.5231, "lon": -122.6765, "alt_m": 30},
 "registered": true}
```

Ring-capped at 5760 lines (2 days at 2/min), pruned only when oversized —
the `scripts/halow-mon:136-141` pattern.

### 3. Adherence ledger in halow-mon

Each minute, after the existing sampling, halow-mon:

1. Loads nodes.json entries that carry `mac` + `expected_interval`.
2. Gathers best evidence per node, most-trusted first:
   - **checkin** — last `checkins.jsonl` record for the node (application
     proof; exact `t`).
   - **frames** — last stations.jsonl sample for the MAC, corrected by a new
     `inactive_ms` field this issue adds to the sampler (`iw` reports
     `inactive time: N ms`; last-frame time = sample `t` − `inactive_ms`/1000).
     Link-level proof: the radio heard actual frames.
   - **lease~** — DHCP renewal approximated as lease expiry minus the
     configured duration (expiry from `dnsmasq.leases` field 1 as parsed at
     `ui/halow_ui.py:277`; duration from the `dhcp-range` third field, same
     regex as `ui/halow_ui.py:326`). Marked approximate with the `~`.
   - **join** — last `AP-STA-CONNECTED` line in station-events.log.
3. Classifies with `window = 3 * expected_interval` (HOLD_MULTIPLIER,
   `nodewatch.py:52`), keeping the dead-vs-quiet split:
   - `fresh` — evidence age <= window.
   - `overdue-associated` — age > window but the MAC is in the current
     station dump: link up, application silent. Points at node software.
   - `overdue-absent` — age > window and not associated: silence at every
     layer. Points at RF or power. (Deliberately not called "down" — the
     gateway only knows its own hearing.)
   - `no-data` — no evidence ever recorded. Not an alarm; "we have not heard
     it yet" is not "it died".
   - Judgment is withheld (state held, no transition emitted) while system
     uptime < the node's window: after a gateway reboot every node would
     otherwise look overdue — the nodewatch rule that a bad answer means *we
     don't know*, applied to ourselves.
4. On state change only (deduped against the previous state), appends a
   transition to station-events.log in the established 3-field-prefix shape
   (`scripts/halow-sta-events:6`):

```
2026-08-05T15:04:01-07:00 PRESENCE-MISSED aa:bb:cc:dd:ee:01 node=trailcam1 window=2700s last=checkin+3841s associated=no
2026-08-05T15:41:02-07:00 PRESENCE-RETURNED aa:bb:cc:dd:ee:01 node=trailcam1 gap=6062s via=checkin
```

5. Writes `/var/lib/halow/presence.json` atomically (tmp + `os.replace`, the
   `nodewatch.py:160-164` / issue #22 convention), chmod 0644 so the
   unprivileged UI reads it without any new sudo grant:

```json
{
  "t": 1754430000,
  "nodes": {
    "trailcam1": {
      "mac": "aa:bb:cc:dd:ee:01",
      "expected_interval": 900, "window_s": 2700,
      "state": "fresh", "since": 1754427300,
      "last_evidence": {"t": 1754429100, "source": "checkin", "age_s": 900},
      "evidence": {"checkin": 1754429100, "frames": 1754429940,
                   "lease~": 1754425200, "join": 1754381200},
      "missed_total": 2,
      "last_checkin": {"t": 1754429100, "uptime_s": 86400, "batt_mv": 4012}
    }
  },
  "unlisted": [{"mac": "66:77:88:99:aa:bb", "seen": 1754429940},
               {"node": "mystery", "checkin": 1754428000}]
}
```

### 4. Read API + UI

`GET /api/presence` (authed, read-only): presence.json plus, per node, a
`batt_mv_trend` computed from checkins.jsonl over the last 24 h when >=5
samples exist (delta first-to-last — the `nodewatch.py:294-299` rule: a slope
means something, one low reading is noise), and the machine-readable contract
so a node developer can discover it:

```json
{"checkin_contract": {"url": "https://10.117.0.1:8443/api/checkin",
  "auth": "Bearer CHECKIN_TOKEN", "max_body_bytes": 2048,
  "fields": {"node": "required", "uptime_s": "optional",
             "batt_mv": "optional", "pos": "optional {lat,lon,alt_m}"}}}
```

UI: an Overview presence card (state chip per configured node: fresh green /
overdue-associated yellow / overdue-absent red / no-data dim, with
`last=<source>+<age>`), and a state badge in the Nodes tab next to
reachable/unreachable.

### Privilege model

| piece | runs as | why |
|---|---|---|
| `POST /api/checkin` receipt | halow-ui (unprivileged) | appends to a directory it already owns (`deploy.sh:70`); no subprocess |
| adherence pass + presence.json + events | root via halow-mon timer | already reads station dump/leases each minute; nodes.json is root-readable (640 root:halow-ui, `deploy.sh:58`) |
| `GET /api/presence` | halow-ui | reads 0644 files only |
| token mint | operator's PC (deploy.sh from secrets.env) | survives redeploys; plaintext never touches the Pi's disk or logs |

No sudoers change. No new systemd unit.

## Implementation steps

Each step is one commit. If #25 has not landed yet, land its nodes.json
schema commit (`mac` + `expected_interval`) first — do not fork the schema.

1. **deploy.sh: check-in token hash.** In `scripts/deploy.sh`, after line 38,
   derive `CHECKIN_TOKEN_HASH=$(printf '%s' "$CHECKIN_TOKEN" | sha256sum |
   cut -d" " -f1)` when `CHECKIN_TOKEN` is set in repo-root `secrets.env`
   (empty otherwise), and extend the line 39-40 `printf` to emit
   `CHECKIN_TOKEN_HASH=%s` into ui.conf. Update the header comment (lines
   4-7) naming the new optional secret. Never echo the value.
2. **halow-mon: record `inactive_ms`.** In `scripts/halow-mon`, in the
   station-dump parse (lines 109-123), add
   `elif k == "inactive time": cur["inactive_ms"] = int(v.split()[0])`.
   This turns stations.jsonl from "was associated" into "last heard a frame".
3. **halow_ui.py: `POST /api/checkin`.** Add `CHECKIN_LOG =
   "/var/lib/halow/checkins.jsonl"` beside `THROUGHPUT_LOG`
   (`ui/halow_ui.py:512`). Add a `checkin_authed` decorator: try Bearer
   against `CHECKIN_TOKEN_HASH` (same `hmac.compare_digest` shape as
   `check_auth`, lines 123-128, sharing `_auth_fail`/`_auth_ok`), fall back to
   the full `check_auth`/session path; 503 if no hash configured. Handler:
   reject `request.content_length > 2048` with 413; parse JSON; require
   `node`; copy only the whitelisted fields; append
   `{t, node, src, uptime_s, batt_mv, pos, registered}` to CHECKIN_LOG; ring
   prune past 5760+200 lines (pattern from `scripts/halow-mon:136-141`);
   return `{"ok": true, "t": now, "registered": ...}`. `registered` = node
   name present in nodes.json.
4. **halow-mon: presence pass.** New function `presence(now)` called from
   `main()` after the station sampling: load
   `/etc/halow/nodes.json`, tail checkins.jsonl, reuse the in-memory station
   entries (with `inactive_ms`), parse leases + `dhcp-range` duration, tail
   station-events.log for last CONNECTED per MAC. Classify per the design
   (window = 3x, uptime guard from `/proc/uptime`), dedupe transitions
   against the previous presence.json, append `PRESENCE-MISSED` /
   `PRESENCE-RETURNED` lines to station-events.log, increment `missed_total`,
   write presence.json via tmp + `os.replace` (use #22's helper if landed;
   inline the same two lines if not), chmod 0644.
5. **halow_ui.py: `GET /api/presence`.** Read presence.json, join per-node
   `batt_mv_trend` from checkins.jsonl (24 h window, >=5 samples), attach the
   static `checkin_contract` block. Read-only, `@authed`.
6. **UI cards.** In the `PAGE` JS of `ui/halow_ui.py`: a `presenceCard()`
   fetched in `ovw()` (beside `monCard()`, line 1183) rendering state chips;
   in `nodes()` (line 1248), a badge per node from the same fetch. Escape
   with the existing `esc()`.
7. **Docs + example config.** `config/nodes.json.example`: show
   `expected_interval` on one entry with a one-line comment in `_doc` ("what
   the node actually does, not what you asked — intervals get clamped").
   Update `docs/feature-roadmap.md` item 30 and `docs/issues/README.md`
   cross-issue notes (add 30 to the halow-mon contention list). Record the
   check-in contract JSON in this issue's Design section as the canonical
   copy for the mesh-v4 client author.

## Surface changes

| API endpoint | method | auth | change |
|---|---|---|---|
| `/api/checkin` | POST | Bearer CHECKIN_TOKEN (or any admin auth) | new — push check-in receiver, 2 KiB cap, 2xx fast-path |
| `/api/presence` | GET | admin (authed) | new — ledger states, evidence, battery trend, contract block |
| `/api/halow/events` | GET | admin | unchanged endpoint; log now also carries `PRESENCE-MISSED`/`PRESENCE-RETURNED` lines |

| file | change |
|---|---|
| `/etc/halow/ui.conf` | new optional key `CHECKIN_TOKEN_HASH` (written by deploy.sh; hash only) |
| `/etc/halow/nodes.json` | consumes #25's `mac` + `expected_interval` (this issue adds no fields) |
| `/var/lib/halow/checkins.jsonl` | new — receipt ring, 5760 lines, halow-ui-written |
| `/var/lib/halow/presence.json` | new — ledger state, root-written, atomic, 0644 |
| `/var/lib/halow/stations.jsonl` | samples gain `inactive_ms` |
| `secrets.env` (repo root, gitignored) | new optional `CHECKIN_TOKEN=` |

| surface | change |
|---|---|
| halowctl | none |
| sudoers (`config/sudoers-halow-ui`) | none — deliberately; receipt is unprivileged, judgment is already root |
| systemd units | none — rides the existing `halow-mon.timer` |
| UI | Overview presence card; Nodes-tab state badge |

## Testing & acceptance criteria

Bench culture applies: every claim below is verified by reading the receiver's
files or API responses, never by trusting the sender's 200. All operations
bounded as specified.

### Testable today (pre-association)

1. **Contract round-trip [M]:** with `CHECKIN_TOKEN` set and deployed,
   `curl -sk -H "Authorization: Bearer $CHECKIN_TOKEN" -H 'Content-Type: application/json' -d '{"node":"node1","uptime_s":5,"batt_mv":4100}' https://192.168.51.202:8443/api/checkin`
   returns `{"ok":true,...}` AND the matching line exists in
   `/var/lib/halow/checkins.jsonl` with server-assigned `t` and `src`.
   Receipt confirmed at the receiver, not inferred from the status code.
2. **Auth boundaries:** wrong bearer -> 401 and, after >3 failures from one
   IP, the existing lockout engages (`ui/halow_ui.py:107-108`). Admin
   bearer/Basic also accepted. With `CHECKIN_TOKEN_HASH` absent -> 503.
3. **Bounds:** a 3 KiB body -> 413, nothing written. 10,000 rapid check-ins
   leave checkins.jsonl at <= ~5960 lines (ring prune observed, file size
   bounded). Unknown JSON fields do not appear in the stored line.
4. **Secrets stay secret:** after deploy + a day of check-ins,
   `grep -r "$CHECKIN_TOKEN" /var/lib/halow /etc/halow/nodes.json` on the Pi
   finds nothing (only the hash in ui.conf); `journalctl -u halow-ui` contains
   no token material; `/api/presence` response contains no token. Redeploy
   (`./scripts/deploy.sh`) and confirm the token still authenticates —
   proves the mint survives the unconditional ui.conf install (`deploy.sh:58`).
5. **Ledger state machine [M], synthetic:** give node1 `expected_interval: 60`
   in `/etc/halow/nodes.json`, POST one check-in, run `sudo halow-mon`
   manually: presence.json shows `fresh` with `last_evidence.source:
   "checkin"`. Stop POSTing; after >180 s of manual runs the state is
   `overdue-absent`, exactly one `PRESENCE-MISSED` line was appended
   (dedupe verified across repeated runs), `missed_total` incremented once.
   POST again -> `fresh`, one `PRESENCE-RETURNED` with a plausible `gap=`.
6. **Reboot guard:** reboot the Pi (or fake `/proc/uptime` in a test run);
   for the first `window` seconds no MISSED transition is emitted for any
   node.
7. **No-contract nodes:** a nodes.json entry without `expected_interval`
   never appears in a MISSED event and shows no window in `/api/presence`.
8. **Unregistered pushes:** `{"node":"mystery"}` -> 200 with
   `"registered": false`, recorded, listed under `unlisted`.
9. **UI render:** Overview card shows the synthetic node's chip cycling
   green -> red -> green through test 5; no layout break with zero configured
   nodes.

### Needs a joined station

10. **Evidence sources go real [M]:** with a Heltec V4.2 associated, the
    node's `evidence` block fills from actual frames (`frames` timestamp
    within ~60 s + inactive time of a live station) and `lease~` from its
    real lease. Confirm `inactive_ms` appears in stations.jsonl for the
    station's MAC.
11. **Dead-vs-quiet split observed [M]:** with the node associated but its
    check-in application halted, the state lands `overdue-associated`;
    power the node off, it lands `overdue-absent`. Two different states for
    two different repair jobs — the classification is the deliverable.
12. **Cadence honesty [M]:** after >=24 h of a node POSTing on schedule over
    the HaLow path (node -> `https://10.117.0.1:8443/api/checkin`), the
    observed inter-arrival p50 in checkins.jsonl is within 20% of the
    configured `expected_interval`, or the config is corrected to what the
    node actually does (the firmware-clamp lesson, `ROADMAP.md:566-575`).
    Mark the interval `[M]` in nodes.json's `_doc` only after this check.
13. **Battery trend [M]:** >=5 real check-ins yield a `batt_mv_trend` in
    `/api/presence` consistent with the node's own `/api/power` reading —
    confirm at the receiver against a second source.

## Out of scope

- **Asset-tag deep-sleep validation.** Stated once more because it will be
  asked: the watcher for sleeping LoRa tags is the LoRa base node
  (`mesh-v4/ROADMAP.md:1026-1031`); this gateway has no LoRa radio, and an
  association + TLS POST per wake does not fit a ~5 mAh/day budget. A
  base-node relay report (base node POSTs on behalf of tags it heard) would
  slot into this same `/api/checkin` contract later — but that is mesh-v4
  work, not this issue.
- **The mesh-v4 client.** The ESP32-side scheduled POST is node firmware
  work; this issue publishes the contract it codes against.
- **Alert delivery.** Missed windows land in station-events, presence.json,
  and the UI. Email/webhook/MQTT fan-out is a separate decision.
- **nodes.json schema changes.** `mac` + `expected_interval` belong to #25;
  this issue only reads them.
- **Fleet-watcher reachability probing.** Active HTTP probing of nodes is
  #25's cached watcher. This ledger is strictly passive plus push — it never
  polls a node (that is the point).
- **Trail-cam image ingest (#29).** An image POST is incidentally presence
  evidence; wiring #29's sink into this ledger is a one-line follow-up there,
  not here.

## Risks & gotchas

- **deploy.sh clobbers ui.conf every deploy** (`scripts/deploy.sh:58`,
  unconditional install). This is why the token is minted from PC-side
  secrets.env and not on the Pi. Any reviewer suggestion to "just add a
  halowctl checkin-token" reintroduces a credential that silently dies on
  redeploy — decline it.
- **The PSK has leaked twice via "harmless" echoes.** The check-in token is a
  new secret traveling a new path. Hash-only at rest, no echo in deploy
  output, `$CHECKIN_TOKEN` env-var form in every documented curl, and the
  field whitelist keeps node-side mistakes out of a world-readable ledger.
- **halow-mon is contended.** Issues 18, 20, 22, 24 also modify
  `scripts/halow-mon` (`docs/issues/README.md:52-54`). Land #22's atomic-write
  helper first if at all possible; regardless, presence.json must be
  tmp+replace from its first commit — it is read every UI refresh and a
  brownout mid-write must not blind the ledger (this bench browned out two
  boards).
- **Timer-fired oneshot.** halow-mon runs once a minute (`halow-mon.timer`);
  a node with `expected_interval: 60` has a 180 s window judged at 60 s
  granularity — transitions can lag up to a minute. Fine for 15-minute trail
  cams; document, don't fix.
- **Intervals lie unless measured.** The mesh-v4 bench asked for 60 s and got
  1800 s, silently (`ROADMAP.md:566-575`). Treat the configured
  `expected_interval` as `[C]` until acceptance test 12 upgrades it to `[M]`;
  a wrong interval makes the ledger cry wolf or sleep through a death —
  both worse than no ledger.
- **`lease~` is an approximation** (expiry minus configured duration) and
  breaks if the operator changes the lease time between renewals. It is the
  weakest evidence tier on purpose; never let it be the sole basis of a
  RETURNED transition when a checkin/frames source disagrees.
- **Self-signed TLS on 8443.** The cert's SAN already covers 10.117.0.1
  (`scripts/deploy.sh:74-77`), so the node client can pin by fingerprint or
  skip verification — its choice, out of scope, but the contract must not
  assume a public CA.
- **ESP32 TLS sessions are scarce** (~6 concurrent, per #20/#25 analysis). One
  short-lived POST per interval is affordable for mains/solar nodes; do not
  extend this contract toward chatty streaming without revisiting that budget.
- **Overdue is not down.** The nodewatch lesson that an HTTP error means
  "alive but broken" (`nodewatch.py:215-233`) has a gateway mirror: the ledger
  reports what the gateway heard, not what the node is. The two overdue
  flavors keep that honest — resist collapsing them into one red light.
