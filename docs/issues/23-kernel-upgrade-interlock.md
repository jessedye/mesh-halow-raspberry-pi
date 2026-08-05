# 23. Kernel-upgrade safety interlock: detect orphaned morse.ko before reboot, auto-rebuild or hold

> Tier 2 - Reliability | Effort: medium | Impact: high | Depends on: none

## Problem

The MM6108 HaLow driver (`morse.ko` + `dot11ah.ko`) is out-of-tree with
no DKMS — the driver tree has no dkms.conf (docs/software-stack.md:45-48)
— so every kernel package bump leaves the new `/lib/modules/<ver>/`
without the module; the install script's header says `--driver-only` is
"needed after every kernel upgrade — no DKMS" (scripts/install.sh:7-8).
The only guards today are advisory and human-gated: a warning inside
`halowctl status` (scripts/halowctl:363-365) and a PASS/FAIL line in
scripts/verify.sh:17-18. Nothing watches for the event; nothing acts.

The trigger is routine. deploy.sh:24 runs `apt-get install` over ssh on
every deploy (userland packages — that line does not itself pull a
kernel, but it normalizes unreviewed apt activity on the box); the repo's
own header-mismatch recovery instructs `sudo apt-get install -y
linux-image-rpi-v8 && sudo reboot` (scripts/install.sh:82); and
software-stack.md:45-48 names `apt full-upgrade` as the orphaning event.
Once a new kernel is installed, the next reboot — planned,
watchdog-driven (#24), or a brownout power-cycle (this bench has browned
out two boards) — comes up with no `morse.ko`, no `halow0`, no AP.

Worse, the failure is *silent by construction*. `halow-ap.service` is
`BindsTo=sys-subsystem-net-devices-halow0.device` — with no module the
device never appears, so the unit never starts and never *fails*. And the
self-healer explicitly ignores it: halow-mon gates AP healing on
`halow_present = os.path.isdir("/sys/class/net/halow0")`
(scripts/halow-mon:50-53), so `ap_wanted` is false and no restart,
counter, or log line fires. Every layer of recovery machinery reports a
calm, healthy gateway with a dead radio.

Why now: first ESP32 association is imminent, and for the mesh-v4 nodes
HaLow is not a convenience — "the Pi NATs toward the LAN, which makes
HaLow the only rung that still reaches the LAN when the 2.4 GHz AP is
gone" (mesh-v4 docs/transport-ladder-halow.md:296-297). A silently dead
AP removes the nodes' last LAN-reaching rung exactly when nobody is at a
shell. The escape hatch is narrow: `--prebuilt` matches only the exact
archived kernel (scripts/install.sh:62), and `prebuilt/` holds exactly
one kernel dir, `6.18.39+rpt-rpi-v8/`. Adversarially verified: no apt
hook, no path unit, no DKMS anywhere in systemd/ or scripts/.

## Current state

All references verified this session, both repos:

- **scripts/install.sh:32-55** — `driver_build()` builds against
  `KERNEL_SRC=/lib/modules/$(uname -r)/build` with
  `KCFLAGS=-Wno-error=cpp` (Pi OS builds warnings-as-errors), then
  `modules_install` + `depmod -a`. Hard-wired to the *running* kernel —
  it cannot build for a newly-installed, not-yet-booted one. **:57** —
  `--driver-only` wraps `driver_build` only. **:60-69** — `--prebuilt`
  installs archived .ko for `$(uname -r)` only, erroring otherwise (62).
  **:34-45** — the mmrc rate-control submodule is absent from release
  tarballs and must be fetched at the pinned SHA; any rebuild path must
  reuse this, not reimplement it. **:79-84** — the full install aborts
  when the running kernel has no headers and tells the operator to
  install `linux-image-rpi-v8` and reboot — our own docs walk operators
  into the orphaning event.
- **scripts/halowctl:362-365** — `status` checks
  `/lib/modules/$K/{updates,extra}/morse.ko*` and prints
  `!! NO morse.ko FOR RUNNING KERNEL...` on miss. Human-run only;
  halowctl:25 also execs sudo when halow.env is unreadable, so the
  unprivileged UI user cannot simply shell out to `status`.
- **scripts/verify.sh:17-18** — the same check as a PASS/FAIL line;
  human-run only.
- **scripts/deploy.sh:24** — `apt-get install ... >/dev/null 2>&1` on
  every deploy; hook output there is swallowed, so alerts must go to the
  journal/state file, never stdout. **:55** — deploy never clobbers a
  live `/etc/halow/halow.env`, so new env keys do not reach deployed
  boxes automatically.
- **scripts/halow-mon:50-53** — `ap_wanted = halow_present and enabled`;
  the missing-module case yields `halow_present=False` → the monitor
  samples on, heals nothing, alerts nothing (timer: 1/min).
- **ui/halow_ui.py:918-933** — `/api/system` returns uptime, load, mem,
  temp, `kernel: uname -r`, disk; no driver-health field. **:1172-1183**
  — Overview `ovw()` renders the kernel string only; no banner
  machinery. **:501-507** — reboot endpoint: the confirm=1 +
  `subprocess.Popen` pattern for privileged async actions. **:373-401**
  — SSID/passphrase confirm=1 pattern.
- **config/sudoers-halow-ui** — exact-match whitelist (20-21 already
  grant restart/reboot); any new UI root action needs a line here.
- **systemd/** — nine units; none watch /lib/modules or touch apt. No
  `/etc/apt/apt.conf.d` fragment anywhere in the repo.
- **prebuilt/** — `6.18.39+rpt-rpi-v8/{morse.ko,dot11ah.ko}` +
  s1g-bins.tar.gz: one kernel, the pinned image (software-stack.md:9).
- **mesh-v4 docs/transport-ladder-halow.md:296-297** — the last-LAN-rung
  claim quoted above; the clients' ladder needs this AP up unattended.

## Design

One root-owned guard script, invoked from three places (apt hook, boot
unit, operator/UI), publishing one world-readable state file every
existing surface reads. Detection is synchronous and cheap; rebuilds
dispatch asynchronously as a journaled, bounded, single-instance unit.

### Components

| Piece | Location | Runs as |
|---|---|---|
| Guard script | `scripts/halow-kernel-guard` → `/usr/local/bin/` | root |
| apt hook | `config/apt-halow-kernel-guard` → `/etc/apt/apt.conf.d/99halow-kernel-guard` | root (inside apt) |
| Boot check | `systemd/halow-kernel-guard.service` (oneshot) | root |
| Rebuild job | transient `halow-kernel-rebuild.service` via `systemd-run` | root |
| State file | `/var/lib/halow/kernel-guard.json` (0644, atomic tmp+mv) | written by root, read by all |
| CLI | `halowctl driver status\|rebuild\|hold\|unhold` | root via existing sudo-climb |
| API/UI | `/api/system.kernel_guard`, Overview banner, `POST /api/system/driver-rebuild` | halow-ui + sudoers line |
| Monitor | halow-mon samples guard state, logs transitions | root (1/min timer) |

### Detection (guard `check` mode)

For every installed kernel under `/lib/modules/*/` with a `kernel/`
subdir (filters build leftovers), test for `updates/morse.ko*` or
`extra/morse.ko*` — the paths halowctl:364 and verify.sh:18 already
trust. The *next-boot* kernel is the highest version (`linux-version
sort` when present, `sort -V` fallback). Verdicts:

- next-boot kernel has the module → `ok`
- next-boot lacks it, running kernel has it → `orphaned` (pre-reboot
  window: the AP is still up; this is the moment to act)
- running kernel lacks it → `dead` (post-reboot; halow0 absent now)

### Response policy (default: rebuild, hold on failure)

On `orphaned` or `dead`, in order:

1. **Prebuilt restore** if `prebuilt/<kver>/` exists in the repo checkout
   — seconds, no toolchain (install.sh --prebuilt logic, generalized to a
   target kernel).
2. **Rebuild** if `/lib/modules/<kver>/build` exists: dispatch
   `systemd-run --unit=halow-kernel-rebuild -p Nice=10
   -p RuntimeMaxSec=1800 halow-kernel-guard rebuild <kver>`. The fixed
   unit name is the concurrency lock — a second dispatch fails cleanly.
   The rebuild calls `install.sh --driver-only <kver>` (extended, see
   steps) so the KCFLAGS workaround, mmrc pin, and vendor-tarball
   fallback stay in one place. Skipped while the undervoltage bit
   (`vcgencmd get_throttled` bit 0) is set — a multi-minute 4-core build
   during active brownout is how boards die on this bench; fall through
   to hold instead (coordinates with #24).
3. **Hold + alert** if neither works or the rebuild fails: `apt-mark
   hold linux-image-rpi-v8 linux-headers-rpi-v8`, state `held`. Hold
   does NOT undo an already-installed kernel — it still boots next; hold
   only stops further drift while the alert is loud. Hence the boot
   check.

### Boot check (belt and suspenders)

`halow-kernel-guard.service` (oneshot) runs `halow-kernel-guard boot`:
if the *running* kernel lacks the module — image reflash, out-of-band
dpkg, or a missed pre-reboot window — write state `dead`, log loudly,
run the same restore ladder for `$(uname -r)`. On success: `depmod -a` +
`modprobe morse`; the udev device appears and halow-ap starts by itself
via its `BindsTo`/`WantedBy` device coupling — the AP recovers without a
second reboot. Caveat: a fresh image may boot an older kernel than the
shipped headers (install.sh:77-84); with no headers for the running
kernel, alert-and-stop, never loop.

Why an apt hook rather than a `/lib/modules` path unit:
`DPkg::Post-Invoke` fires at the causal moment and covers
unattended-upgrades (which drives dpkg through apt), while a path unit
also wakes on every module install *including our own rebuild's*
`modules_install` — a feedback loop — for coverage the boot check
already provides. The hook must never block or fail apt: detection
only, async dispatch, unconditional `|| true` (deploy.sh:24 runs apt on
every deploy; a minutes-long synchronous rebuild would stall it).

### State file contract

`/var/lib/halow/kernel-guard.json`, 0644, written via mktemp + mv (atomic
— #22's brownout-mid-write lesson applies to a file the boot path trusts):

```json
{
  "checked_at": "2026-08-05T14:22:11-0500",
  "running_kernel": "6.18.39+rpt-rpi-v8",
  "running_has_module": true,
  "next_boot_kernel": "6.18.45+rpt-rpi-v8",
  "next_boot_has_module": false,
  "state": "orphaned",
  "policy": "rebuild",
  "holds": [],
  "last_rebuild": {"kernel": "...", "ok": true, "seconds": 0, "at": "..."}
}
```

`state` ∈ `ok | orphaned | rebuilding | held | dead`;
`last_rebuild.seconds` is measured wall time [M], never an estimate.
Nothing secret ever enters this file or the journal: the guard reads its
policy key with a targeted grep of halow.env, never by sourcing the file
(halow.env holds the SAE passphrase, which has leaked twice via
"harmless" echoes; `set -x` in this script is forbidden likewise).

`/api/system` gains `"kernel_guard": { ...kernel-guard.json... }` beside
the existing `kernel` field; the UI reads the 0644 state file directly
(no new sudo for display). Missing file → `{"state": "unknown"}`,
rendered as a warning, never silently ok.

### Privilege model

- The guard runs as root in all three contexts (apt, boot unit, transient
  rebuild unit); it is not network-reachable.
- halow-ui stays unprivileged. Reading state: direct file read. Acting:
  `POST /api/system/driver-rebuild` shells `sudo halowctl driver rebuild`
  via one new sudoers line, using the reboot endpoint's Popen pattern
  (ui/halow_ui.py:506) — return immediately, state file shows
  `rebuilding`. Hold/unhold via API require `confirm=1` (an apt-policy
  change is an operator decision, same bar as ui/halow_ui.py:379/504).
- halow-mon (already root, 1/min) only reads the state file and logs
  transitions — it never triggers rebuilds; one actor owns the response.

## Implementation steps

1. **install.sh: target-kernel builds.** `driver_build()` takes an
   optional kernel version `KVER=${1:-$(uname -r)}`; replace both
   `$(uname -r)` uses (scripts/install.sh:49,53) with `$KVER`, run
   `depmod -a "$KVER"`. Extend the entry points to `--driver-only [KVER]`
   (line 57) and `--prebuilt [KVER]` (lines 60-69). No behavior change
   when invoked bare.
2. **Add `scripts/halow-kernel-guard`** with `check` mode: kernel
   enumeration, next-boot resolution, module tests, verdict, atomic JSON
   write, `logger -t halow-kernel-guard` on every state *transition*.
   Read `HALOW_KERNEL_POLICY` via
   `grep -oP '^HALOW_KERNEL_POLICY=\K.*' /etc/halow/halow.env`, default
   `rebuild` when absent (deployed boxes never get new env keys —
   deploy.sh:55).
3. **Add `rebuild <kver>` mode**: undervoltage gate (get_throttled bit 0
   → refuse, log, fall to hold), prebuilt check
   (`<repo>/prebuilt/<kver>/` — repo path from `HALOW_REPO_DIR`, default
   `/home/pi/mesh-halow-raspberry-pi`, matching halowctl:197), else
   `install.sh --driver-only <kver>` with wall time written to
   `last_rebuild`; on success re-run `check`; on failure apt-mark hold
   both metapackages, state `held`.
4. **Add `check --dispatch` and `boot` modes**: `--dispatch` (apt-hook
   context) dispatches the rebuild via the systemd-run line above and
   returns immediately (unit-name collision = already running = exit 0).
   `boot` runs the ladder synchronously for `$(uname -r)`, finishing with
   `modprobe morse` on success; headers absent → state `dead`, alert,
   stop.
5. **Add `hold`/`unhold` modes** wrapping `apt-mark hold/unhold` on the
   two metapackages, updating `holds` in the state file.
6. **Add `config/apt-halow-kernel-guard`**:
   `DPkg::Post-Invoke { "/usr/local/bin/halow-kernel-guard check --dispatch || true"; };`
   — one line plus a comment header on why it must never fail apt.
7. **Add `systemd/halow-kernel-guard.service`** (oneshot,
   After=local-fs.target, ExecStart=`halow-kernel-guard boot`,
   WantedBy=multi-user.target).
8. **deploy.sh**: install the guard script beside the other `install
   -m755 scripts/...` lines (~66-69), the apt fragment to
   `/etc/apt/apt.conf.d/99halow-kernel-guard` (m644), and add the new
   service to the systemd install line (67) and enable list (81).
9. **halowctl**: new `driver` subcommand — `status` (pretty-print the
   state file), `rebuild [KVER]`, `hold`, `unhold` (delegating to the
   guard). In `status` (lines 363-365), read kernel-guard.json when
   present, keeping the `ls` fallback so a box without the guard still
   warns. Update the usage header (lines 2-17).
10. **config/halow.env.example**: add `HALOW_KERNEL_POLICY=rebuild` and
    `HALOW_REPO_DIR=/home/pi/mesh-halow-raspberry-pi` with comments.
    `halowctl diff` compares env by key set only (halowctl:207-210), so
    upgraded boxes show `missing key on device` until the operator adds
    them — the drift detector working as designed; note it in the commit.
11. **ui/halow_ui.py**: `api_system()` (918-933) merges the state file
    under `kernel_guard`; `ovw()` (1172-1183) renders a `.bad` banner
    card when `state` is not `ok` (`unknown` → `.warn`) with a "rebuild
    driver" button; add `POST /api/system/driver-rebuild` (Popen +
    immediate return) and `POST /api/system/driver-hold` (`confirm=1`).
12. **config/sudoers-halow-ui**: add
    `halow-ui ALL=(root) NOPASSWD: /usr/local/bin/halowctl driver *`.
13. **scripts/halow-mon**: add `"kernel_guard": "<state>"` to the
    per-minute sample; on transition away from `ok`, `logger -t
    halow-mon` an event and remember the last-seen state in
    mon-state.json (no counter bump — detection, not a heal action).
14. **verify.sh**: add a `chk "kernel guard state ok"` line grepping
    `"state": "ok"` out of kernel-guard.json, after the morse.ko check.
15. **Docs**: software-stack.md "No DKMS" bullet (45-48) gains one line
    pointing at the interlock; README state log entry after bench
    verification.

## Surface changes

### API

| Endpoint | Change |
|---|---|
| `GET /api/system` | new `kernel_guard` object (state file passthrough) |
| `POST /api/system/driver-rebuild` | new; async dispatch, returns `{"ok":true,"output":"rebuild dispatched"}` |
| `POST /api/system/driver-hold` | new; `confirm=1` required; body `op=hold\|unhold` |

### halowctl

| Command | Change |
|---|---|
| `halowctl driver status` | new; prints guard state (falls back to ls check) |
| `halowctl driver rebuild [KVER]` | new; runs the restore ladder |
| `halowctl driver hold\|unhold` | new; apt-mark on the two kernel metapackages |
| `halowctl status` | warning now sourced from kernel-guard.json when present |

### UI

| Element | Change |
|---|---|
| Overview banner | red card when guard state != ok; warn on unknown; rebuild button |

### systemd / apt

| Unit / file | Change |
|---|---|
| `halow-kernel-guard.service` | new oneshot boot check, enabled |
| `halow-kernel-rebuild.service` | transient (systemd-run), never installed |
| `/etc/apt/apt.conf.d/99halow-kernel-guard` | new Post-Invoke fragment |

### Config

| File | Change |
|---|---|
| `config/halow.env.example` | `HALOW_KERNEL_POLICY=rebuild`, `HALOW_REPO_DIR=...` |
| `config/sudoers-halow-ui` | `halowctl driver *` line |
| `/var/lib/halow/kernel-guard.json` | new state file (0644, atomic) |

## Testing & acceptance criteria

All bounded; every claim below is a receiver-side observation (journal
line, state-file content, API response), not the actor's own report.

### Testable today (pre-association)

1. **Clean path [M]**: deploy; `halowctl driver status` and
   `GET /api/system` both show `state: ok`, `running_has_module: true`;
   verify.sh gains a PASS line.
2. **Hook fires [M]**: `sudo apt-get install --reinstall dnsmasq`;
   `journalctl -t halow-kernel-guard -n 5` shows a check entry and
   `checked_at` advances. Measure hook overhead by timing the apt run
   with and without the fragment; accept < 2 s added.
3. **Orphan detection [M]**: simulate the pre-reboot window without
   touching the running kernel:
   `sudo mkdir -p /lib/modules/6.99.0-test/kernel`, run
   `halow-kernel-guard check`. Expect `orphaned` then (no headers →
   ladder falls through) `held`; `apt-mark showhold` lists both
   metapackages; UI banner red; `halowctl status` warns; halow-mon's
   next sample carries `kernel_guard: held` and logs one transition.
   Cleanup: rm the fake dir, `halowctl driver unhold`, re-check → `ok`.
   Bounded: the fake dir exists only for the test's duration.
4. **Dead-kernel detection + recovery [M]**: `sudo mv
   /lib/modules/$(uname -r)/updates/morse.ko /root/`, `sudo depmod -a`,
   run `halow-kernel-guard boot`. Expect `dead` in the journal, then the
   prebuilt path restores (the running kernel is the one archived
   kernel, `6.18.39+rpt-rpi-v8`), `modprobe morse` probes, halow0
   reappears, halow-ap active. Confirm at the receiver: from the PC,
   `curl -sk https://192.168.51.202:8443/api/system` shows
   `kernel_guard.state: ok` and verify.sh fully passes. Real recovery,
   not a mock.
5. **Rebuild duration [M]**: `halowctl driver rebuild $(uname -r)` with
   the source tree present; record `last_rebuild.seconds` and SoC temp
   before/after from `/api/metrics`. Acceptance: completes inside
   RuntimeMaxSec=1800, no throttle flag raised, `lsmod` shows morse
   after `probe`. This produces the first measured rebuild number —
   until then the duration is unknown, not "about five minutes".
6. **Concurrency lock [M]**: dispatch two rebuilds back-to-back; the
   second `systemd-run` refuses (unit exists), guard exits 0, exactly
   one rebuild in the journal.
7. **No secret leakage [M]**: after all of the above, an operator-run
   local grep of `journalctl -t halow-kernel-guard -t halow-mon` for the
   passphrase value returns 0 hits; the state file holds no env values.
8. **Undervoltage gate**: do not brown out a board to test the brownout
   guard; verify the branch by review plus a temporary
   `HALOW_GUARD_FAKE_THROTTLE=1` hook deleted before merge.

### Needs a joined station

9. **The end-to-end reason this issue exists [M]**: with a Heltec V4.2
   node associated, run a real `sudo apt full-upgrade` pulling a new
   kernel; observe `orphaned` → `rebuilding` → `ok` with no human
   action, reboot, and confirm at the receiver: the node reassociates
   (station-events shows AP-STA-CONNECTED), holds a lease, and answers
   ICMP (`/api/diag/ping` to its 10.117.0.x). The AP surviving its first
   unattended kernel bump with a live station is the acceptance bar; log
   it in the README state log with the measured rebuild time.

## Out of scope

- **Adopting DKMS** — that means carrying the KCFLAGS workaround and
  mmrc pin inside a dkms script debugged blind inside dpkg triggers; the
  guard reuses install.sh, already proven on this board.
- **Applying Morse's 999-* kernel patches** — unchanged policy
  (docs/software-stack.md:49-52); the interlock rebuilds the module, it
  does not patch kernels.
- **Automatic reboots after rebuild** — the guard makes the *next* reboot
  safe; it never causes one. Reboot policy is #24's territory.
- **Configuring unattended-upgrades** — the hook covers it if present;
  installing/tuning it is an ops decision, not this issue.
- **Off-device backup of built modules** — archiving fresh .ko into
  `prebuilt/` stays a manual PC-side commit (#27 owns off-device copies);
  the guard may leave a copy under
  `/var/lib/halow/prebuilt-candidate/<kver>/`, nothing more.
- **STA-mode specifics** — same module, same fix (A5: STA mode is
  untested anyway).

## Risks & gotchas

- **Post-Invoke blocks apt.** Every second the hook takes is added to
  every apt run, including deploy.sh:24. Detection stays in the tens of
  milliseconds; the rebuild is always dispatched, never inline; `|| true`
  because a failing Post-Invoke breaks apt itself — the guard must never
  wedge package management on a remote gateway.
- **deploy.sh:24 swallows output** (`>/dev/null 2>&1`). The hook's only
  reliable channels are the journal and the state file. Printing advice
  to stdout is not an alert — that is the exact failure mode of the
  current halowctl:363-365 warning.
- **Hold arrives too late by design.** apt-mark cannot un-install the
  kernel that just landed; after `held`, the next reboot still boots the
  module-less kernel. The boot check is not optional hardening — it is
  the second half of the hold policy.
- **Rebuild needs sources.** install.sh builds from `~/halow` work trees
  fetched from `vendor/` tarballs or GitHub (install.sh:20-30). A box
  that only ever ran `--prebuilt` has no work tree; the guard surfaces
  "no source tree" as a distinct journal line, and the vendor tarballs
  keep the path offline-safe. The mmrc pin (:34-45) and KCFLAGS
  workaround (:46-52) are exactly why the guard shells to install.sh
  instead of carrying its own make line.
- **Thermals and power.** A `make -j4` on an enclosed Pi 4 is a sustained
  thermal event, and this bench has brownout history with the MM6108
  TX-bursting the 3V3 rail. Nice=10 + RuntimeMaxSec bound it; the
  undervoltage gate refuses to start one during active brownout (#24's
  DECLINE-high-draw-ops policy, honored early).
- **Version ordering.** `+rpt-rpi-v8` suffixes sort acceptably under
  `sort -V`, but `linux-version sort` (linux-base) is the Debian-correct
  tool; prefer it. Getting next-boot wrong inverts the verdict silently
  — test 3's `6.99.0-test` sorts above any real 6.18.x on purpose.
- **Metapackage vs versioned packages.** Holding a versioned
  `linux-image-6.18.x...` package does nothing; the metapackages
  `linux-image-rpi-v8`/`linux-headers-rpi-v8` pull new versions. Hold
  exactly those two, list them in `holds`, make `unhold` the only way
  out — a forgotten hold stays visible on every status surface.
- **Headers lockstep.** linux-headers-rpi-v8 upgrades with the image, so
  post-upgrade rebuilds normally have headers. The asymmetric case is
  first boot after a reflash (image older than headers, install.sh:77-84)
  — the boot check must alert-and-stop there, not spin retrying a build
  it can never win.
- **halow.env is a secret store.** The guard greps its two keys out,
  never sources the file, never runs under `set -x`; state file and
  journal carry no env values. The PSK has leaked twice through echoes
  that looked harmless — treat every new root script as a leak candidate
  in review.
- **Interactions**: #24 (watchdog) makes unattended reboots *more*
  likely — land the interlock first or together. #22's atomic-write
  convention applies to kernel-guard.json from day one. #16's forensics
  read the journal, hence the stable `halow-kernel-guard` tag. The
  halow-mon change is read-only on purpose so the monitor's bounded-heal
  accounting (mon-state.json counters) stays honest.
