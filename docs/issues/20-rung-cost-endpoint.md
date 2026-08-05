# 20. Measured TRANSPORT_HALOW rung-cost endpoint with vip.py-style hysteresis

> Tier 1 - first-association | Effort: medium | Impact: high | Depends on: #22 (both edit halow-mon's data files — land atomic writes first or together)

## Problem

The mesh-v4 nodes pick a transport per destination by comparing one integer
per rung — administrative distance, lower wins. Today the HaLow rung's
integer is `metric_halow = 7`, a hand-picked ordinal sitting between
wifi=5 and espnow=10 (mesh-v4 patch 0004, config field at line 2126,
default in `transportMetricConfigured()` at 5699). Seven is a guess. The
project rule it violates is written down in mesh-v4
`docs/transport-ladder-halow.md:43-49`: ordinals of 5/10/100 stand against
measured rates spanning 66x to 975x, "32.5 Mbps is a **vendor claim**",
and the conclusion is explicit — *"do not hand-pick an ordinal at all. A
cost derived from measured rate and delivery orders the rungs without
anyone deciding whether HaLow is 7 or 20."*

The gateway already collects everything needed to compute that cost.
Roadmap v1 item 2 deliberately stopped at telemetry: halow-mon samples
per-station signal/rate/retry counters every minute, and
`GET /api/halow/link` serves aggregates whose docstring says it is
"shaped for deriving a TRANSPORT_HALOW rung cost"
(ui/halow_ui.py:746-747). The consumer was left unbuilt. Verified absent
this session: no `rungcost` string exists anywhere in this repo.

Why now: first association is imminent (blocked only on a decoupling
capacitor + antenna confirmation), and the node-side ladder scaffolding is
already flashed — `TRANSPORT_HALOW`, `halowRungHealthy()` (patch 0004:5658),
the +200 not-healthy penalty (patch 0004:5728-5730), and a periodic
joinability check `halowPeriodicCheck()` (patch 0004:5667) that runs every
`halow_check_min` minutes (default 30, floor 5). The moment a node
associates, the ladder starts ranking a 32-Mbps-class link with a guessed
7 against measured rungs. Publishing a measured cost before that moment
means the guess never has to be trusted.

One thing the existing link API cannot provide, which is the genuinely new
work: its `delivery_pct` is computed from the **last sample's cumulative**
`iw` counters (ui/halow_ui.py:766-781 — `tx_packets`/`tx_failed` since
association). A station that had one bad hour yesterday and is perfect now
carries yesterday forever. A rung cost needs **windowed** deltas between
consecutive samples, plus damping so the published number never flaps —
mesh-v4 proved undamped failover unusable and settled on
demote-after-2/promote-after-3 (`tools/vip.py:47-48`).

## Current state

Verified this session, both repos.

**This repo (gateway):**

- `scripts/halow-mon` (root, oneshot, fired by `halow-mon.timer` every 60 s
  — `OnUnitActiveSec=60`): parses `iw dev halow0 station dump` and appends
  one JSON line per associated MAC to `/var/lib/halow/stations.jsonl`
  (lines 104-133). Fields captured per sample: `t`, `mac`, `signal_dbm`,
  `tx_mbps`, `rx_mbps`, and the cumulative counters `tx_packets`,
  `tx_retries`, `tx_failed` (lines 116-123). File is ring-pruned
  (127-131) and chmod 0o644 (133) so the unprivileged UI can read it.
  All of its writes are in-place `open(w)` — the defect #22 fixes.
- `ui/halow_ui.py:742-786` — `GET /api/halow/link[/<mac>]` reads
  stations.jsonl (capped at the last 720 lines, :760), groups by MAC, and
  serves min/avg/max `tx_mbps` and `signal_dbm` over the retained samples
  plus `delivery_pct`/`retry_pct` from the **last sample's lifetime
  counters** (:766-781). No windowing, no health verdict, no damping, no
  cost. Nothing else in the repo computes counter deltas.
- Privilege split: `halow-ui.service` runs `User=halow-ui`; every
  privileged action goes through the sudoers whitelist
  (`config/sudoers-halow-ui`). halow-mon runs as root. `/var/lib/halow/*`
  data files are world-readable by design (halow-mon chmods 644), so the
  UI reads them without sudo — the pattern this feature reuses.
- UI Overview renders a stations table (mac/signal/tx/rx/connected) from
  `/api/halow` at ui/halow_ui.py:1203-1219.

**mesh-v4 (the consumer's side — context only, not edited here):**

- Patch 0004 (`0004-authenticated-admin-api.patch`): `TRANSPORT_HALOW`
  define (:254); settings fields `halow_enabled`, `metric_halow` (1-255,
  "administrative distance for the HaLow rung; between wifi and espnow"),
  `halow_check_min` (floor 5, max 1440) at :2124-2127;
  `transportMetricConfigured()` defaults `mtrhalow` to 7 (:5699);
  `transportMetric()` adds +200 when `!halowRungHealthy()` (:5728-5730),
  pricing the rung below LoRa until the driver reports a usable link;
  `halowPeriodicCheck()` (:5667-5676) runs at `halowCheckIntervalMin()`
  cadence (default 30 min, :5617-5622) and is documented "reads driver
  state and never touches RF".
- `tools/vip.py:47-48`: `DEMOTE_AFTER = 2`, `PROMOTE_AFTER = 3`;
  `evaluate()` (:189-227) — success resets fails, failure resets oks,
  transitions only on the streak thresholds. Its `save()` (:176-180) is
  the tmp+`os.replace` atomic-write pattern.
- `docs/transport-ladder-halow.md`: measured anchors — LoRa best
  9.0 kbps [M] (:20-21), ESP-NOW healthy 604 kbps [M] (:41); the
  "never weight a rung on a datasheet number" rule (:43-54); node heap
  budget 67-69 KB free internal, one TLS session ~37 KB [M] (:222-223).
- Patch 0004 ESP-NOW health precedent: `ESPNOW_FAIL_PCT_BAD 30` — tx
  failures over 30% of attempts = unhealthy (:5466); demotion immediate,
  recovery damped, "hysteresis matters more than the thresholds" (:5461-5465).

## Design

**Shape:** halow-mon computes; the UI serves. One new pure-logic module,
one new state file, one new read-only endpoint.

Windows and hysteresis must advance at wall-clock cadence, not request
cadence — computing them inside a Flask handler would make the published
number depend on who polls and lose the streak counters on service
restart. halow-mon already runs once a minute as root and already holds
the parsed station dump for that minute, so it evaluates one window per
run and persists the verdict. The endpoint is then a stateless authed
read of a 644 file: **no sudoers change, no new privilege.**

### Window evaluation (per MAC, per halow-mon run)

Deltas against the previous run's stored counters:

- `d_pkts = tx_packets - prev.tx_packets`, same for `tx_retries`,
  `tx_failed`.
- Any delta negative → the station reassociated (counters reset since
  association) → verdict **reset**: discard the window, store the new
  baseline. Not evidence of good or bad.
- First sighting of a MAC → verdict **baseline** (no prev to delta).
- `tx_failed` absent from either sample → verdict **no-counters**:
  delivery cannot be computed; never fake 100%.
- `d_pkts == 0` → verdict **quiet**: an idle station is not a broken one
  (mesh-v4's dead-vs-quiet discipline; ESP-NOW health likewise only
  counts silence "once we have tried to use it", patch 0004:5455-5459).
- MAC present in the previous state but absent from this run's dump →
  verdict **absent**: not associated is not quiet — the rung is down for
  that node. Counts as a bad window.
- Otherwise: `delivery_w = 100*(d_pkts - d_fail)/d_pkts`,
  `retry_w = 100*d_retr/d_pkts`, `goodput_kbps = tx_mbps * 1000 *
  delivery_w/100` (current sample's `tx_mbps` — minstrel's chosen rate,
  delivery-weighted). Verdict **good** if `delivery_w >= 70`, else
  **bad** — the 70% line is the complement of mesh-v4's measured-in-anger
  `ESPNOW_FAIL_PCT_BAD 30`, not a new invention.

`good`/`bad`/`absent` drive the hysteresis; `quiet`/`reset`/`baseline`/
`no-counters` leave the streaks untouched (evidence merely ages).

### Hysteresis (vip.py constants, vip.py semantics)

`DEMOTE_AFTER = 2` consecutive bad windows → `healthy = false`.
`PROMOTE_AFTER = 3` consecutive good windows → `healthy = true`.
A good window zeroes `fails`; a bad one zeroes `oks` (vip.py
`evaluate()` :193-207). Initial state for a new MAC is `healthy: false`
— a rung earns its way up with 3 good windows (~3 minutes), it is never
granted health on sight. At one window per minute, worst-case publication
lag is 2 min to demote, 3 min to promote; the node reads at
`halow_check_min` cadence anyway (30 min default).

### Cost formula

`goodput_kbps` is EWMA-smoothed across determinate (good/bad) windows,
`ewma = 0.7*ewma + 0.3*window`, frozen during indeterminate ones. Cost is
a straight line in log-rate space through the ladder's two **measured**
anchors — LoRa best 9.0 kbps [M] → 100, ESP-NOW healthy 604 kbps [M] → 10
(slope 90/log10(604/9) = 49.3 per decade):

```
cost = clamp( round( 100 - 49.3 * log10(goodput_kbps_ewma / 9.0) ), 6, 99 )
```

| windowed goodput | cost | sits |
|---|---|---|
| >= ~730 kbps | 6 | between wifi 5 and espnow 10 — the healthy-HaLow band |
| 604 kbps | 10 | ties ESP-NOW exactly at ESP-NOW's measured rate |
| 333 kbps (MCS0@1MHz PHY, [C]) | 23 | below espnow, far above lora |
| 100 kbps | 48 | |
| 20 kbps | 83 | |
| <= ~9.4 kbps | 99 | still one under lora=100 |

Properties, all deliberate: the floor 6 means a healthy HaLow **never
outranks healthy WiFi (5)** — same intent as the current default 7; cost
crosses ESP-NOW's 10 exactly where measured goodput crosses ESP-NOW's
measured rate, which is the definition of "the measurement orders the
rungs"; the ceiling 99 means an associated, delivering, however-slow
HaLow still beats LoRa (100) — even MCS0@1MHz's claimed 333 kbps is ~37x
LoRa's measured best. When `healthy` is false, `cost` is `null`: the
gateway does not fabricate a number for a link it cannot currently
measure (node-side, unhealthy is already priced by its own +200).

Honesty note for the docstring: `tx_mbps * delivery` is derived from [M]
iw counters (MAC-layer acks are receiver-confirmed per frame), but it is
airtime-ordering evidence, not iperf throughput. Roadmap items 1/19
measure throughput; this endpoint only has to order rungs. Acceptance
below cross-checks the ordering against iperf3.

### State file — `/var/lib/halow/rungcost-state.json` (0644, root-written)

```json
{
  "v": 1,
  "t": 1754410800,
  "stations": {
    "3c:71:bf:aa:bb:cc": {
      "last": {"t": 1754410800, "tx_packets": 51234, "tx_retries": 2101,
               "tx_failed": 12, "tx_mbps": 13.0, "signal_dbm": -58},
      "healthy": true, "cost": 6,
      "goodput_kbps_ewma": 11207.4,
      "oks": 3, "fails": 0,
      "window": {"verdict": "good", "delivery_pct": 98.7, "retry_pct": 4.1,
                 "tx_mbps": 13.0, "goodput_kbps": 12831, "n_pkts": 152},
      "since": 1754408300, "last_seen": 1754410800,
      "windows": {"good": 41, "bad": 2, "quiet": 7, "reset": 1,
                  "absent": 0, "no_counters": 0}
    }
  }
}
```

Written atomically (tmp + `os.replace`, the vip.py `save()` pattern) from
day one — this file feeds a routing decision and must never be readable
half-written after a brownout, which is exactly the #22 failure mode.
Bounded: MACs unseen for 2 days are evicted (matches the stations.jsonl
ring horizon), map capped at 64 MACs, oldest-`last_seen` dropped first.
MAC keys normalized to lower case on store and lookup. Contains MACs and
rates only — no SSID, no PSK, nothing from halow.env.

### Endpoint — `GET /api/halow/rungcost` and `/api/halow/rungcost/<mac>`

Authed (same `@authed` as every API route), reads the state file, serves:

```json
{
  "mac": "3c:71:bf:aa:bb:cc",
  "healthy": true,
  "cost": 6,
  "stale": false,
  "age_s": 22,
  "goodput_kbps_ewma": 11207,
  "window": {"verdict": "good", "delivery_pct": 98.7, "retry_pct": 4.1,
             "tx_mbps": 13.0, "goodput_kbps": 12831, "n_pkts": 152},
  "streaks": {"oks": 3, "fails": 0},
  "windows": {"good": 41, "bad": 2, "quiet": 7, "reset": 1,
              "absent": 0, "no_counters": 0},
  "source": "windowed iw counter deltas, AP-side, MAC-ack confirmed"
}
```

Staleness is fail-safe: if the state's `t` is older than 180 s (three
missed monitor runs), the endpoint serves `stale: true`, forces
`healthy: false`, `cost: null`, and adds `"reason": "monitor stale"` — a
gateway whose monitor died must not keep advertising a healthy rung.
Unknown MAC returns the link API's shape (ui/halow_ui.py:784-785):
`{"error": "no rungcost state for <mac>", "stations": [...]}`. Bare
`/api/halow/rungcost` returns `{"t":..., "stale":..., "stations": {mac:
{...}}}`. A single-MAC response is a few hundred bytes: sized for the
node's one affordable TLS session (~37 KB of 67-69 KB free [M],
transport-ladder-halow.md:222-223) at `halow_check_min` cadence.

**Consumer contract (documented, not implemented here):** a node fetches
its own MAC's object once per `halowPeriodicCheck` interval, and on
`healthy && !stale` may write `cost` into `metric_halow` via its own
settings API. `halowPeriodicCheck()` itself is documented
never-touches-RF and cheap-by-design; adding a network fetch to it is a
mesh-v4 decision with its own heap/TLS budget — out of scope here.
Gateway `healthy` is AP-side evidence about that MAC; the node ANDs it
with its local `halowRungHealthy()` — the two can legitimately disagree
(e.g. AP-side counters fine, node driver wedged).

### Privilege model

| piece | runs as | why |
|---|---|---|
| window/hysteresis/cost computation | root (inside halow-mon) | already runs `iw dev halow0 station dump` each minute; owns the state files |
| `/api/halow/rungcost` | halow-ui (unprivileged) | pure read of a 0644 file — same pattern as `/api/halow/link` |
| sudoers | **unchanged** | nothing new is executed as root on the UI's behalf |

## Implementation steps

1. **`scripts/halow_rungcost.py` — pure logic, no I/O.** Functions:
   `evaluate_window(prev, cur) -> (verdict, window_dict)` implementing the
   delta/reset/quiet/no-counters rules above; `step(state_entry, verdict,
   window) -> state_entry` implementing vip.py hysteresis (constants
   `DEMOTE_AFTER = 2`, `PROMOTE_AFTER = 3` with a comment citing
   mesh-v4 `tools/vip.py:47-48`) and the EWMA (`EWMA_ALPHA = 0.3`);
   `cost_from_goodput(kbps) -> int|None` with constants
   `ANCHOR_LORA_KBPS = 9.0`, `ANCHOR_ESPNOW_KBPS = 604.0`,
   `COST_MIN = 6`, `COST_MAX = 99`, `BAD_DELIVERY_PCT = 70`, each with a
   one-line evidence citation. Add `--selftest`: scripted sample
   sequences (baseline → 3 good → healthy; 2 bad → unhealthy; counter
   reset mid-stream discarded; quiet windows freeze streaks; absent
   counts bad; missing tx_failed never yields a cost) asserting expected
   `healthy`/`cost` at each step, exit non-zero on mismatch. Commit.
2. **Wire into `scripts/halow-mon`.** After the stations.jsonl block
   (line ~133), the parsed `entries` list is already in hand: load
   `/var/lib/halow/rungcost-state.json` (missing/corrupt → fresh
   `{"v":1, "stations":{}}` — corrupt must not wedge the monitor, #22's
   lesson), run every known+seen MAC through
   `halow_rungcost.evaluate_window`/`step` (MACs in state but not in
   `entries` get verdict `absent`), evict >2-day MACs, cap at 64, set
   top-level `t`, write tmp + `os.replace` + chmod 0644. Import via
   `sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))`
   since both land in `/usr/local/bin`. This block must also run when
   `dump` is empty (all stations absent is a result, not a skip). Commit.
3. **Endpoint in `ui/halow_ui.py`.** Add `api_halow_rungcost(mac=None)`
   beside `api_halow_link` (after :786) with both routes, `@authed`,
   reading the state file, applying the 180 s staleness rule, lower-casing
   the `<mac>` lookup, serving the shapes above. Docstring carries the
   cost formula, the two [M] anchors, and the consumer contract — it is
   the node implementer's reference. Commit.
4. **Deploy.** `scripts/deploy.sh`: add
   `sudo install -m755 scripts/halow_rungcost.py /usr/local/bin/` next to
   the halow-mon install (line 68). No unit changes — the timer already
   fires the code path. Commit.
5. **UI surface.** Overview stations table (ui/halow_ui.py:1203-1219):
   fetch `/api/halow/rungcost` alongside `/api/halow` and add two columns
   — `cost` and a healthy/unhealthy/stale badge — dashes pre-association.
   Commit.
6. **Docs.** `docs/feature-roadmap.md` item 20: mark DONE in the
   repo's parenthetical style, pointing at this file for the contract.
   Commit.

## Surface changes

| API endpoint | method | new/changed | notes |
|---|---|---|---|
| `/api/halow/rungcost` | GET | new | all stations; authed; read-only |
| `/api/halow/rungcost/<mac>` | GET | new | single MAC, the node-consumer shape; authed; read-only |
| `/api/halow/link[/<mac>]` | GET | unchanged | stays lifetime-cumulative; documented as such |

| halowctl command | change |
|---|---|
| (none) | state file is world-readable; `curl` + the endpoint cover CLI use |

| UI element | change |
|---|---|
| Overview stations table | + `cost` column, + healthy/stale badge |

| systemd unit | change |
|---|---|
| halow-mon.service / .timer | none (script gains a step inside the existing 1/min run) |

| file | change |
|---|---|
| `scripts/halow_rungcost.py` | new — pure logic + `--selftest` |
| `scripts/halow-mon` | edited — window step + atomic state write |
| `/var/lib/halow/rungcost-state.json` | new runtime state, 0644, atomic writes, bounded (64 MACs, 2-day eviction) |
| `config/sudoers-halow-ui` | **unchanged** — call this out in review |

## Testing & acceptance criteria

### Testable today (pre-association)

1. `python3 scripts/halow_rungcost.py --selftest` exits 0; every scripted
   transition asserts: healthy only after 3 consecutive good windows,
   unhealthy after 2 consecutive bad, counter-reset windows discarded,
   quiet windows change nothing, absent counts bad, missing `tx_failed`
   never produces a cost.
2. Formula anchors, exact: `cost_from_goodput(604.0) == 10`,
   `cost_from_goodput(9.0) == 99` (clamped), `cost_from_goodput(20000)
   == 6`, monotonic non-increasing across 9→20000 kbps.
3. `sudo /usr/local/bin/halow-mon` with zero stations exits 0 and writes
   a valid (possibly empty-`stations`) state file; a hand-corrupted state
   file is replaced, not crashed on, and the heal/sample path still runs.
4. Endpoint, against a hand-seeded state file: authed request returns
   the documented shape; no auth → 401; unknown MAC → error body listing
   known stations; state `t` aged >180 s (or `halow-mon.timer` stopped)
   → `stale: true`, `healthy: false`, `cost: null`.
5. Atomicity, measured and bounded: loop 20x `sudo halow-mon &` +
   `kill -9` at random 0-500 ms offsets; after every iteration
   `json.load` of the state file succeeds. No torn file in 20 kills.
6. Response size: single-MAC body under 1 KB (`curl -sw '%{size_download}'`)
   — the node TLS-session budget check.

### Needs a joined station

7. First real windows [M]: within 3 minutes of a Heltec V4.2 node
   associating, its MAC appears with determinate windows and, after 3
   good ones, `healthy: true` and an integer cost in [6, 99]. Record the
   first real cost in the roadmap entry the way first contact was recorded.
8. Forced-bad, receiver-side, bounded: with the station associated and
   passing traffic, detune/remove the **station** antenna (or walk it to
   RF margin) for ~5 min. `delivery_pct` collapses in the windows;
   `healthy` flips false after exactly 2 consecutive bad windows (verify
   from state-file timestamps, not eyeballs). Restore; `healthy` returns
   after exactly 3 good windows. No intermediate flap in between — grep
   the state history for a single transition each way.
9. Reassociation: reboot the node; the reset window is logged as
   `reset`, streaks survive uncorrupted, no spurious demotion from the
   negative delta.
10. Dead-vs-quiet: leave the joined node idle ≥10 min — windows go
    `quiet`, `healthy` and `cost` hold. Silence is not demotion.
11. Ordering cross-check against a real measurement (the acceptance test
    that the number orders rungs): run the item-1 iperf3 harness under
    max-rate and long-range profiles; the published costs must rank the
    two profiles in the same order as measured iperf3 throughput, and
    both must be marked [M] in the record.

## Out of scope

- **The node-side consumer.** `halowPeriodicCheck()` is documented cheap
  and never-touches-RF (patch 0004:5663-5667); adding an HTTPS fetch to it
  is a mesh-v4 change with its own heap/TLS budget and belongs in that
  repo. This issue publishes and documents the contract; the node decides.
- Replacing node-local `halowRungHealthy()` or the +200 penalty — the
  gateway's healthy is complementary AP-side evidence, not a substitute.
- Touching `metric_wifi`/`metric_espnow`/`metric_lora`, or auto-writing
  `metric_halow` into nodes via the node proxy.
- Actual throughput measurement — that is items 1 (iperf3 harness, built)
  and 19 (ESP32-class UDP tester). This endpoint only orders rungs.
- Changing `/api/halow/link` semantics; its lifetime aggregates stay as
  the long-horizon view.
- A windows-history endpoint — stations.jsonl + `/api/halow/link` already
  carry history.

## Risks & gotchas

- **#22 collision.** Both issues edit halow-mon's file writing. This
  issue writes its new file atomically from day one (vip.py `save()`
  pattern) so #22 never has to chase it, but merge order must be agreed —
  land #22 first or together to avoid conflicting halow-mon diffs.
- **`tx failed` may not exist on this driver.** halow-mon only stores
  keys the morse driver's `iw` actually prints (scripts/halow-mon:122-123);
  whether MM6108+morse reports `tx failed` is unverified until a station
  associates. If absent, every window is `no-counters`, health is never
  earned, and the endpoint honestly serves nothing — by design. Check the
  first real station dump; if the counter is missing, that is a finding to
  escalate, not a reason to fake delivery from `tx retries`.
- **Sender-side counter lies.** MAC-layer acks make `tx_packets -
  tx_failed` receiver-confirmed per frame, but mesh-v4 has already seen a
  sender report acked=1000/failed=0 while 424/1000 arrived (ESP-NOW rate
  harness). The same class of driver lie here would poison the cost
  invisibly — which is why acceptance 11 cross-checks ordering against
  iperf3 before the number is trusted.
- **`tx_mbps` is minstrel's instantaneous rate choice**, not goodput; the
  delivery weighting and EWMA temper it, and the log mapping compresses
  residual jitter. If the bench still shows the integer flapping at a
  boundary (6↔7), add a ±1 publish deadband then — not speculatively now.
- **Brownout mid-write** is this bench's signature failure (two boards
  browned out; #22 exists because mon-state.json can wedge the healer).
  Atomic replace plus corrupt-file-means-fresh-start keeps the rung cost
  from becoming its own outage.
- **NTP steps.** chrony can step time early in boot; age math
  (`now - t`) must clamp at 0 and staleness must key off the state's own
  `t`, or a backwards step fabricates staleness.
- **Secrets discipline.** Nothing in this path touches halow.env, and the
  response must stay MACs+rates only. The SAE PSK has leaked twice via
  "harmless" echoes; review the endpoint against that history even though
  it never handles a secret.
- **Cadence mismatch is fine, but say it.** Demotion publishes in ≤2 min;
  a node polling at the 30-min default can act on a number up to 30 min
  old. That is the node's `halow_check_min` knob (floor 5), not a gateway
  defect — the contract documents it so nobody "fixes" it with an
  unbounded push channel.
- **Case-sensitivity trap.** The link API does a two-way
  `mac.lower()/mac.upper()` fallback (ui/halow_ui.py:784); rungcost
  normalizes to lower everywhere instead. Do not copy the fallback.
