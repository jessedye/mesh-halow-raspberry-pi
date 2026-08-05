# 22. Storage discipline: atomic ring writes, rotation, journald caps, disk low-water (DEFECT)

> Tier 2 - reliability | Effort: medium | Impact: medium | Depends on: none — but MUST land before #18 (per-station health ladder) and #20 (rung-cost endpoint) extend halow-mon, so their new writes are born atomic.

## Problem

The gateway is a Pi 4 running a Morse Micro MM6108 802.11ah AP (SSID `mesh`,
10.117.0.1/24) for ESP32 Meshtastic nodes. Two long-running writers keep its
operational state on the SD card: `scripts/halow-mon`, a root oneshot fired
every minute by `halow-mon.timer`, which samples metrics and self-heals the AP
stack; and `scripts/halow-sta-events`, a hostapd_cli action hook that appends
station join/leave lines. Both were built fast during roadmap v1 and neither
survives a power cut cleanly, and nothing on the box bounds log growth.

The write defect: halow-mon rewrites whole files in place with `open(path,
"w")` — the stations ring, the metrics ring, and `mon-state.json`. `open(...,
"w")` truncates immediately; the data lands later. A brownout in that window
leaves an empty or partial file. For the rings that means losing up to two
days of history that `/api/halow/link` and `/api/metrics` serve (and that #20
will turn into the TRANSPORT_HALOW rung cost). For `mon-state.json` it is
worse: that file is the self-healer's own memory — restart counters, down
streaks, and the 30-minute eth0-bounce holdoff. The audit called a torn write
here "wedging the healer"; the code is slightly kinder (see Current state):
the common outcome is a silent reset of counters and backoff — the healer
forgets every intervention it made and its bounded-action guarantees — and the
tail-risk outcome is a crash loop that stops healing and sampling entirely.
Either way the mechanism that keeps an unattended AP up degrades exactly when
power is flaky, which is exactly when it is needed.

Brownouts are not hypothetical on this bench. mesh-v4 `docs/hardware.md:24-26`
records the shared-VBUS budget that produced load-induced power cuts, and
`docs/transport-ladder-halow.md:218` states it plainly: "this bench already
browned out two boards on a bus-powered hub." The gateway's own roadmap item 4
exists because the MM6108 TX-bursts on the 3V3 rail. Field deployments will be
power-cycled without ceremony.

The growth defect: `station-events.log` is an unbounded append with no
logrotate anywhere in the repo, journald has no `SystemMaxUse` cap, and
`/api/system` returns raw `df` tokens with no low-water evaluation — nobody is
watching the disk. All of it is dormant today because zero stations have
associated; it starts filling at real cadence the moment the first ESP32 joins
(imminent — blocked only on a decoupling capacitor and antenna confirmation),
and #16 (hostapd log level raised, dnsmasq log-dhcp) and #18 (per-minute
classified transitions) multiply the volume.

## Current state

All lines below re-verified against the working tree this session.

**scripts/halow-mon** (root, oneshot, every 60 s via `halow-mon.timer`):

- `metrics.jsonl` append, lines 99-100 — append-only, safe to keep: a torn
  trailing line is tolerated by the reader (see below).
- `stations.jsonl` append lines 124-126, then in-place prune rewrite lines
  127-133: line 130 `open(stations_log, "w").writelines(lines[-RING_LINES *
  2:])`. Note the whole stations block, prune included, sits inside `if
  dump:` (line 105) — it only executes when a station is associated.
- `metrics.jsonl` prune rewrite lines 136-142: line 139 `open(METRICS,
  "w").writelines(lines[-RING_LINES:])`. `RING_LINES = 2880` (line 18, 2 days
  at 1/min); prune fires only when oversized, so roughly daily.
- `mon-state.json` write line 144: `json.dump(st, open(STATE, "w"))`. The
  file object is never explicitly closed — truncation happens at `open()`,
  bytes land at refcount-close. Brownout in between = empty file.
- `load_state()` lines 30-36: `json.load(open(STATE))` under `except
  Exception` returning a defaults dict. Consequences of a torn file:
  - Empty/invalid JSON (the common torn outcome): silent reset. All
    counters zero, `last_eth0_bounce` 0 — which makes line 76's `now -
    st["last_eth0_bounce"] > 1800` holdoff instantly true — and the
    `actions` audit trail erased. This directly violates the module's own
    contract (lines 5-9): "counting every intervention ... never hide how
    often healing fired."
  - Valid-JSON prefix that parses but lacks keys (rare — needs the torn
    file to end on a closing brace): `load_state` returns it as-is, and
    line 61 `st["ap_down_streak"] += 1` (or `act()`'s `state[key] += 1`,
    line 40) raises KeyError. main() dies before any heal and before the
    metrics sample; every subsequent minute dies the same way until a human
    deletes the file. This is the wedge. Correction to the audit text: the
    wedge is the tail risk, the silent reset is the likely outcome; the same
    two-line fix (atomic replace + defaults merge) closes both.
- Post-hoc `os.chmod(..., 0o644)` at lines 133, 142, 145 — needed because
  the files must stay readable by the unprivileged UI user.

**scripts/halow-sta-events**: line 6, `printf ... >> "$LOG"` to
`/var/lib/halow/station-events.log` — unbounded, no rotation. The service
(`systemd/halow-sta-events.service:8`) runs as `User=halow-ui`, and
hostapd_cli invokes the script once per event: no long-lived file descriptor,
which makes plain logrotate `create`-style rotation safe.

**ui/halow_ui.py** (Flask console, unprivileged `halow-ui` user):

- `/api/system` lines 918-933: `"disk": sh("df -h / | tail -1").split()` at
  line 932 — a raw token list, no thresholds. Sole consumer is the Overview
  card at line 1182: `root ${esc((s.disk[3]||"?"))} free`.
- Ring readers are already torn-line tolerant: `/api/halow/link` parses
  per-line under try/except (lines 751-757), `/api/metrics` likewise (lines
  798-804), `mon-state.json` read at 807-811 falls back to `{}`. Good — the
  fix does not need reader changes for the jsonl files.
- One more unbounded append the audit missed: `throughput.jsonl` at lines
  544-545 (`/api/halow/throughput` results). Manual cadence, so slow, but
  same discipline applies — fold it into the logrotate drop-in.
- Low-water precedent already in the codebase: `/api/metrics` computes
  `mem_low_water_kb` at lines 819-820 with the comment "low-water mark is
  the value that means something (bench lesson)". Disk should follow it.

**Install path**: `scripts/deploy.sh` is where system config lands on the Pi
— dnsmasq/chrony/avahi drop-ins at lines 61-63, units at line 67, halow-mon
at 68, `/var/lib/halow` created `halow-ui:halow-ui` at line 70, sudoers at
71. `scripts/install.sh` is driver/toolchain provisioning only and installs
no service config — the audit named both files, but the new drop-ins belong
in deploy.sh alone.

**The convention to copy**: mesh-v4 `tools/meshdata.py:137-146` `state_save()`
— write to `p + ".tmp"`, then `os.replace(tmp, p)`, with the comment "a crash
mid-write must not leave a truncated state file". That code survived the file
transfer campaigns; halow-mon predates the lesson.

**The bound to copy**: `scripts/halowctl` capture, lines 163-169 — 3-30 s and
`-c 5000` frames, every operation capped. Grep confirms zero occurrences of
"logrotate", "journald", or "SystemMaxUse" outside the roadmap text.

## Design

Four small pieces, no new daemon, no new privilege.

### 1. Atomic write helper in halow-mon

One function, used at every full-file write site:

```python
def atomic_write(path, data, mode=0o644):
    """tmp in same dir + fsync + os.replace — a brownout mid-write leaves
    the OLD file intact, never a truncated one (meshdata.py convention;
    fsync added because the threat here is power loss, not process crash)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)          # mode set BEFORE it becomes the live file
    os.replace(tmp, path)
```

Two deliberate choices:

- `fsync` is an extension of the meshdata.py pattern. meshdata guards
  against process death; we guard against power death. ext4's
  `auto_da_alloc` usually flushes data on rename-over-existing anyway, but
  that is a mount-option accident, not a contract. Three fsyncs a minute of
  ~1 KB is nothing to the SD.
- The tmp file MUST be a sibling in `/var/lib/halow` — `os.replace` across
  filesystems raises EXDEV, and `/tmp` is a different filesystem.

Applied to: the stations prune (replaces line 130), the metrics prune
(replaces line 139), and the state write (replaces lines 144-145). The two
*appends* (99-100, 124-126) stay appends: a torn append costs at most one
trailing line, which every reader already tolerates, and converting them to
rewrites would multiply SD wear for nothing.

Tolerant reader for a torn legacy file: `load_state()` merges over defaults
so a valid-but-partial dict can never KeyError the healer:

```python
DEFAULTS = {"ap_restarts": 0, "dnsmasq_restarts": 0, "eth0_bounces": 0,
            "ap_down_streak": 0, "upstream_down_streak": 0,
            "last_eth0_bounce": 0, "disk_low": False, "disk_low_events": 0,
            "actions": []}

def load_state():
    try:
        return {**DEFAULTS, **json.load(open(STATE))}
    except Exception:
        return dict(DEFAULTS)
```

### 2. logrotate drop-in

New file `config/logrotate-halow`, installed to `/etc/logrotate.d/halow`:

```
/var/lib/halow/station-events.log {
    maxsize 1M
    rotate 3
    compress
    delaycompress
    missingok
    notifempty
    create 0644 halow-ui halow-ui
}
/var/lib/halow/throughput.jsonl {
    maxsize 256k
    rotate 2
    missingok
    notifempty
    create 0644 halow-ui halow-ui
}
```

Bound math (the capture-cap culture wants the number, not "should be fine"):
worst case with #18 landed is ~10 classified events/min × ~80 bytes × 1440
min = ~1.1 MB/day. Pi OS runs logrotate daily, so the live file peaks around
2 MB before rotation catches it; 1 live + 3 rotated ≈ 5 MB ceiling. [C —
projection; measure at first real cadence.] `delaycompress` keeps the newest
rotated file plain text so #16's join forensics can read it without gunzip.
`create` (not copytruncate) is correct ONLY because halow-sta-events opens
per event — flagged in Risks.

### 3. journald caps

New file `config/journald-halow.conf`, installed to
`/etc/systemd/journald.conf.d/halow.conf`:

```
[Journal]
SystemMaxUse=100M
RuntimeMaxUse=32M
```

deploy.sh restarts `systemd-journald` after install and runs a one-time
`journalctl --vacuum-size=100M` so the cap takes effect immediately rather
than at next rotation. 100M leaves room for #16's raised hostapd/dnsmasq
verbosity; #16 must size within it.

### 4. Disk low-water in the sampler and the API

halow-mon gains a per-sample disk reading and an *edge-triggered* event
(bounded: one log line per crossing, not one per minute):

```python
DISK_LOW_MB = 512   # SD low-water; mirrored in halow_ui.py

vfs = os.statvfs("/")
free_mb = vfs.f_bavail * vfs.f_frsize // 1048576
sample["disk_free_mb"] = free_mb          # joins the metrics.jsonl record
low = free_mb < DISK_LOW_MB
if low and not st.get("disk_low"):
    act(st, "disk_low_events",
        f"disk free {free_mb}MB below {DISK_LOW_MB}MB low-water")
st["disk_low"] = low
```

`/api/system` replaces the raw `df` token list with a structured object (the
only consumer is the bundled Overview card, updated in the same commit):

```json
"disk": {"total_mb": 29500, "free_mb": 24100, "used_pct": 18.3,
         "low": false, "low_water_mb": 512}
```

`/api/metrics` summary gains the low-water aggregate, same shape and
placement as `mem_low_water_kb` (halow_ui.py:819-820):

```json
"summary": {"disk_free_low_water_mb": 23980, "...": "existing keys unchanged"}
```

**Privilege model**: no sudoers change (`config/sudoers-halow-ui` untouched).
halow-mon already runs as root; `os.statvfs` and the structured disk read in
the UI need no privilege; logrotate runs as root from the stock system timer;
the drop-ins are installed by deploy.sh's existing root-over-ssh block. No
secrets touch any of these files, and the new logger line carries numbers
only — the SAE PSK has leaked twice through "harmless" echoes, so no new code
path echoes env or config.

## Implementation steps

Each step is one commit; paths are repo-relative.

1. **`scripts/halow-mon`: helper + tolerant reader.** Add `atomic_write()`
   and `DEFAULTS` as specified above; rewrite `load_state()` as the
   defaults-merge. Add `"disk_low": False, "disk_low_events": 0` to
   DEFAULTS. No call sites changed yet — pure addition, trivially
   revertible.
2. **`scripts/halow-mon`: convert the three rewrite sites.** Line 130 →
   `atomic_write(stations_log, "".join(lines[-RING_LINES * 2:]))`; line 139
   → same for `METRICS` with `lines[-RING_LINES:]`; lines 144-145 →
   `atomic_write(STATE, json.dumps(st))`. Delete the now-redundant
   `os.chmod` at old lines 133 and 142 (the helper sets 0o644).
3. **`scripts/halow-mon`: disk sampling + edge-triggered low event.** Add
   `DISK_LOW_MB = 512` beside `RING_LINES`; add `disk_free_mb` to the
   sample dict (lines 88-98 region) and the crossing logic from Design 4
   before the state write.
4. **`ui/halow_ui.py`: structured `/api/system` disk.** In `api_system()`
   (line 918), replace the `"disk"` df line with an `os.statvfs("/")`
   computation producing the JSON object above (`DISK_LOW_MB = 512`
   module-level, comment pointing at halow-mon's copy). Update the Overview
   card at line 1182 to `root ${s.disk.free_mb} MB free` with the existing
   `warn`/`bad` class when `s.disk.low`.
5. **`ui/halow_ui.py`: `/api/metrics` disk aggregates.** In `api_metrics()`
   add `"disk_free_mb"` to the per-key min/max/now loop tuple (line 814)
   and `summary["disk_free_low_water_mb"] = min(...)` beside
   `mem_low_water_kb`. Add a `disk low-water` stat to `monCard()`
   (~line 1184) next to the mem low-water stat.
6. **`config/logrotate-halow`: new file** with the stanza from Design 2.
   deploy.sh: add `sudo install -m644 config/logrotate-halow
   /etc/logrotate.d/halow` beside the dnsmasq/chrony installs (lines
   61-62).
7. **`config/journald-halow.conf`: new file** with the `[Journal]` caps.
   deploy.sh: `sudo mkdir -p /etc/systemd/journald.conf.d && sudo install
   -m644 config/journald-halow.conf
   /etc/systemd/journald.conf.d/halow.conf`, then `sudo systemctl restart
   systemd-journald` and `sudo journalctl --vacuum-size=100M` in the same
   block (idempotent on redeploy).
8. **`docs/feature-roadmap.md`: close item 22** with the DONE marker and
   one evidence line (measured numbers from the acceptance tests below),
   matching the house style of items 1-15.

## Surface changes

**API endpoints**

| Endpoint | Change |
|---|---|
| `GET /api/system` | `disk` field CHANGES SHAPE: `df` token list → `{total_mb, free_mb, used_pct, low, low_water_mb}`. Breaking for any external consumer of the token list; only known consumer is the bundled UI, updated same commit. |
| `GET /api/metrics` | NEW summary keys: `disk_free_mb` (min/max/now) and `disk_free_low_water_mb`; NEW per-sample field `disk_free_mb`. Additive. |

**halowctl commands** — none added or changed.

**UI elements**

| Element | Change |
|---|---|
| Overview "kernel / disk" card | Shows `free_mb`, colored warn when `disk.low`. |
| Overview monitor card | New "disk low-water" stat beside mem low-water. |

**systemd units** — none added or changed (`halow-mon.service`/`.timer`
unchanged; journald gets a conf drop-in, not a unit).

**Config files**

| File | Installs to | Purpose |
|---|---|---|
| `config/logrotate-halow` (new) | `/etc/logrotate.d/halow` | size-capped rotation: station-events.log (1M×3), throughput.jsonl (256k×2) |
| `config/journald-halow.conf` (new) | `/etc/systemd/journald.conf.d/halow.conf` | `SystemMaxUse=100M`, `RuntimeMaxUse=32M` |
| `config/sudoers-halow-ui` | — | UNCHANGED (no new root actions for the UI) |

**State files** (`/var/lib/halow`) — shapes: `mon-state.json` gains
`disk_low` (bool) and `disk_low_events` (int); `metrics.jsonl` samples gain
`disk_free_mb`. Transient `*.tmp` siblings exist only inside a single
halow-mon run.

## Testing & acceptance criteria

Everything measured, receiver-side (read the file/API afterward — never
trust the writer's return), and bounded. Mark results [M] in the roadmap
close-out.

### Testable today (pre-association)

1. **Reproduce the defect first** (proves the test can fail): on the Pi,
   `echo -n '{"ap_restarts":3' | sudo tee /var/lib/halow/mon-state.json`,
   run `sudo /usr/local/bin/halow-mon` with the OLD code → defaults reset
   (counters gone). Then `echo -n '{"ap_restarts":3}' | sudo tee ...` →
   KeyError traceback at the down-streak increment. Record both. [M]
2. **Tolerant reader**: same two seeds against the NEW code → no traceback;
   `ap_restarts` preserved as 3 in the valid-partial case; missing keys
   filled from DEFAULTS; the run's own state write leaves valid JSON
   (verify with `python3 -m json.tool`). PASS = both seeds survive.
3. **Atomicity under observation**: in one shell, `while :; do s=$(stat
   -c%s /var/lib/halow/mon-state.json 2>/dev/null||echo GONE); { [ "$s" =
   GONE ] || [ "$s" -eq 0 ]; } && echo TORN; done`; in another, run
   halow-mon 20 times. PASS = zero TORN lines and no `*.tmp` file remains
   after the last run (`ls /var/lib/halow/*.tmp` → none).
4. **Write ordering is real, not assumed**: `sudo strace -f -e
   trace=openat,fsync,rename /usr/local/bin/halow-mon 2>&1 | grep -B1 -A1
   mon-state` — must show tmp opened, fsync, then rename onto the live
   name. PASS = that ordering for all three managed files that fire. [M]
5. **Ring prune atomic + exact**: seed
   `python3 -c "print('\n'.join('{\"t\":%d}'%i for i in range(3100)))" |
   sudo tee /var/lib/halow/metrics.jsonl >/dev/null`, run halow-mon once.
   PASS = file has exactly `RING_LINES` (2880) + 1 fresh sample lines,
   every line parses, mode 0644. (The stations prune shares the code path
   but sits behind `if dump:` — its end-to-end test needs a station.)
6. **logrotate**: `sudo logrotate -d /etc/logrotate.d/halow` (dry run, no
   errors); seed 2 MB into station-events.log, `sudo logrotate -f
   /etc/logrotate.d/halow`; PASS = `.1` file exists, live file recreated
   `0644 halow-ui:halow-ui`, and a manual event — `sudo -u halow-ui
   /usr/local/bin/halow-sta-events halow0 TEST-EVENT 00:11:22:33:44:55` —
   appends to the fresh file and shows up in `GET /api/halow/events`.
7. **journald caps**: `systemd-analyze cat-config systemd/journald.conf |
   grep -i maxuse` shows both values; after the vacuum, `journalctl
   --disk-usage` ≤ 100M. [M]
8. **`/api/system` shape**: authenticated curl → `disk` object present;
   `free_mb` within 5% of `df -m / ` output taken in the same minute;
   Overview card renders it.
9. **Low-water crossing, edge-triggered**: on the Pi copy only, raise
   `DISK_LOW_MB` above current free in BOTH halow-mon and halow_ui.py, run
   halow-mon twice. PASS = exactly ONE `disk free ...` line in `journalctl
   -t halow-mon`, `disk_low: true` + `disk_low_events: 1` in
   mon-state.json, `/api/system` reports `low: true`. Then RESTORE the
   constant and verify the recovery edge (a control that drifts is the
   bug — the restore is part of the test).

### Needs a joined station

10. **stations.jsonl end-to-end**: with ≥1 associated ESP32, confirm
    per-minute entries accumulate; once past `RING_LINES * 4` (11520)
    lines, the prune fires and criteria from test 3 hold for
    stations.jsonl. (Reaching the threshold naturally takes days; seeding
    the file and letting one real sample trigger the prune is acceptable —
    the `if dump:` gate needs the real station.)
11. **Real-cadence rotation**: after a day of genuine join/leave traffic
    (plus #18's transitions if landed), station-events.log rotates on the
    daily logrotate run; `/api/halow/events` and #16's consumers still
    read the live file; measured daily growth recorded against the ~1.1
    MB/day projection, converting it from [C] to [M].
12. **24 h unattended soak**: metrics.jsonl gap-free (~1440 new samples/
    day), `journalctl --disk-usage` stable under the cap, zero `*.tmp`
    litter, `disk_free_low_water_mb` reported in `/api/metrics`.

## Out of scope

- **Hardware watchdog, brownout ledger, undervoltage-gated behavior** —
  that is #24. This issue makes a brownout non-destructive to state; it
  does not detect or react to power quality.
- **Off-device backup** (#27) — this keeps the SD tidy and consistent, not
  safe from SD death.
- **Config knob for `DISK_LOW_MB`** — a constant (512) in two files,
  cross-referenced by comment. Promote to `/etc/halow/halow.env` only if
  field SD sizes actually vary.
- **Hourly logrotate timer** — daily + `maxsize` bounds the file at ~2 MB
  worst case (math in Design 2); not worth a unit.
- **Making the appends transactional** — torn trailing lines in the jsonl
  files remain possible and remain tolerated by every reader.
- **Compressing or archiving the metrics/stations rings** — they are
  already bounded by line count.

## Risks & gotchas

- **EXDEV**: `os.replace` requires same-filesystem source and target. The
  tmp file must be a sibling in `/var/lib/halow`; using the scratchpad or
  `/tmp` fails at runtime on a stock image.
- **Permission window**: halow-mon runs as root writing into a
  `halow-ui`-owned directory (deploy.sh:70). If the helper forgot the
  chmod-before-replace, the live file could surface root-0600 and the UI's
  monitor card would go silently blank (its reader swallows OSError). The
  helper sets 0644 on the tmp, so the world-readable guarantee holds at
  every instant — keep it that way in review.
- **logrotate `create` vs `copytruncate`**: `create` is safe today only
  because halow-sta-events opens the log per event and exits
  (halow-sta-events.service is just the hostapd_cli dispatcher). If #16
  replaces the hook with a long-lived writer holding the fd, switch that
  stanza to `copytruncate` or events will land in the rotated ghost file.
- **`/api/system` shape break**: any out-of-repo script parsing
  `disk[3]` breaks. Grep found no consumer beyond the bundled UI, but say
  so in the commit message.
- **journald restart in deploy**: momentary logging gap; acceptable during
  a deploy, so it lives in deploy.sh — never restart journald from
  halow-mon's heal path.
- **Do not "fix" the appends into rewrites**: converting lines 99-100 or
  124-126 to full rewrites would turn a one-line torn-write risk into a
  whole-file risk and multiply SD wear 60-fold. The helper is for files
  that are already whole-file writes.
- **Sequencing with #18/#20**: both extend halow-mon's per-minute work. If
  they land first, their writes inherit the `open(w)` pattern and this
  issue grows. Land steps 1-2 before either.
- **#16 journal volume**: raised hostapd logger level and dnsmasq log-dhcp
  will eat into the 100M cap; #16 should check `journalctl --disk-usage`
  in its acceptance pass rather than assuming headroom.
- **Secrets discipline**: none of the touched files carry secrets, and the
  single new logger line emits two integers. Keep it that way — the SAE
  PSK has leaked twice via echoes that looked harmless, and `halowctl
  diff` deliberately compares env by key set only. Nothing in this issue
  may print env values.
- **Bench lesson driving the whole issue**: the brownout evidence is
  node-side (shared VBUS on the powered hub, two boards down —
  hardware.md:24-26), but the gateway shares the same bench power culture
  and the MM6108 TX-bursts on 3V3 (roadmap item 4). Treat power-loss
  mid-write as expected field behavior, not a freak event.
