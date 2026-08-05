# 16. Association forensics: per-MAC join transcript with stage-of-failure verdict

> Tier 1 - first-association | Effort: medium | Impact: high | Depends on: none (shares journal-parsing machinery with #28 — build the marker table so 28 extends it)

## Problem

A HaLow join can die at five distinct stages: SAE commit/confirm, 802.11
association, the 4-way EAPOL handshake, DHCP, and first ARP/ICMP. The
gateway currently records exactly one of those outcomes — total success.
`scripts/halow-sta-events` is a hostapd_cli action script that appends
whatever event the ctrl_iface forwards (line 6), and that channel carries
station *lifecycle* events (`AP-STA-CONNECTED`/`AP-STA-DISCONNECTED`) —
a half-completed SAE handshake produces no ctrl_iface event and therefore
no gateway record anywhere. The generated hostapd conf sets no logger
level, so the per-STA auth-flow records hostapd emits at DEBUG never
reach the journal either. dnsmasq runs without `log-dhcp`. When the
first join attempt fails, the gateway will have nothing to say about it.

This matters *now* because the client side cannot testify. The ESP32
node (mesh-v4, Heltec V4.2, same MM6108 silicon) has a proven driver
below RF, but RF-core bring-up wedges the chip's SPI slave on the bench
wiring — the stall runs 23+ seconds continuously and only a chip re-init
ends it (mesh-v4 `firmware/halow/README.md`, sessions 5-6). The first
failure burst lands deterministically ~1.6 s after "Attempting to
connect", i.e. potentially *mid-SAE*. When that happens, node-side
evidence vanishes with the bus. First association is blocked only on a
bulk decoupling capacitor + antenna confirmation; the first attempt could
happen any bench session, and this recorder must already be running —
a missed first failure is unrepeatable evidence lost.

The second failure class is credentials. `halow_psk` is a WRITE-ONLY
setting in node NVS, reported solely as a set/unset boolean
(mesh-v4 `docs/transport-ladder-halow.md:283-286` — the wifi_ssid lesson:
a readable network name poisons every settings snapshot). Nobody can read
the node's PSK back to check it. Only gateway-side SAE forensics can
distinguish wrong-PSK (SAE confirm exchange fails) from
RF-died-mid-handshake (silence after commit). Without this feature those
two failures are indistinguishable, and the bench will burn sessions
re-flashing credentials to chase a hardware fault, or re-soldering
capacitors to chase a typo.

This was the only roadmap-v2 feature proposed independently by all four
audit lenses, and it survived adversarial verification: no join-log
endpoint exists in `ui/halow_ui.py`, `halow-sta-events` has no failure
path, `halowctl gen` writes no logger level, `dnsmasq-halow.conf` has no
`log-dhcp`. The UI even documents the trap it cannot yet diagnose:
`ui/halow_ui.py:1047` — *"FAILED with a known mac = the 'associated but
answers no ARP' trap"* — a hint for a human reading a table, not a verdict.

## Current state

Verified against the working tree this session.

Gateway repo (`mesh-halow-raspberry-pi`):

- `scripts/halow-sta-events:6` — the entire event recorder:
  `printf '%s %s %s\n' "$(date -Is)" "$2" "${3:-}" >> "$LOG"`. It records
  whatever hostapd_cli forwards, and hostapd_cli's action interface
  forwards lifecycle events only. No SAE, EAPOL, or DHCP visibility.
- `systemd/halow-sta-events.service:8-10` — runs `hostapd_cli_s1g -a` as
  `User=halow-ui`, `BindsTo=halow-ap.service`, with an ExecStartPre loop
  waiting for the ctrl socket. This is the daemon pattern the new watcher
  copies.
- `scripts/halowctl:63-83` — `gen()` heredoc for
  `/etc/halow/hostapd_s1g.conf`: interface, hw_mode, channel/op_class,
  SAE keys, ctrl_interface. **No `logger_*` keys at all**, so hostapd_s1g
  runs at its compiled default (INFO); per-STA SAE commit/confirm records
  are emitted at DEBUG and are lost. Note `umask 077` at line 50 — the
  conf holds `sae_password` and is root-only.
- `config/dnsmasq-halow.conf:1-11` — interface bind, dhcp-range, options.
  No `log-dhcp`.
- `scripts/halowctl:296-305` — **trap the spec missed**: `dhcp-config`
  rewrites `/etc/dnsmasq.d/halow.conf` from its own heredoc ("Managed by
  halowctl dhcp-config — edits here are overwritten"). Adding `log-dhcp`
  only to the repo config means the first UI DHCP edit
  (`ui/halow_ui.py:433`, `api_config_dhcp`) silently reverts forensics.
  Both templates must change.
- `ui/halow_ui.py:563-572` — `GET /api/halow/events` tails the last 100
  lines of `/var/lib/halow/station-events.log`. Success-only, by the above.
- `ui/halow_ui.py:575-585` — `GET /api/logs?unit=` wraps journalctl,
  units whitelisted in `LOG_UNITS` (line 514, includes `halow-ap`,
  `dnsmasq`, `kernel`). It runs journalctl **without sudo** — works
  because deploy.sh:51 creates the `halow-ui` user with
  `-G systemd-journal`. The new machinery inherits this: journal parsing
  needs no new privilege.
- `ui/halow_ui.py:742-786` — `GET /api/halow/link[/<mac>]` is the
  existing per-MAC API shape (dict keyed by MAC, per-MAC drill-down URL);
  the join-log API mirrors it.
- `scripts/halow-first-contact.sh` + `systemd/halow-first-contact.service`
  — the one-shot evidence-bundle pattern: fires on interface appearance,
  appends a bounded snapshot (kernel probe lines, `iw` state, morse_cli)
  to a log. This issue builds the per-MAC analogue.
- `scripts/halowctl:163-169` — `capture` clamps to 3-30 s and 5000
  frames. Every bound below follows this pattern.
- `config/sudoers-halow-ui` — the UI's exact root surface. **This issue
  adds nothing to it** (see privilege model).
- `scripts/deploy.sh:51,61,66-70,81` — user/group creation, dnsmasq conf
  install, script/unit install lists, `/var/lib/halow` owned by
  `halow-ui`, enable list. All the wiring points for new files.

Client repo (`mesh-v4`):

- `firmware/halow/README.md`, sessions 5-6 (~lines 165-205): the fault is
  deterministic — first failure burst at the same driver timestamp ~1.6 s
  after "Attempting to connect", when `sta_enable` brings up the RF core;
  the post-bring-up SPI stall runs 23+ s and only re-init recovers;
  software space exhausted; blocking item is the decoupling capacitor.
  Consequence for us: on the very attempts we most need to diagnose, the
  node logs nothing usable. The gateway is the only reliable witness.
- `docs/transport-ladder-halow.md:283-286`: `halow_ssid`/`halow_psk` are
  write-only in NVS, surfaced as booleans. Wrong-PSK cannot be ruled out
  client-side, ever, by design.

## Design

Three small pieces of instrumentation plus one correlator, all bounded,
all unprivileged.

**1. Make hostapd talk.** `halowctl gen` adds a logger block to the
generated conf:

```
logger_syslog=-1
logger_syslog_level=2
logger_stdout=-1
logger_stdout_level=1
```

stdout goes to the journal via halow-ap.service, so level 1 (debugging)
there captures per-STA `hostapd_logger()` records — SAE commit/confirm RX
included — while syslog stays at INFO. Deliberately **not** the `-d`/`-dd`
command-line flags: those raise `wpa_printf` debugging, and at `-dd` that
path can dump EAPOL key material. The conf logger keys are the bounded,
secret-safe knob. hostapd_s1g is Morse's fork of hostapd; these are core
config-parser keys, but the fork must be verified to accept them (a
rejected key aborts hostapd at ExecStartPre-gen'd config load — see
Risks).

**2. Make dnsmasq talk.** `log-dhcp` in both templates (repo conf and the
`dhcp-config` heredoc). dnsmasq already logs the basic
DISCOVER/OFFER/REQUEST/ACK lines by default; `log-dhcp` adds per-option
detail that shows *which* step of an incomplete DHCP exchange died.

**3. Correlate.** New script `scripts/halow-join-log` (python3, stdlib
only, like halow-mon). One marker table maps journal lines to stages —
the table #28 will extend with deauth reason codes. Expected markers,
from upstream hostapd 2.10 lineage — **[C] until re-captured verbatim on
the bench; the parser must treat unmatched lines as raw events, never
crash, and the table is one place to fix**:

| stage | source | marker (substring) |
|---|---|---|
| `sae_commit` | halow-ap | `start SAE authentication (RX commit` |
| `sae_confirm` | halow-ap | `SAE authentication (RX confirm` (status captured) |
| `authenticated` | halow-ap | `IEEE 802.11: authenticated` |
| `associated` | halow-ap | `IEEE 802.11: associated (aid` |
| `eapol_done` | halow-ap | `pairwise key handshake completed` or `AP-STA-CONNECTED` |
| `wrong_psk_hint` | halow-ap | `AP-STA-POSSIBLE-PSK-MISMATCH` (if the fork emits it) or confirm status != 0 |
| `dhcp_discover/offer/request/ack` | dnsmasq | `DHCPDISCOVER(halow0)` … `DHCPACK(halow0)` |
| `reachable` | measured probe | not parsed — see below |

The seventh stage is **measured, not inferred**: on `dhcp_ack` the
watcher waits 5 s then probes the leased IP (`ping -c 3 -W 2 -n` +
`ip -j neigh`) and records the receiver-side result `[M]`. This automates
the `ui/halow_ui.py:1047` ARP trap.

**Verdicts** name the last stage reached and what it means:

| verdict | evidence | reading |
|---|---|---|
| `never_seen` | no lines for MAC | wrong SSID/channel, or RF never made it out |
| `silence_after_commit` | commit, then nothing for 30 s | RF died mid-handshake — matches the node's deterministic ~1.6 s SPI stall |
| `sae_confirm_failed` | confirm status != 0, or commit/confirm cycles ending in deauth, or PSK-mismatch event | wrong PSK — the verdict the write-only NVS design needs |
| `assoc_no_eapol` | associated, no handshake completion | driver/supplicant died post-assoc |
| `eapol_no_dhcp` | connected, no DISCOVER in 30 s | node IP stack never started (or static IP — probe checks anyway) |
| `dhcp_incomplete` | DISCOVER seen, no ACK | log-dhcp detail names the dead step |
| `dhcp_no_reach` | ACK, probe failed | the 1047 trap, now automated |
| `complete` | all stages + probe 3/3 | joined and answering `[M]` |

**Modes.** `halow-join-log` runs three ways:
- `--follow`: daemon under new `halow-join-watch.service`
  (`User=halow-ui`, `BindsTo=halow-ap.service` — the
  halow-sta-events.service pattern). Follows
  `journalctl -f -u halow-ap -u dnsmasq -o short-iso`, maintains
  `/var/lib/halow/join/`, runs the reachability probe, and on **first
  sight of a new MAC** writes a one-shot evidence bundle
  (halow-first-contact.sh pattern, per-MAC): transcript so far,
  `iw dev halow0 station get <mac>`, the dnsmasq lease line, `ip neigh`
  for the leased IP. Bundle written once per MAC (persisted in state),
  capped at 32 KB.
- `<mac>` / `--all [--since-hours N]`: on-demand CLI, re-parses a bounded
  journal window (default 24 h, max 48 h, max 5000 lines parsed — the
  capture-cap pattern), prints JSON.
- `--selftest`: runs the parser against embedded fixture transcripts
  (wrong-PSK, RF-silence, complete join) — the testable-today hook.

**State** (all under `/var/lib/halow/join/`, owner halow-ui, world-readable
like the other jsonl stores; **atomic writes via tmp+os.replace from day
one** — do not repeat halow-mon's `open(w)` defect that #22 exists to fix):
- `state.json` — `{mac: {first_seen, attempts, last_stage, last_stage_at, verdict, ip}}`, max 64 MACs, evict oldest.
- `<mac>.jsonl` — event transcript ring, 500 lines/MAC.
- `<mac>-first-sight.log` — the evidence bundle.

**API** (Flask, existing authenticated TLS, `@authed` like every route):

`GET /api/halow/join-log` →
```json
{
  "window_hours": 24,
  "stations": {
    "aa:bb:cc:dd:ee:ff": {
      "first_seen": "2026-08-09T14:02:11-0700",
      "attempts": 3,
      "last_stage": "sae_commit",
      "verdict": "silence_after_commit",
      "last_stage_at": "2026-08-09T14:02:12-0700"
    }
  }
}
```

`GET /api/halow/join-log/<mac>` →
```json
{
  "mac": "aa:bb:cc:dd:ee:ff",
  "verdict": "wrong_psk_suspected",
  "stages": {
    "sae_commit": "2026-08-09T14:02:11-0700",
    "sae_confirm": "2026-08-09T14:02:11-0700",
    "authenticated": null,
    "associated": null,
    "eapol_done": null,
    "dhcp_ack": null,
    "reachable": null
  },
  "events": [
    {"t": "2026-08-09T14:02:11-0700", "src": "halow-ap",
     "stage": "sae_confirm", "line": "... (RX confirm, status=1) ..."}
  ],
  "bundle": "/api/halow/join-log/aa:bb:cc:dd:ee:ff/bundle"
}
```

`GET /api/halow/join-log/<mac>/bundle` → the first-sight bundle,
`text/plain` (404 until it exists). MAC path segments validated with the
regex already used at `scripts/halowctl:146`.

**Privilege model.** Everything runs as `halow-ui`: journal read via the
`systemd-journal` group (deploy.sh:51 — already how `/api/logs` works),
`iw`/`ip neigh`/`ping` are unprivileged, lease file
`/var/lib/misc/dnsmasq.leases` is world-readable, state dir is owned by
halow-ui (deploy.sh:70). The only root-side change is the conf `gen`
runs as root already (halow-ap.service ExecStartPre). **No sudoers
change. The endpoints never shell through sudo.** Secrets: the parser
stores journal lines and lease data only; the bundle never reads
`/etc/halow/*`; the conf logger keys (unlike `-dd`) emit no key material
— and acceptance still checks that claim rather than trusting it.

## Implementation steps

Each step is one commit; a contributor can execute top to bottom.

1. **hostapd logger level.** In `scripts/halowctl`, `gen()` (heredoc at
   lines 63-83): after the `ctrl_interface_group=halow-ui` line, add the
   four logger keys shown in Design. No new env keys. Update the header
   comment if it enumerates conf contents. Bench step recorded in the
   commit message: `halowctl gen && sudo systemctl restart halow-ap &&
   systemctl is-active halow-ap` — a fork-rejected key kills the AP here,
   not at 2 a.m.
2. **dnsmasq log-dhcp, both templates.** Add `log-dhcp` to
   `config/dnsmasq-halow.conf` *and* to the `dhcp-config` heredoc in
   `scripts/halowctl` (lines 296-305). One commit — it is one config in
   two places. Verify with `halowctl dhcp-config lease=12h && grep
   log-dhcp /etc/dnsmasq.d/halow.conf`.
3. **Parser + verdict engine.** New `scripts/halow-join-log` (python3,
   stdlib only): marker table, per-MAC stage machine, verdict rules,
   30 s silence timeout, bounded window parsing (`--since-hours` ≤ 48,
   ≤ 5000 lines), `--selftest` with three embedded fixtures, and a
   `--parse-file <path>` mode reading `journalctl -o short-iso`-format
   text (selftest uses it; bench replays will too). All state writes
   tmp+`os.replace`. Caps: 64 MACs, 500 events/MAC, 32 KB bundle.
4. **Watcher mode + unit.** `--follow` mode in the same script:
   subprocess `journalctl -f -u halow-ap -u dnsmasq -o short-iso -n 0`,
   feed the parser incrementally, trigger the post-ACK probe (5 s delay,
   `ping -c 3 -W 2 -n <ip>`, `ip -j neigh`), write the first-sight bundle
   on new MAC. New `systemd/halow-join-watch.service` copied from
   `halow-sta-events.service`: `User=halow-ui`,
   `BindsTo=halow-ap.service`, `Restart=on-failure`, `RestartSec=10`
   (no ctrl-socket ExecStartPre — the journal needs no socket).
5. **Deploy wiring.** `scripts/deploy.sh`: add
   `systemd/halow-join-watch.service` to the unit install list (line 67),
   `scripts/halow-join-log` to the script install (line 69), and
   `halow-join-watch` to the enable list (line 81). The watcher creates
   `/var/lib/halow/join/` itself (parent already halow-ui-owned, line 70).
6. **API endpoints.** In `ui/halow_ui.py`, next to `api_halow_events`
   (line 563): `GET /api/halow/join-log`, `GET /api/halow/join-log/<mac>`,
   `GET /api/halow/join-log/<mac>/bundle`. List route reads `state.json`;
   the per-MAC route invokes `halow-join-log <mac> --since-hours N`
   (bounded, unprivileged, `sh()` timeout 15) for a fresh parse and merges
   the stored transcript; MAC validated with
   `^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$` before touching paths or argv.
   Empty state returns `{"stations": {}}` 200, not an error.
7. **UI card.** Debug tab (`PAGE`, near the neighbors card at ~1045):
   "join forensics" card — table of MAC / attempts / last stage / verdict
   from `/api/halow/join-log`, click-through pre that renders the per-MAC
   JSON, link to the bundle. Replace the static 1047 hint text with
   "dhcp_no_reach in join forensics = this trap, auto-detected".
8. **halowctl convenience + docs.** `join-log` subcommand in
   `scripts/halowctl` that execs `/usr/local/bin/halow-join-log "$@"`
   (read-only, no sudo), plus the usage line in the header block.
   Add `halow-join-watch` to the service checks in
   `ui/halow_ui.py` `api_diag_bundle` (line 835 list) and to
   `LOG_UNITS` (line 514) so `/api/logs?unit=halow-join-watch` works.
   Mark roadmap item 16 with its DONE line per repo convention.

## Surface changes

New/changed API endpoints:

| method+path | change | notes |
|---|---|---|
| `GET /api/halow/join-log` | new | all MACs, verdict summary; authed TLS |
| `GET /api/halow/join-log/<mac>` | new | full transcript + stages + verdict |
| `GET /api/halow/join-log/<mac>/bundle` | new | first-sight bundle, text/plain |
| `GET /api/logs?unit=halow-join-watch` | changed | new whitelisted unit in `LOG_UNITS` |
| `GET /api/diag` | changed | `services` block gains `halow-join-watch` |

halowctl:

| command | change | notes |
|---|---|---|
| `halowctl gen` | changed | emits 4 `logger_*` keys into hostapd_s1g.conf |
| `halowctl dhcp-config` | changed | heredoc emits `log-dhcp` |
| `halowctl join-log [mac\|--all\|--selftest]` | new | wrapper for halow-join-log; no sudo |

UI elements:

| element | change |
|---|---|
| Debug tab "join forensics" card | new: MAC/attempts/stage/verdict table + drill-down |
| Debug tab neighbors hint (line 1047) | reworded to point at the automated verdict |

systemd units:

| unit | change |
|---|---|
| `halow-join-watch.service` | new: `User=halow-ui`, `BindsTo=halow-ap.service`, runs `halow-join-log --follow` |

Config files:

| file | change |
|---|---|
| `config/dnsmasq-halow.conf` | `+log-dhcp` |
| `/etc/halow/hostapd_s1g.conf` (generated) | +4 logger keys |
| `config/sudoers-halow-ui` | **unchanged** — everything runs unprivileged |
| `/var/lib/halow/join/` (state) | new dir: state.json, per-MAC jsonl + bundle |

## Testing & acceptance criteria

### Testable today (pre-association)

1. `halow-join-log --selftest` exits 0: the wrong-PSK fixture yields
   `sae_confirm_failed`, the RF-silence fixture yields
   `silence_after_commit`, the complete fixture reaches `dhcp_ack` with
   `reachable` pending. Garbage/unmatched lines produce raw events, no
   exception.
2. On the Pi: `halowctl gen` writes the logger keys (grep all four in
   `/etc/halow/hostapd_s1g.conf`), then `systemctl restart halow-ap` and
   `systemctl is-active halow-ap` — the fork accepted the keys. Journal
   shows hostapd startup lines at the new verbosity.
3. `dnsmasq --test` clean and `systemctl is-active dnsmasq` after the
   conf change; then `halowctl dhcp-config lease=12h` and confirm
   `log-dhcp` **survives the rewrite**.
4. `systemctl is-active halow-join-watch` after deploy; restart halow-ap
   twice and confirm the watcher rides the BindsTo cycle without wedging
   and `state.json` stays valid JSON (the atomic-write check).
5. `GET /api/halow/join-log` with no history returns
   `{"stations": {}}` 200; a malformed MAC path returns 400; bundle for
   an unseen MAC returns 404. All over the existing authenticated TLS —
   unauthenticated requests bounce exactly like `/api/halow/events`.
6. **Secret check (root runs it, reports only pass/fail):**
   `sudo sh -c 'journalctl -u halow-ap --since -1h | grep -cF "$(grep -oP "^HALOW_PASSPHRASE=\K.*" /etc/halow/halow.env)"'`
   must print 0 after an AP restart at the new logger level, and the same
   grep against `/var/lib/halow/join/` must be empty. The PSK has leaked
   twice via "harmless" echoes; this claim gets measured, not assumed.

### Needs a joined (or attempting) station

7. **Wrong-PSK run [M]:** set a deliberately wrong `halow_psk` on one
   node, attempt a join. Verdict is `sae_confirm_failed` within one
   window; capture the exact hostapd_s1g strings from the transcript and
   fold them into the marker table, replacing the [C] upstream guesses.
   This is also the moment to confirm whether the fork emits
   `AP-STA-POSSIBLE-PSK-MISMATCH`.
8. **RF-death run [M]:** correct PSK, antenna pulled (or the bench's
   known SPI-stall condition). Verdict `silence_after_commit` — and now
   distinguishable from run 7 by gateway evidence alone, which is the
   whole point.
9. **Clean join [M]:** all seven stages timestamped in order; verdict
   `complete` only after the probe reports 3/3 received at the gateway
   (receiver-side count, not the station's claim); first-sight bundle
   written exactly once for that MAC and non-empty; `eapol_done`
   timestamp within a few seconds of the `AP-STA-CONNECTED` line in
   `/var/lib/halow/station-events.log` (two independent observers agree).
10. Bounds hold under reality: transcript stops at 500 events/MAC after
    repeated attempt cycles; state dir never exceeds 64 MACs; per-MAC
    on-demand parse returns in < 15 s (the endpoint's `sh()` timeout).

## Out of scope

- Deauth/disassoc **reason-code** capture and battery-AP knobs — issue
  #28, which reuses this marker table.
- Per-minute station health walking (assoc → lease → ARP → ICMP for
  *already-joined* stations) — issue #18; this issue diagnoses the join,
  not the tenure.
- Log retention/rotation policy and journald `SystemMaxUse` — issue #22.
  This issue bounds its own files but does not touch global retention.
- Any node-side (mesh-v4) change. The gateway witnesses; the node is
  assumed mute.
- Automatic packet capture on failed joins. `halowctl capture` exists
  for manual follow-up; auto-pcap is new attack/storage surface for
  little gain over the transcript.
- Healing actions from verdicts. halow-mon owns bounded healing; this
  records and classifies only.

## Risks & gotchas

- **A rejected conf key kills the AP.** halow-ap.service regenerates the
  conf at ExecStartPre and hostapd aborts on unknown keys. hostapd_s1g is
  a Morse fork; the logger keys are core-parser, but verify on the bench
  immediately after step 1 (acceptance 2). If the fork rejects them, the
  fallback is syslog-only (`logger_syslog_level=1`) or, last resort,
  parsing at INFO with a degraded stage set — say so in the conf comment.
- **Marker strings are [C] until captured.** The parser must never
  hard-fail on unmatched lines; verdicts degrade to the last matched
  stage. Runs 7-9 convert the table to [M]. Keep the table in one dict so
  #28's edits and bench corrections are one-line diffs.
- **The dhcp-config rewrite trap.** Anyone touching DHCP from the UI
  regenerates the dnsmasq conf from the halowctl heredoc. Step 2 patches
  both copies; acceptance 3 proves it. The same class of trap exists for
  the hostapd conf, but `gen` *is* the single source there.
- **Never reach for `-d`/`-dd`.** The conf logger keys are secret-safe;
  the wpa_debug flag family can dump EAPOL keying material at `-dd`. If
  someone later wants more depth, the answer is a bounded
  `halowctl capture` during a join window, not debug flags. The generated
  conf itself contains `sae_password` (mode 0600 via umask 077 at
  halowctl:50) — the bundle must never cat conf files.
- **DEBUG chatter volume.** Level 1 on stdout is quiet on an idle AP
  (beacons are not logged) but bursts during association storms; journald
  rate-limiting may drop mid-burst lines. Acceptable: the stage machine
  needs any one marker per stage, not every line. Issue #22 caps journal
  disk use.
- **Watcher state across AP restarts.** BindsTo cycles the watcher with
  every `halowctl set`/profile change (halow-ap restarts). Atomic state
  writes are therefore load-bearing from the first deploy — halow-mon's
  in-place rewrites are the documented defect (#22); do not clone that
  bug into the component whose job is to survive failures.
- **`iw station get` permissions.** Expected to work unprivileged
  (read-only nl80211, same as the UI's `iw` calls); if the bench proves
  otherwise, omit that bundle section — do **not** add a sudoers row for
  a nice-to-have.
- **Two DHCP servers, one journal.** dnsmasq binds halow0 only
  (`interface=halow0`, dnsmasq-halow.conf:3); the 2.4 GHz AP's DHCP is
  NetworkManager's own. `DHCPACK(halow0)` lines are therefore HaLow-only
  — the parser keys on the `(halow0)` tag anyway, defensively.
- **Timestamps.** Order events by journal timestamps only; never mix in
  `date -Is` from other logs when computing stage deltas
  (station-events.log is a cross-check, acceptance 9, not a clock source).
