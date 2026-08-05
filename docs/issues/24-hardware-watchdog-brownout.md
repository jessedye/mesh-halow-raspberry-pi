# 24. Hardware watchdog + brownout ledger with high-draw decline policy
> Tier 2 - reliability | Effort: medium | Impact: high | Depends on: #22 (ledger writes use its atomic-write helper; land #22 first)

## Problem

The gateway's self-healing story has a hole one layer below where it operates.
`halow-mon` restarts a dead AP, dnsmasq, or upstream path — but it is itself a
`Type=oneshot` process fired by a systemd timer (`systemd/halow-mon.service:5`,
`systemd/halow-mon.timer:7`). A kernel hang, an SD-card stall, or a wedged
PID 1 stops the timer along with everything it was supposed to heal. Nothing in
the repo arms the BCM2835/BCM2711 hardware watchdog: no `dtparam=watchdog=on`
in `config/config.txt.snippet`, no `RuntimeWatchdogSec` anywhere, no
`WatchdogSec`/`sd_notify` on any unit, nothing in `scripts/deploy.sh` (verified
absent this session — the only "watchdog" hits in the repo are the roadmap
entry and the issues index). A hung gateway is a site visit, and HaLow is
deliberately the last LAN-reaching rung of the mesh-v4 transport ladder — when
it hangs, the fleet loses its only high-rate path and the operator loses the
console that would have shown why.

The second half is power. This bench has a measured brownout history:
mesh-v4's transport-ladder doc records that **two boards browned out on a
bus-powered hub [M]** (`mesh-v4/docs/transport-ladder-halow.md:218`) and that the
MM6108 draws **~200–250 mA total during TX at 21 dBm [C]** (VBAT 54–92 mA +
VDD_FEM 142–162 mA, `:119-120`). The gateway's own module is the same chip fed
from **Pi 3V3 pin 1, "inside but near the header's ~500 mA guidance"**
(`docs/wiring.md:70-74`, which says to adopt an external regulator "if TX-burst
brownouts ever appear"). The Pi firmware already reports rail sag — `vcgencmd
get_throttled` bit 0 — and `halow-mon` already samples it every minute. But the
sample is stored as a raw hex string and the UI decodes it for display only.
Nobody persists *transitions* (when undervoltage started, ended, how often),
and nothing stops an operator from piling a max-rate profile apply or a
30-second iperf3 saturation run onto an actively sagging rail — the exact move
that browns out a board mid-write.

Why now: first ESP32 association is imminent (blocked only on a decoupling
capacitor + antenna confirmation on the node side), and it brings the first
sustained TX bursts on the gateway radio. Arming the watchdog and brownout
policy *before* the first join means the first undervoltage event is recorded
evidence, not a mystery reboot.

## Current state

All references re-verified this session.

**Watchdog — verified absent everywhere it could live:**
- `config/config.txt.snippet` is 4 lines: comments + `dtoverlay=mm610x-spi`
  (line 4). No `dtparam=watchdog=on`.
- `systemd/` — all nine units read end to end. No `WatchdogSec=`, no
  `RuntimeMaxSec=`, no `TimeoutStartSec=`, no `Type=notify` anywhere.
- `scripts/deploy.sh` — installs units at line 67, `daemon-reload` at 80;
  never touches `/boot/firmware/config.txt` (the snippet is manually appended,
  per its own header) and never writes `/etc/systemd/system.conf.d/`.
- `scripts/verify.sh` — ten `chk` probes, none power- or watchdog-related.

**The verifier correction, confirmed in the units:** `halow-mon.service` is
`Type=oneshot` (`systemd/halow-mon.service:5`) fired by `halow-mon.timer` with
`OnUnitActiveSec=60` (`systemd/halow-mon.timer:7`). Unit-level `WatchdogSec`
petting is meaningless there — every run is a fresh process. Worse: for
`Type=oneshot` the default start timeout is **infinity** (systemd's
`DefaultTimeoutStartSec` exemption), and `halow-mon` makes unbounded blocking
calls — `subprocess.run(["systemctl", "restart", "halow-ap"])` at
`scripts/halow-mon:64`, dnsmasq restart at `:71`, `nmcli con up` at `:79-80` —
none with a timeout (only the `sh()` helper at `:22-27` has one). One hung
`systemctl restart` leaves the unit stuck "activating" forever, and
`OnUnitActiveSec` never re-fires because the unit never deactivates: the whole
sampling *and healing* chain stalls silently. A further correction to the spec
as written: `RuntimeMaxSec=` is documented as having **no effect on
`Type=oneshot`** units (systemd directs oneshots to `TimeoutStartSec=`), so
this issue uses `TimeoutStartSec=50`, not `RuntimeMaxSec`.

**The pet-able unit is halow-ui:** `Type=simple`, long-running Flask
(`systemd/halow-ui.service:8-9`), `Restart=on-failure`/`RestartSec=5`
(`:10-11`), runs as `halow-ui` (`:12`) with `NoNewPrivileges=no` +
`ProtectSystem=true` + `PrivateTmp=yes` (`:16-18`). The app serves HTTPS on
:8443, `threaded=True` (`ui/halow_ui.py:1265-1273`), TLS only when
`/etc/halow/ui-cert.pem`/`ui-key.pem` exist (`:1266-1271`).

**Undervoltage sampling exists; transitions and policy do not:**
- `scripts/halow-mon:87` runs `vcgencmd get_throttled`; `:95` stores the raw
  hex substring (`"throttled": thr.split("=")[-1] ...`) into the 2-day
  `metrics.jsonl` ring. No decode, no state, no transition detection.
- `ui/halow_ui.py:593-598` defines `THROTTLE_BITS` (bit 0 "undervoltage NOW",
  bit 16 "undervoltage occurred", etc.); `GET /api/diag/power`
  (`:710-724`) decodes flags per request — display only, nothing persisted.
- High-draw operations, all currently ungated:
  - `POST /api/halow/profile` (`ui/halow_ui.py:229-234`) → `sudo halowctl
    set-profile` → regenerates conf and restarts hostapd
    (`scripts/halowctl:99-104`). The `max-rate` profile is 8 MHz ch44
    (`config/halow-profiles.json:35-41`).
  - `POST /api/diag/capture` (`ui/halow_ui.py:480-486`) → `halowctl capture`,
    bounded 3–30 s / 5000 frames (`scripts/halowctl:163-170`).
  - `POST /api/halow/throughput` (`ui/halow_ui.py:518-548`) → iperf3 client,
    seconds capped at 30 (`:525`) — a sustained TX saturation run.

**Privilege facts that shape the design:** `halow-ui` is already in the
`video` group (`scripts/deploy.sh:52`) — the unprivileged UI runs `vcgencmd`
today (`/api/diag/power` proves it). `halow-mon` runs as root (no `User=`) and
already writes world-readable state: `mon-state.json` and `metrics.jsonl` both
chmod 0644 (`scripts/halow-mon:142,144-145`). `mon-state.json` is rewritten in
place with `open(w)` — #22's non-atomic-write defect; a brownout mid-write can
wedge the self-healer's own state, exactly the failure this issue's reboots
would trigger more often if #22 were skipped.

## Design

Four layers, each covering the layer below's blind spot:

| Layer | Covers | Mechanism |
|---|---|---|
| Hardware watchdog (BCM2835/BCM2711) | kernel hang, PID 1 death, SD stall | `dtparam=watchdog=on` + `RuntimeWatchdogSec=15` (PID 1 pets the hardware) |
| Unit watchdog on halow-ui | console hung/deadlocked while kernel fine | `Type=notify` + `WatchdogSec=60`; app pets only when it verifies itself serving |
| `TimeoutStartSec=50` on halow-mon | one wedged monitor run stalling the timer chain | systemd kills the run before the next minute fires |
| halow-mon (existing) | AP / dnsmasq / upstream faults | unchanged |

**1. Hardware watchdog.** Add `dtparam=watchdog=on` to
`config/config.txt.snippet` (manually appended to `/boot/firmware/config.txt`,
same as the overlay line — deploy.sh does not manage that file and this issue
keeps it that way). New drop-in `config/watchdog-system.conf`, installed by
deploy.sh to `/etc/systemd/system.conf.d/10-halow-watchdog.conf`:

```ini
# Hardware watchdog: PID 1 pets /dev/watchdog; a kernel hang or PID 1 death
# reboots the gateway autonomously. BCM2711 hardware maximum is ~15 s —
# larger values are silently clamped, so state 15 explicitly.
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
```

Applying `[Manager]` watchdog keys needs `systemctl daemon-reexec`
(`daemon-reload` is not sufficient); deploy.sh gains that line.

**2. halow-ui becomes the pet-able unit.** `systemd/halow-ui.service` changes
to `Type=notify` + `NotifyAccess=main` + `WatchdogSec=60`. In
`ui/halow_ui.py`, a daemon thread started before `app.run()`:

- implements `sd_notify` in ~10 lines of pure Python (AF_UNIX datagram to
  `$NOTIFY_SOCKET`, handling the abstract-namespace `@` prefix) — no new
  package; no-op when `NOTIFY_SOCKET` is unset so `python3 halow_ui.py` by
  hand still works;
- every `WATCHDOG_USEC/3` seconds (≈20 s; 20 if unset) performs a real local
  request — `GET https://127.0.0.1:8443/healthz`, timeout 5 s, cert
  verification off, scheme matching the same cert-existence check `__main__`
  uses — and only on success sends `WATCHDOG=1`. First success also sends
  `READY=1`: systemd considers the unit started only once it actually serves.
  On probe failure it sends nothing; after 60 s of missed pets systemd
  SIGABRTs the service and `Restart=on-failure` brings it back.
- New route `GET /healthz`: unauthenticated, outside `/api/`, returns the
  constant `ok` (200, text/plain). No version, no uptime, no config — nothing
  for the secrets rule to worry about, nothing touching the auth throttle.

The pet thread is independent of request handlers, so a slow sudo/halowctl
call in one worker thread does not miss pets (Flask is `threaded=True`); only
a genuinely wedged process/socket does.

**3. halow-mon run bound.** `TimeoutStartSec=50` in
`systemd/halow-mon.service` (below the 60 s cadence). A wedged run is killed
and the next timer fire lands on schedule; worst-case sampling gap ≈2 minutes.

**4. Brownout transition ledger** (in `scripts/halow-mon`, root, using #22's
atomic-write helper). Decode the already-sampled raw value; keep state in
`mon-state.json` via new keys (accessed with `.get()` defaults so existing
state files keep working — `load_state()` at `scripts/halow-mon:30-36` only
provides defaults for a missing file):

```json
{"uv_active": false, "uv_since": 0, "uv_count": 0,
 "uv_last_raw": 0, "uv_last_t": 0}
```

Transition rules per one-minute sample (`val` = parsed hex, bit 0 = NOW,
bit 16 = occurred-since-boot):

- bit 0 set, `uv_active` false → append `undervolt_start`, set
  `uv_active/uv_since`, `uv_count += 1`;
- bit 0 clear, `uv_active` true → append `undervolt_end` with
  `dur_s = now - uv_since`;
- bit 0 clear and not active, but bit 16 rose since the previous sample **in
  the same boot** → append `undervolt_blip` (`uv_count += 1`): the event
  started and ended between samples; bit 16 latches until reboot, so a rising
  edge is real evidence even when bit 0 was never caught high. Boot handling:
  if `uv_last_t` predates `now - /proc/uptime`, treat the previous bit 16 as
  clear (it reset at reboot).

Events append to `/var/lib/halow/brownout.jsonl`, chmod 0644, ring-capped at
1000 lines (prune rewrite goes through the #22 atomic helper — this file is
the record of power faults and must survive one):

```json
{"t": 1754419200, "event": "undervolt_start", "raw": "0x50005"}
{"t": 1754419320, "event": "undervolt_end", "dur_s": 120, "raw": "0x50000"}
{"t": 1754430000, "event": "undervolt_blip", "raw": "0x50000",
 "note": "occurred-bit rose between samples; sub-minute event"}
```

**5. Decline policy** (in `ui/halow_ui.py`, unprivileged). The gate is a
**live** `vcgencmd get_throttled` sample at request time — the UI can run it
itself (video group), so there is no staleness to reason about; the ledger is
history, not the gate. Decline only on **bit 0** (never bit 16 — it latches
until reboot and would refuse forever). If `vcgencmd` returns nothing, fail
open: absence of evidence is not evidence, and the watchdog covers a dead
monitor. Declined operations return HTTP 409:

```json
{"error": "undervoltage active (get_throttled bit 0): refusing iperf3 run. MM6108 TX bursts ~200-250 mA on the Pi 3V3 header pin (docs/wiring.md) and this bench has browned out two boards. Fix power first; history at /api/diag/brownouts.",
 "declined": "throughput", "policy": "brownout-high-draw"}
```

Gates while bit 0 is set:

| Operation | Rule | Rationale |
|---|---|---|
| `POST /api/halow/profile` | decline iff requested profile `width_mhz >= 8` (today: `max-rate` only) | the remediation path — switching *down* to `long-range` — must never be blocked |
| `POST /api/diag/capture` | decline iff `seconds > 10` | short captures stay allowed: debugging the brownout may need receiver-side eyes |
| `POST /api/halow/throughput` | decline always | up-to-30 s saturation TX is exactly the 200–250 mA burst case |

**6. Ledger API + UI.** New `GET /api/diag/brownouts`:

```json
{"active": false, "since": null, "count": 3,
 "last_event": {"t": 1754419320, "event": "undervolt_end", "dur_s": 120},
 "events": ["... last 50, parsed per-line, bad lines skipped ..."],
 "live": {"throttled_raw": "throttled=0x50000", "undervolt_now": false},
 "policy": {"declines_while_active":
   ["profile apply width>=8MHz", "capture>10s", "throughput runs"]}}
```

`GET /api/diag/power` gains `"undervolt_now"` and `"brownout_count"`. The Diag
power card grows one line — `brownouts: N (last: <event> <when>)`, red badge
while active. No preemptive button-disabling: the 409 body is the contract and
already surfaces in each card's output `<pre>`.

**Privilege model — zero new sudo surface.** Ledger written by root halow-mon
into `/var/lib/halow` (0644, same as `metrics.jsonl`); UI reads files and runs
`vcgencmd` via its existing video-group membership; watchdog config installs
through deploy.sh's existing root-over-ssh path. `config/sudoers-halow-ui` is
untouched.

## Implementation steps

1. **`config/config.txt.snippet`**: add `dtparam=watchdog=on` with a comment
   ("BCM2711 hardware watchdog — armed by systemd RuntimeWatchdogSec; kernel
   hangs reboot in ≤15 s"). Note in the snippet header that a reboot applies
   it. One line + comment; manual-append model unchanged.
2. **New `config/watchdog-system.conf`** (content in Design §1).
   **`scripts/deploy.sh`**: `sudo mkdir -p /etc/systemd/system.conf.d`,
   `sudo install -m644 config/watchdog-system.conf
   /etc/systemd/system.conf.d/10-halow-watchdog.conf`, then `sudo systemctl
   daemon-reexec` after the existing `daemon-reload` (line 80). In the verify
   section (lines 88–90), print `systemctl show -p RuntimeWatchdogUSec` and
   warn if `grep -q "^dtparam=watchdog=on" /boot/firmware/config.txt` fails.
3. **`scripts/verify.sh`**: three new `chk` probes — `/dev/watchdog0` exists;
   `systemctl show -p RuntimeWatchdogUSec` reports `15s`; and (post step 5)
   `systemctl show halow-ui -p WatchdogTimestampMonotonic` is nonzero (proves
   a pet was *received*, not merely sent — confirm at the receiver).
4. **`systemd/halow-mon.service`**: add `TimeoutStartSec=50` with a comment
   stating both facts: oneshot start-timeout defaults to infinity, and
   `RuntimeMaxSec` is a no-op for oneshot (why this isn't the spec's knob).
5. **`ui/halow_ui.py`**: add `_sd_notify(msg: bytes)` (pure-Python
   NOTIFY_SOCKET datagram, `@`→`\0` abstract handling, swallow `OSError`,
   no-op without the env var); add `GET /healthz` returning constant `ok`;
   add `_watchdog_thread()` per Design §2 (probe → `READY=1` once →
   `WATCHDOG=1` per success, nothing on failure); start it as a daemon thread
   in `__main__` before `app.run()`, choosing the probe scheme from the same
   cert-existence check.
6. **`systemd/halow-ui.service`**: `Type=simple` → `Type=notify`; add
   `NotifyAccess=main` and `WatchdogSec=60`; comment that missed pets SIGABRT
   the service and `Restart=on-failure` (unchanged) revives it. Deploy steps
   5+6 together — `Type=notify` with the old app never reaches READY.
7. **`scripts/halow-mon`**: parse the sampled `thr` into an int (the raw
   string at `:95` stays in the metrics sample unchanged); implement the
   transition rules from Design §4 with the new `uv_*` keys accessed via
   `.get()`; append events to `/var/lib/halow/brownout.jsonl`, `os.chmod`
   0644, ring-cap 1000 lines with the prune rewrite going through #22's
   atomic helper (as does the existing `mon-state.json` dump once #22 lands).
8. **`ui/halow_ui.py`**: add `GET /api/diag/brownouts` (Design §6 shape:
   state from `mon-state.json`, last 50 events parsed per-line with bad lines
   skipped, live `vcgencmd` sample); extend `api_diag_power()` with
   `undervolt_now` and `brownout_count`.
9. **`ui/halow_ui.py`**: add `_undervolt_now()` (live sample, bit 0 only,
   fail-open on empty output) and `_decline_high_draw(op)` returning the 409
   tuple or `None`; call it from `api_halow_profile` (only when the requested
   name resolves to `width_mhz >= 8` in `PROFILES`), `api_diag_capture` (only
   when `seconds > 10`), and `api_halow_throughput` (always). Error text as
   in Design §5 — names the reason, the measured evidence, and the history
   endpoint; echoes no command output.
10. **UI**: extend the Diag power card in `PAGE` (fetch
    `/api/diag/brownouts` alongside the existing `/api/diag/power` call in
    `diag()`): count + last event line, red `active` badge.
11. **Bench drills** (procedures in Testing): kernel-hang on a bench SD card,
    UI-freeze, PATH-shim brownout, halow-mon wedge. Record measured results
    ([M], with times) in the commit message and tick roadmap item 24 in
    `docs/feature-roadmap.md`.

## Surface changes

**API**

| Endpoint | Change |
|---|---|
| `GET /healthz` | new; unauthenticated constant `ok` — watchdog self-probe target |
| `GET /api/diag/brownouts` | new; ledger + counters + live undervolt + policy list |
| `GET /api/diag/power` | extended: `undervolt_now`, `brownout_count` |
| `POST /api/halow/profile` | 409 decline when undervolt active and target width ≥ 8 MHz |
| `POST /api/diag/capture` | 409 decline when undervolt active and seconds > 10 |
| `POST /api/halow/throughput` | 409 decline when undervolt active |

**halowctl** — no changes (the CLI is a root/trusted path; the policy gates
the network-facing API only).

**systemd units**

| Unit | Change |
|---|---|
| `halow-ui.service` | `Type=notify`, `NotifyAccess=main`, `WatchdogSec=60` |
| `halow-mon.service` | `TimeoutStartSec=50` |
| (manager drop-in) | new `/etc/systemd/system.conf.d/10-halow-watchdog.conf`: `RuntimeWatchdogSec=15`, `RebootWatchdogSec=2min` |

**Config files**

| File | Change |
|---|---|
| `config/config.txt.snippet` | + `dtparam=watchdog=on` (manual append + reboot) |
| `config/watchdog-system.conf` | new (installed by deploy.sh) |
| `config/sudoers-halow-ui` | **unchanged** — zero new sudo surface |
| `/var/lib/halow/brownout.jsonl` | new on-device ledger, 0644, 1000-line ring |
| `/var/lib/halow/mon-state.json` | new keys `uv_active/uv_since/uv_count/uv_last_raw/uv_last_t` |

**UI** — Diag power card: brownout count, last event, active badge.

## Testing & acceptance criteria

### Testable today (pre-association)

1. **Watchdog armed [M]**: after snippet append + reboot + deploy:
   `ls /dev/watchdog0` exists; `wdctl` shows the Broadcom watchdog with a
   15 s timeout; `systemctl show -p RuntimeWatchdogUSec` = `15s`; boot journal
   contains systemd's "Using hardware watchdog" line. All four pass.
2. **Kernel-hang drill [M]** — **bench SD card only, never the production
   card**: clone the card, boot it, `echo 1 > /proc/sys/kernel/sysrq;
   echo c > /proc/sysrq-trigger` over SSH. Pass: the Pi reboots with no human
   touch and answers ping within 15 s + normal boot time (record wall-clock);
   `verify.sh` passes after; `journalctl --list-boots` shows the boundary.
3. **UI-freeze drill [M]**: `sudo kill -STOP $(systemctl show halow-ui -p
   MainPID --value)`. Pass: within ≤90 s (WatchdogSec 60 + margin) the journal
   shows the watchdog timeout and SIGABRT kill, `NRestarts` increments, and —
   receiver-side — `curl -sk https://localhost:8443/healthz` returns `ok`
   again within 2 min of the STOP.
4. **Pet-verifies-serving [M]**: `systemctl show halow-ui -p
   WatchdogTimestampMonotonic` advances across two 30 s checks while serving
   (this is verify.sh's new probe). Negative half: on a bench run with the
   probe pointed at a wrong port, the timestamp must freeze and the service
   must be killed at 60 s — proving pets stop when serving stops.
5. **Ledger drill [M]** (PATH shim — both `halow-mon` and the UI resolve
   `vcgencmd` through the shell): in a bench shell, put a stub `vcgencmd`
   earlier in PATH and run `/usr/local/bin/halow-mon` by hand with the
   sequence `0x0` → `0x50005` → `0x50005` → `0x50000`. Pass:
   `brownout.jsonl` gains exactly one `undervolt_start` and one
   `undervolt_end` with `dur_s` ≈ the gap between runs; `uv_count` = 1.
   Blip case: fresh state, `0x0` → `0x50000` yields exactly one
   `undervolt_blip`. Re-running with `0x50000` again yields nothing (no
   re-trigger from the latched bit).
6. **Decline drill [M]**: run `halow_ui.py` by hand under the same shim
   reporting `0x50005`. Pass: `POST /api/halow/throughput` → 409 with
   `policy: brownout-high-draw`; `POST /api/halow/profile name=max-rate` →
   409; `name=long-range` → succeeds (remediation never blocked);
   `capture seconds=5` → succeeds; `seconds=20` → 409. With the shim
   reporting `0x0` all five succeed. Grep the 409 bodies: no passphrase, no
   env values, no command output — the error is a fixed string.
7. **Monitor wedge drill [M]**: bench copy of halow-mon with a `sleep 120`
   injected; trigger via the timer. Pass: journal shows the unit killed at
   ~50 s, the next timer fire lands on the next minute, and `metrics.jsonl`
   shows a gap of ≤2 samples.
8. **Deploy idempotence**: `deploy.sh` twice in a row; `daemon-reexec` leaves
   `halow-ap`/`halow-net`/`dnsmasq` active before/after; second run changes
   nothing.

### Needs a joined station

9. **Premise measured on the gateway [M]**: with a station associated, run
   max-rate iperf3 bursts and watch `get_throttled` + the ledger for 24 h.
   Record whether bit 0/bit 16 ever assert on this PSU — the policy's premise
   measured on the gateway itself, not inherited from the node bench. (If they
   never assert, the ledger is the proof of a clean rail — also a result.)
10. **Decline in anger [M]**: if undervoltage asserts during station TX,
    confirm the throughput endpoint 409s while the station *stays associated*
    (`iw dev halow0 station dump` before/after) — the decline must not itself
    disturb the link.
11. **Reboot re-association [M]**: repeat the kernel-hang drill (bench card)
    with a node joined. Pass: the station re-associates with zero node-side
    action; measure the join gap receiver-side from `station-events.log`
    timestamps.

## Out of scope

- **Node-side power work** — the Heltec boards' brownout handling, decoupling
  capacitor, and Vext switching live in mesh-v4.
- **PSU hardware changes** — the external 3.3 V regulator `docs/wiring.md:72-74`
  recommends is a wiring decision this ledger informs, not part of it.
- **Override flag on declines** — no `force=1`. SSH and `halowctl` remain
  ungated root paths; a policy with a web-side bypass is theater.
- **Gating inside halowctl** — the CLI is the trusted surface; the policy
  guards the network-facing API only.
- **Watchdog-reboot cause classification** — the Pi exposes reset causes
  poorly; boot-gap detection in metrics is future work.
- **Petting any unit other than halow-ui** — halow-ap and dnsmasq are already
  supervised by halow-mon's behavioral checks, which beat liveness pets.

## Risks & gotchas

- **BCM2711 hardware limit ~15 s**: larger `RuntimeWatchdogSec` values are
  silently clamped — keep 15 explicit and verify against `wdctl` output, not
  the config (measured over claimed applies to watchdogs too).
- **`daemon-reload` does not apply `[Manager]` watchdog keys** —
  `daemon-reexec` is required, or the drop-in is cosmetic until reboot.
- **`RuntimeMaxSec` is a documented no-op for `Type=oneshot`** (corrects the
  roadmap/spec wording); `TimeoutStartSec` is the real knob, and the oneshot
  default of *infinity* is exactly why the wedge risk exists today.
- **Watchdog resets are unclean SD cuts** — the precise failure #22's atomic
  writes exist to survive. Land #22 first (see the coordination note in
  `docs/issues/README.md`: 18/20/22/24 all touch halow-mon; 24 adds its
  ledger last).
- **Persistent-hang reboot loop**: a fault that recurs after every boot
  becomes a reboot loop. Accepted: a loop is visible in `journalctl
  --list-boots` and recoverable; a silent hang on the last LAN-reaching rung
  is neither. The runtime watchdog arms only after systemd starts, so a
  boot-time hang does not loop.
- **Interaction with #23**: a watchdog reboot right after an apt kernel bump
  boots a kernel with an orphaned `morse.ko` — the AP stays down (`BindsTo`
  the absent device) while the watchdog stays happy. Not a loop, but a
  radio-dead site; #23's interlock is the cover, not this issue.
- **Bit 16 latches until reboot** — decline on bit 0 only, or the gateway
  refuses high-draw ops forever after one blip. The UI already displays
  "occurred" separately (`THROTTLE_BITS`, `ui/halow_ui.py:593-598`); keep it
  display-only.
- **Threshold flapping**: bit 0 asserts at ~4.63 V with little hysteresis; a
  marginal PSU can flap per-request, giving intermittent 409s. That is the
  policy working — but the 1-minute ledger undercounts flaps (blips recover
  some). `uv_count` is a lower bound, not a flap count.
- **Type=notify failure semantics change**: a cert/bind breakage that today
  leaves a silently dead-but-"active" service becomes a visible restart loop
  (READY never sent → start timeout → `Restart=on-failure`). Intended, and
  bounded by `RestartSec=5`, but noisier in the journal.
- **`NOTIFY_SOCKET` under sandboxing**: the unit keeps `ProtectSystem=true` +
  `PrivateTmp=yes` (`systemd/halow-ui.service:16-18`); the notify socket
  lives under `/run/systemd/` and is unaffected, and `NotifyAccess=main`
  covers the pet thread (threads share the main PID). Verify with drill 4,
  not by assumption.
- **Kernel-hang drill discipline**: a sysrq crash on the production card
  risks the very SD corruption this feature must survive — bench card only
  (verifier requirement), and re-run `verify.sh` + `halowctl diff` after any
  drill before trusting the card again.
- **vcgencmd absent/failing** (e.g. video group lost in a future change):
  ledger and policy both fail open, matching the existing "vcgencmd
  unavailable" display path (`ui/halow_ui.py:716-717`) — the gateway must
  never brick its own diagnostics on a missing tool.
