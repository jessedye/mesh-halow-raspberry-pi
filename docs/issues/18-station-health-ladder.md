# 18. Automated per-station health ladder in halow-mon (assoc, lease, ARP, ICMP) with sleep-aware freshness

> Tier 1 - first-association | Effort: small | Impact: high | Depends on: #22 (atomic-write helper; land first or together — both edit `scripts/halow-mon`); interval-source schema coordinates with #25/#30

## Problem

Context for a contributor new to both repos: this Pi 4 is an 802.11ah
(HaLow, sub-GHz "S1G") access point on the Morse Micro MM6108 driver,
broadcasting SSID `mesh` and routing 10.117.0.0/24 (gateway 10.117.0.1).
Its stations are ESP32 Meshtastic nodes from the sibling repo `mesh-v4`
— two Heltec V4.2 boards carrying the same MM6108 chip. First
association is imminent, blocked only on a decoupling capacitor and
antenna confirmation. When it happens, the first question is not "did
it associate" but "which layer works": association, DHCP lease, ARP,
real ICMP.

The bench has a named recurring failure mode here: a station that is
**associated but answers no ARP** — the driver reports a Station entry
while the link above it is dead. Today the only defense is a manual hint
on the Diag tab (`ui/halow_ui.py:1047`: "FAILED with a known mac = the
'associated but answers no ARP' trap") — a human must open the tab, know
the MAC, and interpret the kernel neighbor cache. The per-minute monitor
`scripts/halow-mon` already states the doctrine in its docstring (lines
8-9): reachability is tested with ICMP, "not by asking the driver how it
feels". But it applies it only to the upstream LAN gateway
(`UPSTREAM = "192.168.50.1"`, line 19, pinged at lines 55-57) — never to
the stations the gateway exists to serve.

The second half is dead-vs-quiet. The mesh-v4 watcher
(`tools/nodewatch.py`) exists because on this mesh **quiet is the normal
state** — telemetry floors at 30 minutes, an asset tag checks in every
six hours — so "no news" carries no information (docstring, lines
11-16). A deep-sleeping asset tag silent for 6 h is healthy; the same
silence from a trail cam is a fault. Without a per-station expected
cadence, any absence alarm either cries wolf on sleepers or stays silent
on corpses. nodewatch solved this with staleness at 3x the node's own
interval (`HOLD_MULTIPLIER = 3`, nodewatch.py:52, applied at :254). The
gateway should apply the same rule, not invent a second one.

This lands now because it classifies the very first join the minute it
happens — including the degraded joins (assoc but no DHCP; leased but
ARP-dead) that are otherwise indistinguishable from success in `iw`
output and from RF failure in the node's logs.

## Current state

All references below verified against the working tree this session.

**Gateway repo (`mesh-halow-raspberry-pi`):**

- `scripts/halow-mon` (root, fired every 60 s by `halow-mon.timer`,
  `OnUnitActiveSec=60`): pings the upstream gateway only (lines 19,
  55-57). Stations get **passive** sampling: `iw dev halow0 station dump`
  is shelled twice (line 96 for a count, line 104 for parsing), and
  signal/bitrate/retry fields are appended to
  `/var/lib/halow/stations.jsonl` (lines 104-133). No lease check, no
  ARP probe, no ICMP to any station, no per-station state across runs,
  no classified transitions. The healer's own discipline is worth
  copying: two consecutive bad samples before acting
  (`ap_down_streak >= 2`, lines 60-67). It also writes state in place
  (`mon-state.json` line 144, ring prunes 127-133/135-142) — #22's
  defect; this issue must not add more in-place writes.
- `scripts/halow-sta-events` (3 lines of shell): hostapd_cli action hook
  appending `date -Is EVENT MAC` lines to
  `/var/lib/halow/station-events.log`. It runs as **halow-ui** (unit
  `systemd/halow-sta-events.service:8`, `User=halow-ui`), and
  `/var/lib/halow` is owned `halow-ui:halow-ui` (`scripts/deploy.sh:70`).
  Only successful connects/disconnects appear; no health classes.
- `ui/halow_ui.py`: `GET /api/halow/link[/<mac>]` (742-786) aggregates
  stations.jsonl into min/avg/max rate, signal, delivery%, retry% —
  purely passive, nothing about lease/ARP/ICMP. `GET /api/halow`
  (187-226) parses live `iw station dump` including `inactive time`
  (203), a field halow-mon does not yet keep. Leases parsed at 271-280
  (`/var/lib/misc/dnsmasq.leases`); reservations at 333-342
  (`/etc/dnsmasq.d/halow-reservations.conf`, `dhcp-host=MAC,IP[,NAME]`,
  written by `halowctl dhcp-reserve`, scripts/halowctl:137-162).
  `GET /api/diag/neigh` (671-681) exposes the kernel ARP cache; the
  manual-trap hint is Diag-tab line 1047. Station table in the HaLow
  tab (1203-1206, 1218-1219): mac/signal/tx/rx/connected — no health
  column. Debug tab renders station-events.log verbatim (1094-1095), so
  new event types surface there for free.
- `config/sudoers-halow-ui`: the UI's entire root surface; nothing here
  needs to grow (see privilege model).
- `config/nodes.json.example`: `/etc/halow/nodes.json` entries are
  `{name, url, token}` only — **contains bearer tokens**, never
  committed. #25 plans the `mac` + expected-interval fields.

**Client repo (`mesh-v4`):**

- `tools/nodewatch.py`: `HOLD_MULTIPLIER = 3` (line 52), staleness
  window `interval * 3` (:254), interval read from the node itself with
  a **conservative 3600 s fallback** — "a too-long window under-alerts
  rather than crying wolf" (:167-180). States
  healthy/stale/wifi-down/down/unknown; an HTTP error or empty body is
  explicitly *not* down (:215-243). Atomic state writes via
  tmp + `os.replace` (:160-164).
- `nodes/node1/device.env`, `nodes/node2/device.env`: the `MAC` fields
  (`b0:a6:04:c5:a2:68`, `8c:fd:49:b6:19:04`) are the **ESP32 WiFi
  MACs**. The MM6108 radio has its own MAC, first observable in
  `iw dev halow0 station dump` at association — per-MAC gateway config
  must expect to be filled in at first join.

## Design

Everything active lives in `scripts/halow-mon` (root, already fires
every minute, already owns ICMP doctrine). The UI only reads a
world-readable state file. **Zero new sudoers entries.**

### The ladder, per pass

For every MAC in the current `iw dev halow0 station dump`:

1. **assoc** — present in the dump (true by construction).
2. **IP discovery** — first hit wins: dnsmasq lease
   (`/var/lib/misc/dnsmasq.leases`) → reservation
   (`/etc/dnsmasq.d/halow-reservations.conf`) → kernel neighbor cache
   (`ip -j neigh show dev halow0`, reverse-matched by `lladdr`). No IP
   anywhere → stop; probing is impossible.
3. **arp** — `arping -c 2 -w 3 -I halow0 <ip>` (rc 0 = a receiver
   answered). Binary absent → fall back to neighbor-cache state
   `REACHABLE`, mark `arp_method: "cache"` — degraded, not fatal.
4. **icmp** — `ping -c 1 -W 5 -n <ip>`. The 5 s timeout is deliberate:
   an S1G station in power-save has frames buffered at the AP until its
   next wake; a slow answer is normal, not a failure.

Classification = highest rung achieved:

| state | meaning |
|---|---|
| `reachable` | assoc + ICMP answered (lease/arp implied working) |
| `arp-no-icmp` | L2 answers, IP stack doesn't (firewalled or wedged) |
| `leased-no-arp` | the named bench trap, now machine-detected |
| `assoc-no-lease` | in the dump, no IP discoverable anywhere |

MACs **absent** from the dump are **never probed** — no ARP, no ICMP,
no "failed" verdicts (verifier constraint: a power-saving station
absent from the dump must never be counted ICMP-failed). They are
judged only by sleep-aware freshness:

| state | rule |
|---|---|
| `quiet` | known MAC, `now - last_seen_assoc <= 3 * expected_interval_s` |
| `lost` | known MAC, silence beyond that window |
| `never-seen` | known MAC with no association on record (the pre-association steady state; no event, no alarm) |
| `gone` | unknown MAC that disassociated; one `HEALTH-GONE` event, entry pruned after 24 h |

"Known" = MAC appears in the reservations file or has a `mac` entry in
`/etc/halow/nodes.json`. `HOLD_MULTIPLIER = 3` — same constant, name,
and justification as nodewatch.py:52. `expected_interval_s` comes from
nodes.json when an entry has both `mac` and `expected_interval_s`
(schema owned by #25/#30 — read defensively, tolerate absence), else
`DEFAULT_INTERVAL_S = 3600`, nodewatch's conservative fallback (:180).

### Transition discipline

Classification is computed every pass but **committed** (state file +
event log) only after `HOLD_SAMPLES = 2` consecutive passes agree — the
healer's own two-samples-not-a-blip rule (halow-mon:62). Two exceptions:
transitions *into* `reachable` commit immediately, and `quiet -> lost`
commits at the threshold crossing (already a 3x hold by construction).
This also gives DHCP a free grace pass after association before
`assoc-no-lease` can be logged.

Committed transitions append to the **existing**
`/var/lib/halow/station-events.log` in its existing line shape
(timestamp, event token, MAC, detail):

```
2026-08-05T14:03:11-05:00 HEALTH-REACHABLE aa:bb:cc:dd:ee:ff ip=10.117.0.50 was=assoc-no-lease
2026-08-05T14:21:11-05:00 HEALTH-LEASED-NO-ARP aa:bb:cc:dd:ee:ff ip=10.117.0.50 was=reachable held=2
```

Ownership caveat found this session: the log is created and appended by
`halow-sta-events` running as **halow-ui**, in a halow-ui-owned
directory (deploy.sh:70). Root appending is always fine, but if
halow-mon ever *creates* the file first, a root-owned 0644 file locks
halow-ui out of its own log. After appending, if the file is root-owned,
`shutil.chown(log, "halow-ui", "halow-ui")`.

### Bounds

Every probe and the whole walk are capped, following the capture
pattern (halowctl capture: 3-30 s, 5000 frames). Per-station worst
case: 3 s arping + 5 s ping = 8 s. Whole walk: `WALK_BUDGET_S = 30`
wall clock (`time.monotonic()`); stations left unwalked keep their
previous state with `detail: "walk=skipped"` and go first next pass
(round-robin cursor persisted in the state file). The pass must always
finish well inside the 60 s timer cadence (#24 later adds a hard
RuntimeMaxSec).

### State file

`/var/lib/halow/station-health.json`, mode 0644, written atomically
(tmp + `os.replace` — #22's helper if landed, else a local two-liner
matching nodewatch.py:160-164 that #22 then unifies). All MAC keys
normalized lowercase. Shape:

```json
{
  "_cursor": 0,
  "stations": {
    "aa:bb:cc:dd:ee:ff": {
      "state": "leased-no-arp", "since": 1754407500,
      "pending": null, "pending_streak": 0,
      "last_seen_assoc": 1754407560,
      "ip": "10.117.0.50", "ip_source": "lease", "known": true,
      "expected_interval_s": 300, "stale_after_s": 900,
      "rungs": {"assoc": true, "lease": true, "arp": false, "icmp": false},
      "arp_method": "arping", "probe_ms": 6120, "detail": ""
    }
  }
}
```

Secrets rule: this file is world-readable, so it carries **only**
mac/ip/state/timestamps/interval. The nodes.json loader reads `mac` +
`expected_interval_s` and drops the parsed object, so `url`/`token`
cannot leak into state, logs, or API responses (the SAE PSK has leaked
twice through "harmless" echoes; tokens get the same paranoia).

### API contract

`GET /api/halow/link` and `/api/halow/link/<mac>` (ui/halow_ui.py:
742-786) gain a `health` object per MAC, merged from the state file.
MACs known to health but absent from stations.jsonl (e.g. `never-seen`
reserved nodes) are included with telemetry fields null — visible on
day one, pre-association:

```json
{
  "stations": {
    "aa:bb:cc:dd:ee:ff": {
      "now": {"t": 1754407560, "signal_dbm": -58, "tx_mbps": 2.6},
      "n_samples": 240,
      "tx_mbps": {"min": 0.6, "avg": 2.1, "max": 3.3},
      "signal_dbm": {"min": -71, "avg": -60.2, "max": -55},
      "delivery_pct": 99.61, "retry_pct": 4.1,
      "health": {
        "state": "reachable", "since": 1754406000,
        "ip": "10.117.0.50", "ip_source": "lease",
        "rungs": {"assoc": true, "lease": true, "arp": true, "icmp": true},
        "expected_interval_s": 300, "stale_after_s": 900,
        "last_seen_assoc": 1754407560, "probe_ms": 212, "detail": ""
      }
    },
    "11:22:33:44:55:66": {
      "now": null, "n_samples": 0,
      "health": {"state": "never-seen", "expected_interval_s": 21600,
                 "stale_after_s": 64800, "last_seen_assoc": null}
    }
  }
}
```

Absent state file → `"health": null`, endpoint shape otherwise unchanged
(existing consumers keep working).

### Privilege model

| actor | does | privilege |
|---|---|---|
| halow-mon | probes (arping/ping), classifies, writes station-health.json, appends station-events.log | root (existing, via halow-mon.timer) |
| halow-ui | reads station-health.json (0644), merges into /api/halow/link | unprivileged; **no sudoers change** |
| halowctl `health` | formats station-health.json for the bench | unprivileged read; **not** added to sudoers |

## Implementation steps

Each step is one commit. Steps 1-6 are `scripts/halow-mon`; coordinate
step 1 with #22.

1. **Atomic write helper.** Use #22's helper if landed; else add
   `atomic_write_json(path, obj, mode=0o644)` to halow-mon (tmp in
   `STATE_DIR` + `os.replace` + `os.chmod`, the nodewatch.py:160-164
   convention) and use it for the **new** state file only — converting
   existing writers stays #22's commit.
2. **Parse the dump once.** Extract halow-mon:104-126 into
   `parse_station_dump(dump)` returning `[{mac, signal_dbm, tx_mbps,
   rx_mbps, tx_packets, tx_retries, tx_failed, inactive_ms}, ...]`
   (add `inactive time`; the UI already reads it, halow_ui.py:203).
   Shell `iw dev halow0 station dump` once per pass; derive the line-96
   count from the parsed list. Lowercase MACs at this single ingestion
   point.
3. **Input readers + constants.** `read_leases()`
   (`/var/lib/misc/dnsmasq.leases`), `read_reservations()`
   (`dhcp-host=` lines of `/etc/dnsmasq.d/halow-reservations.conf`),
   `read_neigh()` (`ip -j neigh show dev halow0`), `known_intervals()`
   (`/etc/halow/nodes.json`, reading only `mac` + `expected_interval_s`;
   absent/malformed anything yields `{}`). Constants:
   `HOLD_MULTIPLIER = 3`, `DEFAULT_INTERVAL_S = 3600`,
   `HOLD_SAMPLES = 2`, `ARPING_W = 3`, `PING_W = 5`,
   `WALK_BUDGET_S = 30`, `PRUNE_UNKNOWN_S = 86400`.
4. **Pure classifier.** `classify(assoc, ip, arp_ok, icmp_ok,
   last_seen_assoc, interval_s, now, known) -> (state, detail)`
   implementing the two tables above; no subprocess calls inside. This
   is the unit-testable core (halow-mon imports side-effect-free —
   `__main__` guard at line 148).
5. **The walk.** After the stations.jsonl block: load
   station-health.json, walk associated MACs from the round-robin
   cursor under `WALK_BUDGET_S` (IP discovery → arping → ping →
   classify, recording `probe_ms`); classify absent MACs (union of
   state-file keys, reservations, nodes.json macs) by freshness only —
   **no probes**.
6. **Commit + events + prune.** Apply HOLD_SAMPLES pending logic; on
   commit append `HEALTH-<STATE>` lines to station-events.log (format
   above), fix ownership if root created the file (`shutil.chown` to
   halow-ui), prune unknown `gone` entries older than
   `PRUNE_UNKNOWN_S`, write the state file atomically.
7. **Metrics tie-in.** Add `"sta_reachable"` (count of `reachable`) to
   the per-minute `sample` dict (halow-mon:88-98) and to the
   summary-key tuple in `/api/metrics` (halow_ui.py:814).
8. **Dependency.** Add `iputils-arping` to the apt-get list in
   `scripts/install.sh` (lines 72-75); halow-mon still degrades to
   `arp_method: "cache"` when the binary is missing.
9. **API merge.** In `api_halow_link` (halow_ui.py:742-786): load
   station-health.json, attach `health` per lowercased MAC, add
   health-only MACs with null telemetry per the contract above;
   single-MAC route follows.
10. **UI.** HaLow tab: `health` + `last seen` columns on the stations
    table (halow_ui.py:1203-1206, 1218-1219), color-mapped
    `ok` = reachable; `warn` = quiet, never-seen, assoc-no-lease;
    `bad` = leased-no-arp, arp-no-icmp, lost — from one extra
    `j("/api/halow/link")` in `halow()`. Add a "known stations (health
    ladder)" card listing every health entry so reserved-but-never-seen
    nodes are visible pre-join. Reword the Diag hint at 1047: the trap
    is now auto-classified each minute (keep the manual view — useful
    when the timer itself is suspect).
11. **CLI.** `halowctl health`: read-only python3-heredoc formatter of
    station-health.json (mac/state/ip/since/last-assoc/interval). No
    sudo, no sudoers entry.

## Surface changes

**API**

| endpoint | change |
|---|---|
| `GET /api/halow/link` | + `health` object per MAC; + health-only MACs with null telemetry; absent state file → `health: null` |
| `GET /api/halow/link/<mac>` | same, single MAC |
| `GET /api/metrics` | samples and summary gain `sta_reachable` |
| `GET /api/halow/events` | unchanged code; now also carries `HEALTH-*` lines |

**halowctl**

| command | change |
|---|---|
| `halowctl health` | new, read-only view of station-health.json; not in sudoers |

**UI**

| element | change |
|---|---|
| HaLow tab stations table | + health, + last seen columns |
| HaLow tab | + "known stations (health ladder)" card |
| Diag tab neighbor hint (1047) | reworded: trap is auto-classified |
| Debug tab station events | HEALTH-* lines appear (no code change) |

**systemd / sudoers / config / state**

| file | change |
|---|---|
| `systemd/halow-mon.{service,timer}` | none (existing 60 s cadence) |
| `config/sudoers-halow-ui` | **none** — state the fact in the PR |
| `/var/lib/halow/station-health.json` | new, 0644, atomic writes, no secrets |
| `/var/lib/halow/station-events.log` | new `HEALTH-*` line types, same format |
| `/etc/halow/nodes.json` | optionally consumed keys `mac`, `expected_interval_s` (schema owned by #25; this issue only reads) |
| `scripts/install.sh` | + `iputils-arping` |

## Testing & acceptance criteria

### Testable today (pre-association)

1. **Classifier unit matrix** (workstation, no hardware):

   ```bash
   cd mesh-halow-raspberry-pi && python3 - <<'EOF'
   import importlib.util
   s = importlib.util.spec_from_file_location("hm", "scripts/halow-mon")
   m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
   now = 1_000_000
   assert m.classify(True,  "10.117.0.50", True,  True,  now,      300,   now, True)[0] == "reachable"
   assert m.classify(True,  "10.117.0.50", True,  False, now,      300,   now, True)[0] == "arp-no-icmp"
   assert m.classify(True,  "10.117.0.50", False, False, now,      300,   now, True)[0] == "leased-no-arp"
   assert m.classify(True,  None,          False, False, now,      300,   now, True)[0] == "assoc-no-lease"
   assert m.classify(False, None,          False, False, now-600,  21600, now, True)[0] == "quiet"  # asset tag: healthy
   assert m.classify(False, None,          False, False, now-1200, 300,   now, True)[0] == "lost"   # same silence: fault
   assert m.classify(False, None,          False, False, None,     3600,  now, True)[0] == "never-seen"
   print("classify matrix ok")
   EOF
   ```

   Pass: all hold — the quiet/lost pair is identical silence with
   opposite verdicts, driven only by the interval.
2. **Zero-station pass** (on the Pi): `time sudo /usr/local/bin/halow-mon`
   with no stations completes in < 5 s [M], creates a valid
   station-health.json (0644), appends nothing to station-events.log
   (never-seen logs no events).
3. **Never-seen visibility**: `sudo halowctl dhcp-reserve add
   mac=aa:bb:cc:dd:ee:01 ip=10.117.0.99 name=testnode`; after the next
   timer fire the MAC shows `never-seen` in station-health.json, in
   `GET /api/halow/link` (null telemetry), and in the new HaLow tab
   card. Remove the reservation afterward.
4. **No probes to absent stations**: during test 3, `sudo tcpdump -i
   halow0 -c 50 -w /dev/null arp or icmp` alongside a monitor pass shows
   zero probe frames to 10.117.0.99 — bounded capture, receiver-side
   evidence.
5. **Secrets audit**: with a real `/etc/halow/nodes.json` in place,
   `grep -ci token /var/lib/halow/station-health.json` returns 0, and
   the same grep over `curl -sku user:pass .../api/halow/link` output
   returns 0. Also: no `url`, no node names.
6. **Event-log ownership**: delete station-events.log, force root
   halow-mon to create it (seed a fake unknown `gone` entry), then
   `sudo -u halow-ui sh -c 'echo t >> /var/lib/halow/station-events.log'`
   must still succeed.
7. **Endpoint compatibility**: with the state file deleted,
   `GET /api/halow/link` returns the pre-change shape plus
   `health: null` — no 500, no key removals.

### Needs a joined station

8. **First join classified** [M]: within 2 timer fires of the first
   Heltec association, station-events.log shows `AP-STA-CONNECTED` then
   a `HEALTH-*` line for the same MAC; if DHCP completes, state reaches
   `reachable` on the strength of an actual ICMP reply (receiver-side,
   not driver-claimed).
9. **The trap, on demand** [M]: cut the node's power mid-session.
   hostapd keeps the Station entry until inactivity deauth (~300 s
   default), so within 3 timer fires the ladder must commit
   `reachable -> leased-no-arp` (or `arp-no-icmp`) with `was=reachable`
   — the dead-association trap machine-detected minutes before the
   driver notices. After deauth the MAC moves to `quiet`, then `lost`
   after 3x its interval.
10. **Sleep-awareness end-to-end** [M]: with `expected_interval_s:
    21600` for the node's MAC, power it off cleanly: `quiet` (not
    `lost`) for 18 h. With 300: `lost` within ~15 min + one pass. Both
    read from station-health.json timestamps, both bounded waits.
11. **Walk bound** [M]: with both nodes associated, `time sudo
    /usr/local/bin/halow-mon` stays under 45 s worst-case (2 stations x
    8 s probe ceiling + sampling), and `probe_ms` is recorded.

Acceptance = tests 1-7 green pre-association; 8-11 confirmed at first
opportunity with `[M]` numbers recorded in the PR or a results note.
Every claim is measured at a receiver (ping/ARP replies, file contents,
wall clock), never inferred from driver state.

## Out of scope

- **Healing actions on stations** — no deauth, no dnsmasq pokes, no
  restarts keyed on station health. Detect and classify only (the
  halow-mon docstring's own split); station-side healing would be a
  separately-argued item.
- **Rung-cost math** (#20) — this issue produces the health input;
  windowed delivery and damped cost stay in #20.
- **Failed-join forensics** (#16) — SAE/EAPOL/DHCP stage-of-failure for
  MACs that never reach the dump; this ladder starts at association.
- **Fleet watcher over node admin APIs** (#25) — no HTTPS calls to
  nodes, no nodes.json schema ownership; this issue only *reads* two
  optional keys #25 defines.
- **Presence ledger / check-in contract** (#30); **per-STA power-save
  knobs, DTIM, deauth reasons** (#28); **logrotate for
  station-events.log** and conversion of existing in-place writers
  (#22).

## Risks & gotchas

- **The HaLow MAC is not the WiFi MAC.** mesh-v4 `device.env` MACs
  (b0:a6:..., 8c:fd:...) are ESP32 WiFi MACs; the MM6108 radio answers
  with its own. Fill reservations and nodes.json `mac` entries from
  `iw dev halow0 station dump` at first sight, never from mesh-v4
  (#25's onboarding automates this; until then, a manual step worth a
  README line).
- **PS buffering is not failure.** An S1G station in power-save answers
  ICMP late (the AP buffers until wake). The 5 s ping timeout fits
  today — the nodes pin PS off — but when node power-save lands (#28
  era), timeouts may need per-station tuning. Never tighten `PING_W` to
  speed the walk; shrink the walk set instead.
- **Never probe the absent.** Pinging a sleeper's last IP and calling
  silence "down" re-creates the false alarm nodewatch was built to
  kill; freshness verdicts come from timestamps only.
- **station-events.log has two writers with different UIDs** (root
  halow-mon, halow-ui's hostapd hook). Appends are safe; creation order
  is not — step 6's ownership guard is load-bearing.
- **nodes.json holds tokens.** The interval loader is the only new code
  allowed to open it and must return only mac→interval. Review the diff
  for any path where a parsed node object reaches a log line, state
  file, or jsonify call — the SAE PSK has leaked twice through exactly
  this class of "harmless" plumbing.
- **halow-mon contention** (docs/issues/README.md:52-54): #18, #20, #22,
  #24 all edit this file. Land #22's helper first or in the same series;
  no in-place writes even temporarily.
- **Airtime on a 1 MHz channel.** Two arpings + a ping per station per
  minute is negligible at bench scale but real on a busy S1G channel
  co-sited with LoRa in 902-928 MHz. WALK_BUDGET and the round-robin
  cursor cap it; past ~8 stations, probe each every Nth pass rather
  than raising the budget.
- **Timer overlap.** halow-mon is a oneshot on a 60 s `OnUnitActiveSec`
  timer; a walk outliving the cadence delays (not stacks) fires,
  silently thinning metrics. The 30 s budget plus test 11 keep this
  visible; #24 adds the RuntimeMaxSec backstop.
- **Cache-based ARP fallback lies gently.** Without arping, `REACHABLE`
  in the neighbor cache reflects recent kernel traffic, not a probe this
  pass — hence `arp_method` is recorded and the fallback marked, never
  silent.
