# 21. Time holdover: chrony local-stratum fallback + time-validity status
> Tier 2 - reliability | Effort: small | Impact: medium | Depends on: none

## Problem

Roadmap v1 item 12 shipped "NTP for the HaLow net" and was marked DONE with a
measured result: "synced stratum 3" (docs/feature-roadmap.md:76-77). That [M]
was taken with the upstream healthy. The unsynchronized path was never drilled,
and it is broken: `config/chrony-halow.conf` is two `allow` lines and a comment
— no `local` directive — and Debian's base chrony.conf has none either. Without
`local`, chronyd will not hand out usable time while it is itself
unsynchronized. The gateway serves NTP only when it least matters.

Why it matters now: the Pi 4 has no RTC, and neither do the ESP32 nodes. The
field failure is one power-cycle away: gateway loses power with the upstream
LAN down (or the site has no upstream at all), reboots near epoch (bounded only
by fake-hwclock's last save), chrony finds no source, and every NTP query from
the HaLow subnet gets nothing a client will accept. The nodes then run with no
valid time indefinitely.

The client-side evidence is what upgrades this from cosmetic to a defect. The
mesh-v4 admin API documents (firmware/ADMIN-API.md:149-151) that GPS-log
pruning "needs a valid clock... nothing is deleted until the time is real — the
ring can exceed the retention window on a node that has never had time sync."
Safe direction for one node-day; silent flash growth for a deployment. The
bitter detail: config/chrony-halow.conf:2-3 cites this exact bench lesson
("the bench skips pruning while the clock is unset") as the reason the file
exists — the config's own comment describes the failure its behavior permits.

The fix is small: add `local stratum 10` so chrony serves its own clock when no
source is selectable, verify the two things that make holdover time
*approximately right* rather than 1970 (driftfile, fake-hwclock), and expose
time validity in `/api/system` plus an Overview badge — because a gateway
silently serving stale time is the next trap, and the console currently cannot
show it.

## Current state

Verified in this session (line numbers re-derived, not inherited from the
audit):

**Gateway repo** (`mesh-halow-raspberry-pi`):

- `config/chrony-halow.conf:1-5` — the entire file is a 3-line comment plus
  `allow 10.117.0.0/24` (line 4) and `allow 10.42.0.0/24` (line 5). No
  `local`, no `makestep`, no `driftfile`. It is a conf.d drop-in over the
  Debian base `/etc/chrony/chrony.conf` (Pi OS Lite 64-bit Trixie per
  README.md), which ships `driftfile`, `makestep 1 3`, a Debian pool, and no
  `local` directive [C — Debian default; confirm on the Pi, step 1 below].
- `scripts/deploy.sh:24` — apt-installs chrony. `deploy.sh:62` — installs the
  drop-in to `/etc/chrony/conf.d/halow.conf`. `deploy.sh:80-85` — restarts
  halow-net, halow-ui, dnsmasq, halow-ap; **chrony is never restarted**, so a
  redeployed chrony config does not take effect until an unrelated reboot.
  That is a second (smaller) verified gap this issue closes.
- `deploy.sh:63` — precedent for sed-editing a base config in place (avahi
  `host-name`); relevant as a contingency, see Risks.
- `config/dnsmasq-halow.conf:10` — DHCP already advertises
  `dhcp-option=option:ntp-server,10.117.0.1`; `scripts/halowctl:303` preserves
  the option when `dhcp-config` rewrites the file. The DHCP side is done.
- `ui/halow_ui.py:918-933` — `/api/system` returns uptime, load, mem, temp,
  kernel, disk. No time field of any kind.
- `ui/halow_ui.py:1172-1183` — `ovw()` renders the Overview grid of six
  `.stat` tiles from `/api/system` + `/api/halow`; `monCard()` (1184-1196)
  renders 24 h history from `/api/metrics`.
- `ui/halow_ui.py:789-827` — `/api/metrics` computes `uptime_pct` over the
  hard-coded keys `("ap", "dnsmasq", "upstream")` at lines 821-824.
- `scripts/halow-mon:88-100` — the per-minute root sampler writes
  `metrics.jsonl`; no time-sync field. `halow-mon:73-84` — after 5 samples of
  upstream-unreachable it bounces eth0 (30-min cooldown); this interacts with
  the failure drill (see Risks).
- `config/sudoers-halow-ui:4-21` — the UI's entire root surface; nothing
  chrony-related, and this issue adds nothing to it (see privilege model).
- `scripts/verify.sh:7-15` — the `chk` PASS/FAIL helper; no chrony checks
  exist today.

**Client repo** (`mesh-v4`):

- `firmware/ADMIN-API.md:149-151` — pruning is a no-op until the node clock is
  real; the GPS ring (8640 fixes, ADMIN-API.md:153) can exceed retention
  forever on a node that never syncs.
- `firmware/snapshots/tree/src/mesh/wifi/WiFiAPClient.cpp:47` —
  `NTPClient timeClient(ntpUDP, config.network.ntp_server);` and lines
  275-287: the node queries `config.network.ntp_server` every 12 h and sets
  `RTCQualityNTP` on success. **Meshtastic ignores DHCP option 42.** The
  gateway's dnsmasq option is necessary groundwork but not sufficient; the
  node's `network.ntp_server` must be pointed at `10.117.0.1` (node-side
  config, out of scope here, flagged for coordination).

Spec-vs-code note: the audit brief suggested `makestep` tuning "so recovery
from epoch is a step not a slew". The code and chrony semantics say the Debian
default `makestep 1 3` already covers the realistic paths: the 3-update budget
is consumed only by actual clock updates, and an epoch-magnitude offset can
only exist *before* the first update (no source reachable yet). When the source
finally appears, update #1 sees the huge offset and steps. A synced chronyd
cannot later develop a multi-year offset. So no makestep change ships by
default; drill T5 verifies the claim and Risks documents the contingency.

## Design

Three pieces, smallest possible surface:

**1. chrony holdover (`config/chrony-halow.conf`).** Add one directive:

```
local stratum 10
```

When no source is selectable, chronyd serves its own clock at stratum 10 with
the leap flag clear, so RTC-less clients accept it. Stratum 10 leaves headroom:
any client that can also see a real source (a dual-homed laptop on mesh-2g)
prefers the lower stratum. No `orphan` option — orphan mode exists to break
loops between multiple local-capable servers, this subnet has exactly one, and
it changes served-stratum semantics for no benefit here.

Holdover time is only useful because two Debian/Pi OS defaults keep it
approximately right instead of 1970: `driftfile` (base chrony.conf) and
fake-hwclock (restores last saved time at boot; saves hourly). Both are
*verified, not blindly installed* — verify.sh gains checks, and if fake-hwclock
is genuinely absent on the bench Pi the fix is one apt install recorded there.
Without fake-hwclock, holdover after a cold boot would confidently serve epoch
— worse than serving nothing, because nodes would "validate" garbage time and
prune against a nonsense cutoff.

**2. Time-validity in `/api/system` (ui/halow_ui.py).** New helper
`time_sync()` shells `chronyc -c tracking` (constant string through the
existing `sh()` at ui/halow_ui.py:60-64) and returns:

```json
"time_sync": {
  "state": "synced",
  "stratum": 3,
  "ref_id": "C0A83201",
  "leap": "Normal",
  "ref_time": 1754407712,
  "offset_ms": 0.12
}
```

CSV field map for `chronyc -c tracking` (chrony 4.x, 14 fields): `[0]` ref id
hex, `[1]` ref address, `[2]` stratum, `[3]` ref time (epoch float), `[4]`
current correction s, `[5]` last offset, `[13]` leap status. Confirm the order
once on the Pi before merging — one bench run, not an assumption.

Classification is separate from the raw fields (the halow-mon rule: detect the
event, classify separately — raw values are always exposed so the operator is
not hostage to our classifier):

- `chronyc` fails / empty output → `{"state": "unknown"}` (chrony down; never
  a 500).
- `ref_id == "7F7F0101"` (127.127.1.1, the local reference) → `"holdover"`.
- `leap == "Normal"` → `"synced"`.
- anything else → `"holdover"` (serving thanks to `local`, not synced).

Deliberately absent: a `last_sync_age_s` field. Under the local reference it is
unverified whether chrony's reported ref time keeps advancing or freezes at the
last real sync; publishing a number whose meaning is unconfirmed is a [C]
dressed as an [M]. Drill T3 records the actual behavior; add the field in a
follow-up only if the drill shows it is honest. Holdover *duration* has an
honest source already: the per-minute history (piece 3).

**3. History + badge.** `scripts/halow-mon` adds one boolean to its per-minute
sample: `"time_synced": true|false` from the same `chronyc -c tracking` parse
(it runs as root; no privilege question). `/api/metrics` adds `"time_synced"`
to the `uptime_pct` tuple (ui/halow_ui.py:824), giving a 24 h synced-% for
free. The Overview `ovw()` grid (ui/halow_ui.py:1174-1181) gains a seventh
tile:

- synced → `s3` green (`ok`), key "NTP"
- holdover → `HOLDOVER` amber (`warn`)
- unknown → `no time` red (`bad`)

**Privilege model.** No sudoers change. `chronyc tracking` is a read-only
command served over chrony's localhost command port (default `cmdallow`
localhost), so the unprivileged `halow-ui` user can run it directly — confirm
once on the Pi as `sudo -u halow-ui chronyc -c tracking` (acceptance T2).
halow-mon already runs as root via halow-mon.timer. No new halowctl subcommand:
there is nothing to mutate, and the sudoers whitelist
(config/sudoers-halow-ui:3 "keep this list short on purpose") stays untouched.
No confirm=1 flow: nothing here is destructive or identity-changing.

**Bounds.** Every new operation is a single subprocess with the existing
`sh()` 10 s default timeout (UI) or 15 s (halow-mon); no loops, no captures,
no growth in any file beyond the existing metrics ring (halow-mon:18,
RING_LINES 2880 + prune at 135-142).

## Implementation steps

Each step is one commit; execute top to bottom.

1. **Baseline the defect [M].** On the Pi: `sudo chronyc offline`, then from a
   client on mesh-2g (10.42.0.x): `ntpdate -q 10.42.0.1` (or
   `chronyd -Q -t 5 'server 10.42.0.1 iburst'`). Record the refusal (expected:
   "no server suitable for synchronization found" or an unsync/stratum-0
   reply). Also capture the Debian base for the record:
   `grep -E "driftfile|makestep|local|confdir" /etc/chrony/chrony.conf` and
   `dpkg -l fake-hwclock; systemctl is-enabled fake-hwclock` — this converts
   the two [C]s in Current state into [M]s. `sudo chronyc online` to restore.
   Paste results into the PR description; no code change.
2. **`config/chrony-halow.conf`:** append `local stratum 10` with a comment
   stating why (RTC-less clients, holdover bounded by fake-hwclock) and why no
   `orphan`. Keep the existing lesson comment intact.
3. **`scripts/deploy.sh`:** in the ssh install block, after the drop-in
   install (line 62), add `sudo systemctl restart chrony || true` next to the
   existing dnsmasq restart (line 83 pattern) so chrony config changes apply
   on deploy, not on the next reboot.
4. **`scripts/verify.sh`:** add three `chk` lines following the existing
   pattern (verify.sh:7-15):
   - `chk "chrony local holdover directive effective" sh -c 'sudo chronyd -p 2>/dev/null | grep -E "^local"'`
     (`chronyd -p` prints the *effective merged* config — this is the guard
     against the conf.d ordering gotcha in Risks, and against future base-file
     drift).
   - `chk "fake-hwclock enabled" sh -c 'systemctl is-enabled fake-hwclock 2>/dev/null | grep -E "enabled|static"'`
   - `chk "chronyc answers (tracking)" sh -c 'chronyc -c tracking | head -1'`
5. **`ui/halow_ui.py`:** add `time_sync()` helper above `api_system()`
   (~line 916): parse `sh("chronyc -c tracking")` per the Design field map,
   full try/except returning `{"state": "unknown"}` on any failure; add
   `"time_sync": time_sync()` to the `api_system()` dict (918-933). Constant
   command string only — no request data reaches the shell.
6. **`ui/halow_ui.py`:** Overview badge in `ovw()` (1172-1183): seventh stat
   tile driven by `s.time_sync.state` with the ok/warn/bad classes already
   defined at ui/halow_ui.py:1000.
7. **`scripts/halow-mon`:** in `main()`, sample
   `"time_synced": '7F7F0101' not in tr and ',Normal' in tr` where
   `tr = sh("chronyc -c tracking")` (exact parse mirrored from step 5), added
   to the sample dict (88-98). Then in `ui/halow_ui.py:824` extend the
   `uptime_pct` tuple to `("ap", "dnsmasq", "upstream", "time_synced")`.
8. **Docs:** mark roadmap v2 item 21 done in `docs/feature-roadmap.md` with
   the drill numbers (measured stratum, refusal-then-holdover evidence), and
   add a short "Time" note to `docs/software-stack.md` (chrony + local stratum
   10 + fake-hwclock, and the mesh-v4 coordination flag: node
   `network.ntp_server` must be set to 10.117.0.1 because Meshtastic ignores
   DHCP option 42 — WiFiAPClient.cpp:47).

## Surface changes

**API**

| Endpoint | Change |
|---|---|
| `GET /api/system` | gains `time_sync` object (`state`, `stratum`, `ref_id`, `leap`, `ref_time`, `offset_ms`); `state` in `synced\|holdover\|unknown` |
| `GET /api/metrics` | `summary.uptime_pct` gains `time_synced`; per-minute samples gain `time_synced` bool |

**halowctl** — no change (read-only feature; nothing to mutate).

**UI**

| Element | Change |
|---|---|
| Overview gateway grid | new "NTP" stat tile: `s3` ok / `HOLDOVER` warn / `no time` bad |

**systemd** — no new units. deploy.sh now restarts the existing `chrony`
service on deploy.

**Config files**

| File | Change |
|---|---|
| `config/chrony-halow.conf` | `+ local stratum 10` (with comment) |
| `scripts/deploy.sh` | `+ systemctl restart chrony` in install block |
| `scripts/verify.sh` | + 3 `chk` lines (local effective, fake-hwclock, chronyc answers) |
| `config/sudoers-halow-ui` | **unchanged** — deliberate; state this in the PR |

## Testing & acceptance criteria

All receiver-side, bounded, and recorded as [M]. The pre-association drills
use the 2.4 GHz `mesh-2g` AP: `chrony-halow.conf:5` already allows
10.42.0.0/24, so any laptop/phone on mesh-2g is a legitimate NTP client of
10.42.0.1 today — same chronyd, same code path as a future HaLow station,
different interface.

**Testable today (pre-association):**

- T1 (defect baseline, before the fix): step 1 recorded a refusal from a
  mesh-2g client while sources were marked offline. PASS = the refusal is
  reproduced and pasted.
- T2 (privilege + parse): on the Pi,
  `sudo -u halow-ui chronyc -c tracking` returns a CSV line; field order
  matches the Design map; `/api/system` (authenticated curl) contains
  `time_sync.state == "synced"` with the upstream healthy, and stratum agrees
  with `chronyc tracking`. PASS = both, no sudoers edit anywhere in the diff.
- T3 (**the failure drill — mandatory**): `sudo chronyc offline`; wait for the
  source to become unselectable (watch `chronyc tracking` flip to ref id
  `7F7F0101`; bound the wait at 10 min); then (a) from the mesh-2g client,
  `ntpdate -q 10.42.0.1` returns an accepted answer with **stratum 10** and
  leap not unsync, and (b) `/api/system` reports
  `time_sync.state == "holdover"`. Record whether chrony's reported ref time
  advances or freezes during holdover (decides the follow-up
  `last_sync_age_s` field). Then `sudo chronyc online`; within 5 min the
  client sees a low stratum again and the API reports `synced`. PASS = all
  four transitions observed at the receiver. Variant (closer to the field
  event, optional): pull the eth0 cable instead of `chronyc offline` and
  restart chrony — but read the halow-mon interaction in Risks first.
- T4 (degraded API): `sudo systemctl stop chrony` → `/api/system` returns 200
  with `time_sync.state == "unknown"`; Overview shows the red `no time` tile;
  no traceback in the halow-ui journal. `sudo systemctl start chrony`. PASS =
  all three.
- T5 (epoch recovery, optional but recommended, bench Pi only): with sources
  offline, `sudo systemctl stop chrony`, step the clock back two days
  (`sudo date -s '2 days ago'`), start chrony, `sudo chronyc online` → the
  journal shows "System clock was stepped" (makestep budget was unspent), not
  a multi-day slew. Restore before anything else runs long enough to care.
  PASS = step observed. FAIL = execute the makestep contingency in Risks.
- T6 (history): after T3, `/api/metrics?minutes=60` shows `time_synced: false`
  samples spanning the drill and `uptime_pct.time_synced < 100`. PASS = the
  holdover window is visible in history.
- T7 (deploy idempotency): run `./scripts/deploy.sh` twice; both runs succeed;
  `verify.sh` passes all three new checks; `halowctl diff` reports no drift.

**Needs a joined station:**

- T8: a Heltec V4.2 node associated on 10.117.0.x, with `network.ntp_server`
  set to `10.117.0.1` (node-side change), logs "NTP Request Success" /
  reaches `RTCQualityNTP` within one 12 h cycle (force by rebooting the node —
  the update also fires when `lastrun_ntp == 0`, WiFiAPClient.cpp:275). PASS =
  node-side log or admin API shows valid time sourced from the gateway.
- T9: repeat T3 with the node as the client: during holdover the node still
  obtains time (stratum 10) over HaLow, and ADMIN-API pruning remains active
  because the node's clock stays "real". PASS = receiver-side confirmation on
  the node, not the gateway's word for it.

## Out of scope

- Node-side `network.ntp_server` configuration and any Meshtastic firmware
  change (mesh-v4 work; flagged in step 8's coordination note).
- A real RTC (DS3231 or similar) for the gateway — hardware; would upgrade
  holdover from "approximately right" to "right", separate proposal.
- `last_sync_age_s` / holdover-duration fields in `/api/system` — blocked on
  the T3 ref-time observation; history via `/api/metrics` covers the need
  meanwhile.
- GPS-disciplined time on the gateway (no GPS hardware on the Pi).
- Serving NTP to the LAN (192.168.x) — the allow list stays exactly as it is.
- Alerting on prolonged holdover (belongs with the watchdog/brownout policy
  work in item 24, which owns "declare and act on degraded states").

## Risks & gotchas

- **conf.d parse order.** Debian's base chrony.conf places `confdir` near the
  top, so drop-in directives parse *before* the base file's own. For
  single-use directives the effective winner must be observed, never assumed —
  that is exactly what the `chronyd -p` check in verify.sh (step 4) pins.
  `local` has no competitor in the base file, so this issue is safe; but if
  the T5 contingency ever needs a `makestep` override, it likely *cannot* live
  in the drop-in (the base `makestep 1 3` parses later) — the precedented fix
  is a deploy.sh sed of the base file, exactly like the avahi `host-name` sed
  at deploy.sh:63.
- **halow-mon fights the cable-pull drill.** After 5 consecutive
  upstream-unreachable samples, halow-mon bounces eth0 (halow-mon:73-84,
  30-min cooldown) and increments its intervention counters. The
  `chronyc offline` form of T3 avoids this entirely (upstream ICMP still
  works); if you run the cable-pull variant, either finish inside 5 minutes or
  `sudo systemctl stop halow-mon.timer` for the drill and restart it after —
  and expect the bounce counters to move if you don't.
- **Holdover is only as good as fake-hwclock.** Saves are hourly; a power cut
  loses up to an hour plus the outage duration. Nodes doing time-based pruning
  against a clock that is hours stale delete against a shifted cutoff —
  tolerable for retention windows of a day, visible for the 1 h minimum
  (ADMIN-API.md retention range 1-8760 h). If fake-hwclock turns out to be
  missing in step 1, install and enable it *and record that the image lacked
  it* — the next fresh provision needs the fact, and install.sh currently
  never mentions it.
- **Do not "improve" the classifier into the raw fields.** The API exposes
  chrony's raw stratum/ref_id/leap alongside `state` on purpose; collapsing to
  the enum alone repeats the vendor-number mistake in API form. Likewise
  resist adding a ref-time-derived age until T3 says the number is honest.
- **Secrets discipline (why it's easy here).** The chrony path touches no
  secret: the drop-in is world-readable config, `chronyc tracking` output
  contains only time state and upstream IPs, and both new shell-outs are
  constant strings through the existing `sh()` helpers. Keep it that way — no
  variant of these calls should ever interpolate request data.
- **Meshtastic ignores DHCP option 42** (verified, WiFiAPClient.cpp:47): the
  existing `dhcp-option=option:ntp-server` (dnsmasq-halow.conf:10) does
  nothing for the nodes by itself. Without the node-side `ntp_server` change,
  T8/T9 cannot pass; a node whose only route is the HaLow gateway will try
  the default NTP pool through NAT — which works while the upstream is up and
  silently dies with it, i.e. the same failure this issue fixes, one hop over.
- **Roadmap interactions.** Item 22 (storage discipline) rewrites how
  halow-mon persists its files; the `time_synced` field lands inside the same
  sample dict and survives that refactor, but coordinate commit order if both
  are in flight. Item 24 (watchdog/brownout) will want the holdover state as
  an input to its "decline high-draw ops" policy — the `/api/metrics` history
  added here is its feed.
- **Trixie chrony version.** `chronyd -p` and the 14-field `-c tracking`
  output assume chrony >= 4.0; Trixie ships 4.x, and step 1's baseline capture
  confirms the exact version and field order before any parser lands.
