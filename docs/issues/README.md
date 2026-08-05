# Roadmap v2 issues — index and build order

One file per approved roadmap item (see `../feature-roadmap.md`, items
16–30). Each issue is pickup-ready: problem, verified current-state
analysis, design, ordered implementation steps, surface changes, and
acceptance criteria split into "testable today" vs "needs a joined
station". Mirrored to GitHub issues; the files are canonical.

GitHub mapping (filed 2026-08-05): roadmap item N = GitHub issue N−15,
i.e. [#1](https://github.com/jessedye/mesh-halow-raspberry-pi/issues/1)–[#15](https://github.com/jessedye/mesh-halow-raspberry-pi/issues/15)
cover items 16–30 in order. Labels: `roadmap-v2`, `tier-1`…`tier-4`,
`defect` on items 21/22.

## Milestones

### M1 — Association-ready (build before the capacitor goes on)

The node-side driver is proven below RF; first association is blocked
only on the bulk decoupling capacitor + antenna confirmation. These five
pay off within minutes of the first join attempt — including a failed one.

| # | Issue | Effort | Why this order |
|---|---|---|---|
| 17 | [Pinned-scan compatibility guard](17-pinned-scan-guard.md) | small | Removes the foot-gun that would strand the first station; smallest item |
| 16 | [Association forensics](16-association-forensics.md) | medium | Must be recording before the first join attempt, success or failure |
| 18 | [Station health ladder](18-station-health-ladder.md) | small | Classifies the join the moment it happens ("associated" vs "reachable") |
| 19 | [ESP32-class UDP link tester](19-udp-link-tester.md) | medium | Publishes the wire format so the node client is written against a live target |
| 20 | [Measured rung-cost endpoint](20-rung-cost-endpoint.md) | medium | Starts accumulating windows minutes after first association |

### M2 — Unattended reliability (contains two live defects)

| # | Issue | Effort | Note |
|---|---|---|---|
| 22 | [Storage discipline](22-storage-discipline.md) | medium | **DEFECT** (non-atomic self-healer state). Land the atomic-write helper before 18/20 extend halow-mon |
| 21 | [Time holdover](21-time-holdover.md) | small | **DEFECT** (chrony refuses to serve when unsynced) |
| 23 | [Kernel-upgrade interlock](23-kernel-upgrade-interlock.md) | medium | deploy.sh apt-get installs on every deploy — routine trigger |
| 24 | [HW watchdog + brownout policy](24-hardware-watchdog-brownout.md) | medium | Pet halow-ui (notify), not oneshot halow-mon; brownout ledger uses 22's helper |

### M3 — Fleet operations through the gateway

| # | Issue | Effort | Note |
|---|---|---|---|
| 25 | [Fleet health watcher](25-fleet-health-watcher.md) | medium | Defines the nodes.json `mac` (+ interval) schema that 26 and 30 reuse |
| 26 | [Proxy failover + reach matrix](26-proxy-failover-reach.md) | medium | Depends on 25's mac field |
| 27 | [Encrypted backup/restore](27-backup-restore.md) | medium | Independent; do any time |

### M4 — Product-path groundwork

| # | Issue | Effort | Note |
|---|---|---|---|
| 28 | [Battery AP knobs + deauth reasons](28-battery-ap-knobs.md) | small | AP side ready before node power-save lands; shares journal parsing with 16 |
| 29 | [Trail-cam ingest sink](29-trailcam-ingest.md) | medium | Contract curl-testable today; full value gated on node TX datapath |
| 30 | [Presence ledger + check-in](30-presence-ledger-checkin.md) | medium | Uses 25's schema; explicitly does NOT cover asset-tag sleep validation |

## Cross-issue coordination

- **halow-mon contention**: 18, 20, 22, and 24 all touch `scripts/halow-mon`.
  Land 22's atomic-write helper first; 18 and 20 build on it; 24 adds the
  ledger last.
- **nodes.json schema**: one change (25) adds `mac` and `expected_interval`;
  26 and 30 consume it. Do not add fields piecemeal.
- **hostapd journal parsing**: 16 raises the logger level and parses the
  journal; 28's deauth-reason capture reuses that machinery.
- **Rejected-list boundaries** (from roadmap v1, still binding): no
  cleartext admin API on the gateway (19's UDP responder is
  measurement-only, no control surface); no store-and-forward relay (29 is
  a terminal sink); no gateway GPS features; no PKI nodedb (25 stays
  non-cryptographic).
