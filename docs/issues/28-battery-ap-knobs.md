# 28. Battery-ready AP session knobs (idle limits, DTIM, deauth reasons, per-STA idle state)
> Tier 4 - product-path | Effort: small | Impact: medium | Depends on: none (shares hostapd-journal parsing with #16)

## Problem

The gateway is a Pi 4 running a Morse Micro MM6108 802.11ah (HaLow) AP —
SSID `mesh`, 10.117.0.1/24 — that ESP32 Meshtastic nodes (Heltec V4.2,
same MM6108 chip as STA) are about to join. The vendored Morse hostapd
fully supports the session-lifetime knobs a sleeping battery client
needs — `ap_max_inactivity`, `bss_max_idle`, `max_acceptable_idle_period`
(the S1G long-idle grant), `dtim_period` — but `halowctl gen` writes none
of them. Every station therefore lives under hostapd's compiled-in
default: a 300 s inactivity limit (`AP_MAX_INACTIVITY (5 * 60)`,
sta_info.h:343).

This is **not a day-one outage**, and the issue says so deliberately
(verifier correction to the original audit): before kicking, hostapd
sends a null-data poll and an *awake* station hardware-ACKs it, which
resets the timer (sta_info.c:657-666, poll TX at :680-683). Today the
node firmware pins power-save off — mesh-v4 patch
`0048-halow-transport-rung.patch` line 133 forces
`mmwlan_set_power_save_mode(MMWLAN_PS_DISABLED)` — so an idle node stays
awake, ACKs the poll, and survives. The risk activates the day node
power-save lands, and it must land: the asset-tag budget in
mesh-v4/ROADMAP.md (battery table, lines 308-311) models ~1.7 years on
3000 mAh at 4.97 mAh/day, which only works if the STA sleeps almost
always. The node's HaLow joinability check runs every `halow_check_min`
minutes — default 30, range 5-1440 (mesh-v4 patch 0004, lines 2127 and
5617-5623) — versus a 300 s AP-side idle limit. A dozing node cannot ACK
a poll inside the 3 s disassoc window; sleep lands, and every sleeping
node is silently deauthed six times per check interval.

The second gap is forensic. When the AP removes a station, the
`hostapd_cli` action-script hook receives only `<iface> <event> <mac>` —
never a reason code — so `station-events.log` can say a node left but
not why. The first PS-enabled bench session will produce disconnects,
and the question that matters ("AP kicked it for inactivity" vs "node
died / node left cleanly") is answerable from the gateway alone only if
the deauth *reason* is recorded. Third gap: the link telemetry API has
no per-station idle/sleep visibility, so nobody can see a station
drifting toward its idle limit before the kick.

Build the AP side now, before node PS exists, so the gateway is ready
first and the first sleepy association is measured, not debugged.

## Current state

All line numbers re-verified this session. Vendored tarballs inspected
by extraction (`vendor/hostap-mm6108-2.0.1.tar.gz`,
`vendor/morse_driver-mm6108-2.0.1.tar.gz`,
`vendor/morse_cli-mm6108-2.0.1.tar.gz`).

**Gateway repo (`mesh-halow-raspberry-pi`):**

- `scripts/halowctl` `gen()` (lines 48-85) writes
  `/etc/halow/hostapd_s1g.conf` with interface/ssid/hw_mode/channel/
  op_class/SAE/ctrl_interface only (heredoc at 63-83). No
  `ap_max_inactivity`, `bss_max_idle`, `max_acceptable_idle_period`, or
  `dtim_period` anywhere in `scripts/`, `config/`, `ui/`, `systemd/`
  (grep verified, zero hits).
- `halowctl set` (lines 105-117) whitelists exactly
  `channel|width|ssid`, seds `/etc/halow/halow.env`, then one
  `gen` + `systemctl restart halow-ap`. Note: `sed s/^KEY=.*/` only
  rewrites keys that already exist; the `mode` handler shows the
  append-if-missing pattern (line 124).
- `scripts/halow-sta-events` (entire file, 7 lines): appends
  `date -Is $2 $3` to `/var/lib/halow/station-events.log` and calls
  `logger`. Runs as `User=halow-ui` under
  `systemd/halow-sta-events.service` (line 8), hooked via
  `hostapd_cli_s1g ... -a` (line 10). Reason codes never reach it —
  hostapd ctrl events carry event + MAC only.
- `ui/halow_ui.py` `/api/halow/link[/<mac>]` (lines 742-786): serves
  min/avg/max rate, signal, delivery%, retry% from
  `/var/lib/halow/stations.jsonl`. No idle or PS fields.
- `scripts/halow-mon` (lines 104-133): per-minute `iw station dump`
  parse into `stations.jsonl` — signal, bitrates, tx packet counters
  only. Runs as root via `halow-mon.timer`.
- `config/sudoers-halow-ui` line 6 already grants
  `halowctl set *` — the UI needs no new sudo for this feature.
- `scripts/deploy.sh` line 51 creates `halow-ui` in group
  `systemd-journal` — the events script can read the `halow-ap` journal
  without privilege changes.
- `halowctl diff` (lines 207-210) compares env by KEY SET only against
  `config/halow.env.example` — new env keys must be added to the
  example or every synced device reports drift.

**Vendored hostapd (`hostap-mm6108-2.0.1`):**

- `hostapd/config_file.c:2367-2368` parses `ap_max_inactivity`
  (seconds); `:2373-2381` `bss_max_idle` (0-2, rejects others);
  `:2382-2383` `max_acceptable_idle_period`; `:3301-3309` `dtim_period`
  (1-255).
- Defaults: `src/ap/ap_config.c:99` `ap_max_inactivity =
  AP_MAX_INACTIVITY` = 300 s (`src/ap/sta_info.h:343`); `:100`
  `bss_max_idle = 1` — the BSS Max Idle IE is **already advertised** in
  assoc responses with the 300 s value (`src/ap/ieee802_11.c:5713`,
  built in `src/ap/ieee802_11_shared.c:757-787`); `:93`
  `dtim_period = 2`. What's missing is any way to *change* the period.
- The S1G long-idle path: a STA may request an idle period in its assoc
  request; hostapd grants it only if `max_acceptable_idle_period` is
  set (`src/ap/ieee802_11.c:5071-5082` — request and config are in
  units of 1000 TUs ≈ 1.024 s, NOT seconds). A granted period overrides
  the AP limit per-STA: `src/ap/sta_info.c:593-594`,
  `max_inactivity = (max_idle_period * 1024 + 999) / 1000` seconds.
- Kick sequence (`src/ap/sta_info.c` `ap_handle_timer`): inactivity
  check at :580-656, null-data poll at :680-683, ACK-rescue at
  :657-666, then INFO-level `"disassociated due to inactivity"`
  (:733-734) and `"deauthenticated due to inactivity (timer
  DEAUTH/REMOVE)"` (:748-750). **Spec correction:** the original audit
  cited sta_info.c:587 — that line is the "local deauth request"
  message; the inactivity lines are :733-734 and :748-750.
- **Second spec correction — no logger raise needed for these lines:**
  default logger levels are already INFO (`src/ap/ap_config.c:57-58`),
  `hostapd_logger_cb` passes level ≥ INFO to stdout
  (`hostapd/main.c:116-118`), and `halow-ap.service` runs
  `hostapd_s1g` plainly (stdout → journal). The inactivity kick lines
  land in `journalctl -u halow-ap` at stock levels, formatted
  `halow0: STA <mac> IEEE 802.11: <text>` (main.c:94-97). What DOES
  need the #16 debug raise (`-d` on ExecStart; `wpa_debug_level`
  default is MSG_INFO, `src/utils/wpa_debug.c:30`) is the
  STA-*originated* reason codes: `"deauthentication: STA=<mac>
  reason_code=N"` is MSG_DEBUG (`src/ap/ieee802_11.c:6518-6520`), as is
  disassoc (`:6542-6544` — note hostapd's actual spelling is
  `disassocation:`, grep for that). A STA-sent *disassoc* also logs
  INFO `"disassociated"` (:6551-6552) but a STA-sent *deauth* logs only
  DEBUG (:6528-6529).
- Per-STA introspection already exists on the ctrl socket:
  `STA <mac>` (`src/ap/ctrl_iface_ap.c:516`) returns `inactive_msec`
  (:95-98), `listen_interval` (:263-265), `timeout_next=NULLFUNC
  POLL|DISASSOC|DEAUTH|...` (:279-280 via :199-215), and
  `max_idle_period=N` when granted (:285-291), plus flags including
  `[PENDING_POLL]` (`src/ap/sta_info.c:1915-1935`). The socket group is
  already `halow-ui` (`halowctl` gen line 82).

**Third spec correction — there is no "PS field" in `iw station dump`.**
Upstream `iw` has no per-STA power-save field, and the Morse driver
declares `AP_LINK_PS` (`morse_driver mac.c:7223`) while never calling
`ieee80211_sta_ps_transition` (grep across all driver .c files: zero
hits) — the chip firmware handles STA sleep buffering below the host,
so host-side mac80211 never learns per-STA PS state. `morse_cli stats`
"STA PS state" (`stats_format_regular.c:368-369`) is the *local chip's*
MAC state, not a remote station's. The honest, observable surface is
hostapd's per-STA idle machinery above — which answers the actual
question ("how close is this station to being kicked, and what idle
period was it granted") from the receiver side.

**Client repo (`mesh-v4`):** PS pinned off at patch 0048:133;
`halow_check_min` default 30 min (patch 0004:5620); asset-tag budget
ROADMAP.md:308-311 (`~1.7 years` is a *model* [C], the roadmap says so —
the mismatch against 300 s is arithmetic, not a claim).

## Design

Three small pieces, one commit each plus config/plumbing. No new
services, no new sudoers entries.

**1. Session knobs via env → `halowctl gen`.** Four new optional keys in
`/etc/halow/halow.env` (empty = omit the conf line = hostapd default):

| env key | conf key | unit / range | default when unset |
|---|---|---|---|
| `HALOW_AP_MAX_INACTIVITY` | `ap_max_inactivity` | seconds, 60-86400 | 300 |
| `HALOW_BSS_MAX_IDLE` | `bss_max_idle` | 0/1/2 | 1 |
| `HALOW_MAX_IDLE_TU` | `max_acceptable_idle_period` | units of 1000 TUs (≈1.024 s), 1-65535 | 0 (never grant) |
| `HALOW_DTIM_PERIOD` | `dtim_period` | beacons, 1-255 | 2 |

`gen()` appends the lines after the existing heredoc, conditionally.
`halowctl set` gains the keys `inactivity|bss_max_idle|max_idle|dtim`
with validation *before* touching the env, and an append-if-missing
helper so `set` works on deployments whose env predates the keys.
Units are exposed as-is: `max_idle` is in 1000-TU units because that is
what hostapd compares against the STA request
(ieee802_11.c:5075-5077) — converting to seconds in the CLI would bake
in a silent ×1.024 lie.

Documented battery preset (a command, not a profile — RF profiles pick
channel/width; session lifetime is orthogonal and a profile matrix
would multiply both):

```
halowctl set inactivity=5400 bss_max_idle=1 max_idle=5273 dtim=10
```

5400 s = 3× the node's 30-min default check interval (the nodewatch
"3× expected interval" freshness rule); 5273 ≈ 5400/1.024 so a node
requesting the full period gets it granted; `dtim=10` ≈ 1.02 s
broadcast buffering at the default 100 TU beacon interval [C —
default, unmeasured on air]. `bss_max_idle=2` (protected keep-alive
required, ieee80211_shared.c:783) stays out of the preset until
measured against the node firmware.

**2. Disconnect reason capture in `halow-sta-events`.** On
`AP-STA-DISCONNECTED`, the action script (already `halow-ui`, already
in `systemd-journal`) sleeps 1 s (the ctrl event is emitted from
`ap_sta_set_authorized` *before* the INFO logger line in the disassoc
path — sta_info.c:723 vs :733 — so give journald the write), then runs
one bounded `journalctl -u halow-ap --since "-3 min"` query, greps for
the MAC, and extracts the reason **by pattern only** — never appending
raw journal text to the log (the SAE PSK has leaked twice through
"harmless" echoes; a pattern-extract cannot carry arbitrary text).
Appended log line format (backward compatible — a fourth
space-separated field):

```
2026-08-05T21:14:03-07:00 AP-STA-DISCONNECTED aa:bb:cc:dd:ee:ff reason="disassociated due to inactivity"
```

Coverage is tiered and the issue is honest about it: AP-side
inactivity kicks are visible at stock log levels today;
STA-originated `reason_code=N` lines appear only once #16 adds `-d` to
`halow-ap.service`. Until then a clean node deauth yields
`reason=""` — absence is itself the classifier (no inactivity line +
no reason line = node-initiated or died; inactivity line = AP kicked
it). The grep patterns live in this script and #16 reuses them.

**3. Per-STA idle state in `stations.jsonl` → `/api/halow/link`.**
`halow-mon` (root) already loops station MACs each minute; for each it
additionally runs `hostapd_cli_s1g -p /var/run/hostapd_s1g -i halow0
sta <mac>` (5 s timeout, capped at 16 stations/cycle) and merges four
fields into the existing per-station JSONL entry: `inactive_ms`,
`listen_int`, `max_idle_tu` (absent if not granted), `timeout_next`,
`pending_poll`. `/api/halow/link` derives an `idle` block per station:

```json
{
  "stations": {
    "aa:bb:cc:dd:ee:ff": {
      "now": {"t": 1754500000, "mac": "aa:bb:cc:dd:ee:ff",
              "signal_dbm": -62, "tx_mbps": 6.5,
              "inactive_ms": 12040, "listen_int": 10,
              "max_idle_tu": 5273, "timeout_next": "NULLFUNC POLL",
              "pending_poll": false},
      "n_samples": 240,
      "tx_mbps": {"min": 0.3, "avg": 5.1, "max": 7.2},
      "signal_dbm": {"min": -70, "avg": -63.2, "max": -58},
      "delivery_pct": 99.1,
      "retry_pct": 3.4,
      "idle": {
        "inactive_s": 12.0,
        "limit_s": 5400,
        "limit_source": "sta-negotiated",
        "used_pct": 0.2,
        "listen_interval": 10,
        "timeout_next": "NULLFUNC POLL"
      }
    }
  }
}
```

`limit_s` resolution order: granted `max_idle_tu` → `ceil(tu × 1.024)`
seconds (mirrors sta_info.c:594, `limit_source: "sta-negotiated"`);
else `HALOW_AP_MAX_INACTIVITY` from env (`"ap-config"`); else 300
(`"ap-default"`). `used_pct = 100 × inactive_s / limit_s` — the
"about to be kicked" gauge.

**Privilege model:** halow-mon samples as root (existing timer);
halow-sta-events reads the journal as halow-ui via existing
systemd-journal membership; the UI reads files and calls
`sudo halowctl set` which sudoers line 6 already permits. Zero
sudoers changes. Secrets: env values other than the four new keys are
never read into responses; reason strings are pattern-extracted; the
generated conf (contains `sae_password`) is never echoed — `gen`'s
summary line stays ssid/ch/op/width only.

## Implementation steps

1. **`config/halow.env.example`** — add the four keys, empty, with a
   comment block stating units (esp. `HALOW_MAX_IDLE_TU` is 1000-TU
   units ≈ 1.024 s, not seconds) and the battery preset one-liner.
   Required for `halowctl diff` key-set comparison (halowctl:207-210).
2. **`scripts/halowctl` `gen()`** — after the `$CONF` heredoc (line 83),
   append conditionally:
   ```bash
   [ -n "${HALOW_AP_MAX_INACTIVITY:-}" ] && echo "ap_max_inactivity=${HALOW_AP_MAX_INACTIVITY}" >> "$CONF"
   [ -n "${HALOW_BSS_MAX_IDLE:-}" ]      && echo "bss_max_idle=${HALOW_BSS_MAX_IDLE}" >> "$CONF"
   [ -n "${HALOW_MAX_IDLE_TU:-}" ]       && echo "max_acceptable_idle_period=${HALOW_MAX_IDLE_TU}" >> "$CONF"
   [ -n "${HALOW_DTIM_PERIOD:-}" ]       && echo "dtim_period=${HALOW_DTIM_PERIOD}" >> "$CONF"
   ```
   Extend the `echo "generated ..."` summary with
   `idle=${HALOW_AP_MAX_INACTIVITY:-300} dtim=${HALOW_DTIM_PERIOD:-2}`
   (values only, never the conf body).
3. **`scripts/halowctl` `set`** — add an `ensure_key()` helper
   (`grep -q "^$1=" "$ENV" || echo "$1=" | sudo tee -a "$ENV"`), then
   four case arms with validation before sed; empty value clears (back
   to hostapd default), matching the `channel=` convention:
   - `inactivity`: empty or 60-86400
   - `bss_max_idle`: empty or 0|1|2 (config_file.c:2375-2381 rejects
     others at hostapd level — fail earlier, in the CLI)
   - `max_idle`: empty or 1-65535
   - `dtim`: empty or 1-255
   Update the `unknown key` usage string and the header comment
   (lines 6, 113).
4. **`scripts/deploy.sh`** — after line 56 (env perms), migrate
   existing deployments: for each of the four keys,
   `sudo grep -q "^KEY=" /etc/halow/halow.env || echo "KEY=" | sudo tee -a ...`.
   Keeps `halowctl diff` quiet on freshly deployed devices.
5. **`scripts/halow-sta-events`** — on `AP-STA-DISCONNECTED` with a MAC:
   `sleep 1`, then
   ```bash
   REASON=$(journalctl -u halow-ap --since "-3 min" --no-pager -q 2>/dev/null \
     | grep -iF "$3" \
     | grep -oE '(deauthenticated|disassociated) due to [a-zA-Z ()/]+|(deauthentication|disassocation): STA=[0-9a-f:]+ reason_code=[0-9]+' \
     | tail -1)
   ```
   and emit `reason="$REASON"` as a fourth field when non-empty. Keep
   the connect path untouched (no journal call, no sleep on joins).
6. **`scripts/halow-mon`** — in the per-station loop (after line 123),
   per MAC:
   `info = sh(f"hostapd_cli_s1g -p /var/run/hostapd_s1g -i halow0 sta {mac}", timeout=5)`
   (cap: `entries[:16]`), parse `inactive_msec` → `inactive_ms` (int),
   `listen_interval` → `listen_int`, `max_idle_period` → `max_idle_tu`,
   `timeout_next` → string, `[PENDING_POLL` in flags line →
   `pending_poll`. Skip silently if output is empty (hostapd down —
   absence over invention).
7. **`ui/halow_ui.py` `/api/halow/link`** (lines 764-782) — build the
   `idle` block from the last sample plus
   `load_kv(ENV_CONF).get("HALOW_AP_MAX_INACTIVITY")` with the
   resolution order above; omit the block when the sample has no
   `inactive_ms` (old rows, absent station).
8. **`ui/halow_ui.py` `/api/halow/set`** (line 258) — extend the form
   key tuple to
   `("channel", "width", "ssid", "inactivity", "bss_max_idle", "max_idle", "dtim")`.
   halowctl validates; sudoers line 6 already covers `set *`. No
   `confirm=1`: these are not identity changes (SSID/passphrase keep
   theirs), though the restart bounces stations like `set channel`
   already does.
9. **`docs/feature-roadmap.md`** — mark item 28's machinery done with
   date + what remains gated on a joined station (the [M] items below).

## Surface changes

| kind | item | change |
|---|---|---|
| halowctl | `set inactivity= bss_max_idle= max_idle= dtim=` | new keys, validated, empty clears; one gen+restart per call |
| halowctl | `gen` | emits up to 4 new conf lines; summary line gains idle/dtim values |
| config | `config/halow.env.example` | +4 keys (empty defaults) |
| config | `/etc/halow/hostapd_s1g.conf` | generated: up to 4 new lines |
| API | `POST /api/halow/set` | accepts 4 new form fields |
| API | `GET /api/halow/link[/<mac>]` | per-station `idle` block; `now` gains `inactive_ms`, `listen_int`, `max_idle_tu`, `timeout_next`, `pending_poll` |
| file | `/var/lib/halow/stations.jsonl` | rows gain the same 5 fields (additive) |
| file | `/var/lib/halow/station-events.log` | disconnect lines gain `reason="..."` fourth field (additive) |
| script | `scripts/halow-sta-events` | journal reason lookup on disconnect |
| script | `scripts/halow-mon` | per-STA hostapd_cli `sta` sample |
| script | `scripts/deploy.sh` | env key migration |
| systemd | — | none (no new units, no ExecStart changes — the `-d` raise belongs to #16) |
| sudoers | — | none (`halowctl set *` already whitelisted, config/sudoers-halow-ui:6) |
| UI | — | none required; events pane and link API surface the new data as-is |

## Testing & acceptance criteria

Bench culture applies: every check is a bounded command with an
expected observation; `[M]` = measured on the bench Pi, `[C]` = claimed
from code reading until then.

**Testable today (pre-association):**

1. `halowctl set inactivity=5400 bss_max_idle=1 max_idle=5273 dtim=10`
   → exactly one AP restart; `/etc/halow/hostapd_s1g.conf` contains the
   four lines; `systemctl is-active halow-ap` = `active`;
   `journalctl -u halow-ap -n 30` has no `Invalid`/`Line N:` parse
   error. This proves the vendored `hostapd_s1g` binary accepts the
   keys [M] — until run, config_file.c acceptance is [C].
2. Rejection before mutation: `halowctl set bss_max_idle=3`,
   `set dtim=0`, `set inactivity=10` each exit non-zero and
   `/etc/halow/halow.env` is byte-identical after (`sudo md5sum`
   before/after).
3. Clearing: `halowctl set inactivity=` → conf omits
   `ap_max_inactivity`; hostapd back to the 300 s default.
4. Drift discipline: after deploy, `halowctl diff` reports `no drift`;
   with a knob set locally it reports nothing about *values* (key-set
   comparison only — the passphrase rule).
5. Reason path, bounded and safe:
   `sudo -u halow-ui /usr/local/bin/halow-sta-events halow0 AP-STA-DISCONNECTED 02:00:00:00:00:01`
   completes in < 3 s, appends a line with no `reason=` field (no
   journal match), exit 0. Connect events
   (`... AP-STA-CONNECTED <mac>`) complete with no sleep and no
   journal call (time it: < 0.2 s).
6. Pattern fixtures: pipe the two real hostapd formats through the
   extraction grep —
   `halow0: STA aa:bb:cc:dd:ee:ff IEEE 802.11: disassociated due to inactivity`
   and
   `disassocation: STA=aa:bb:cc:dd:ee:ff reason_code=8` (hostapd's
   actual misspelling, ieee802_11.c:6543) — both must extract; an
   arbitrary line containing the MAC must extract nothing.
7. `/api/halow/link` with no stations → `200 {"stations": {}}`; with a
   hand-written `stations.jsonl` fixture row carrying
   `inactive_ms/max_idle_tu` → `idle` block present,
   `limit_s == ceil(max_idle_tu * 1.024)`, `used_pct` correct,
   `limit_source` follows the resolution order (delete `max_idle_tu`
   from the fixture → falls back to env → to 300).
8. Secrets: `grep -ci passphrase /var/lib/halow/station-events.log
   /var/lib/halow/stations.jsonl` = 0; `halowctl gen` stdout contains
   no `sae_password`; `/api/halow/link` response contains no env value
   other than the derived `limit_s`.

**Needs a joined station:**

9. [M] Poll survival (the "not urgent today" claim, measured): node
   associated, PS off (patch 0048 pins it), zero traffic for 8 min →
   station still in `iw dev halow0 station dump`, no inactivity line in
   the journal. With #16's `-d`: journal shows `has ACKed data poll`
   (sta_info.c:661).
10. [M] Kick forensics: hard-power the node off (bench switch), wait
    `limit_s + 60 s` → journal shows the INFO inactivity lines
    (sta_info.c:733-734, 748-750) and `station-events.log` gains
    `reason="disassociated due to inactivity"` within one event. This
    is the acceptance headline: *AP kicked it vs node died, answered
    from the gateway alone.*
11. [M] Receiver-side knob confirmation: `halowctl capture 10` (already
    3-30 s / 5000-frame bounded) during a node join → assoc response
    carries BSS Max Idle Period IE (tag 90) with the configured value;
    beacons carry the DTIM period set. Confirm at the receiver, not in
    the config file.
12. [M] Long-idle grant: if the node firmware requests an idle period,
    `hostapd_cli_s1g -p /var/run/hostapd_s1g -i halow0 sta <mac>` shows
    `max_idle_period=` and `/api/halow/link` reports
    `limit_source: "sta-negotiated"`. Whether the Morse STA stack
    requests it is unknown [C] — this test answers it.
13. [M] `idle.used_pct` sanity: silent node, watch two consecutive
    `/api/halow/link` samples → `inactive_s` grows ~60 s/sample and
    resets on traffic.

## Out of scope

- Node-side power-save enablement (mesh-v4 work; blocked on
  battery-only bench per ROADMAP item 15) and any node keep-alive
  scheduling.
- The hostapd `-d` logger raise and full join-stage forensics — that is
  #16; this issue only ships journal parsing that degrades gracefully
  without it.
- TWT (the driver ships `twt.h`) — a bigger S1G power lever, separate
  investigation once basic PS survives.
- Battery *profiles* (auto-switching knob sets per RF profile) — one
  documented preset command, nothing stateful.
- UI polish (idle gauges, kick-warning banners) — data lands in
  existing panes/APIs; visualization can follow usage.
- Presence/check-in contracts (#30) and per-station health ladder
  (#18) — they consume these fields, not the reverse.

## Risks & gotchas

- **Unit trap:** `max_acceptable_idle_period` and the granted
  `max_idle_period` are in 1000-TU units (≈1.024 s), compared raw
  against the STA request (ieee802_11.c:5075) and converted with
  `ceil(×1.024)` at sta_info.c:594. The CLI key is deliberately named
  `max_idle` with TU units documented — converting "helpfully" to
  seconds would make every displayed number 2.4% wrong somewhere.
- **A long idle limit delays death detection:** with `inactivity=5400`
  a dead node stays in `station dump` (and counts in halow-mon's
  `stations` metric) for up to 90 min. Dead-vs-quiet classification is
  exactly #18/#30's job; until then, expect the stations stat to lag
  reality after a node is pulled. Don't "fix" it by shortening the
  limit back — that reintroduces the sleep kick.
- **Event/journal race:** the ctrl disconnect event fires before the
  INFO logger line is flushed in the disassoc path (ap_sta_set_authorized
  at sta_info.c:723 precedes the logger at :733); hence the 1 s sleep
  and 3-min journal window. If reasons come up empty on the bench,
  widen the window before suspecting the pattern.
- **STA-sent deauth is DEBUG-only** (ieee802_11.c:6528-6529): until #16
  lands, a clean node leave shows `reason=""`. Do not read empty as
  "AP kicked it" — the inactivity kick always leaves an INFO line.
- **`bss_max_idle=2`** requires protected keep-alives from the STA
  (ieee80211_shared.c:783 sets the required bit). Untested against the
  node's MM6108 stack — enabling it blind could convert every
  keep-alive into a kick. Measure with one node before any preset
  includes it.
- **DTIM vs the ARP trap:** a dozing STA gets broadcasts only at DTIM;
  `dtim=10` legitimately delays ARP answers ~1 s. The Diag pane's
  "FAILED with a known mac" hint gains a benign cause once PS lands —
  worth a note in that pane's text when this bites.
- **sed-only `set` silently no-ops on missing keys** (existing
  pattern, halowctl:110-112 rewrites only present keys) — hence
  `ensure_key()` and the deploy.sh migration. Test on a device whose
  env predates this change.
- **AP restart on knob change bounces all stations** (same as
  `set channel`). Set all four knobs in one `halowctl set` call — the
  loop seds everything, then restarts once (halowctl:107-116).
- **Leak discipline:** the journal (especially post-#16 at `-d`) and
  `hostapd_cli sta` output are near the passphrase's home. Only
  pattern-extracted reasons and five named fields ever leave the
  scripts; never append raw journal or ctrl-socket text to logs or API
  responses. The PSK has leaked twice via echoes that looked harmless.
