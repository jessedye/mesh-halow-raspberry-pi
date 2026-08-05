# 19. ESP32-class link tester: sequenced-UDP sink/echo with receiver-side counting

> Tier 1 - first-association | Effort: medium | Impact: high | Depends on: none (record shape coordinated with #20)

## Problem

The only throughput machinery this gateway has is iperf3: a server unit
(`systemd/halow-iperf3.service`) and an iperf3-*client* endpoint
(`POST /api/halow/throughput`, ui/halow_ui.py:518-548). Both assume the
peer can run iperf3. The peers are two Heltec V4.2 ESP32-S3 nodes running
Meshtastic with the same MM6108 HaLow chip attached over SPI (mesh-v4
repo), whose node plan milestone (f) — mesh-v4
firmware/halow/README.md:403-405 — is explicitly "iperf/fulltest across the
HaLow path and compare against the LoRa/ESP-NOW numbers". A full iperf3
client (TCP control channel, JSON negotiation, bidirectional streams)
inside Meshtastic on an SPI-bound node is unrealistic: proven raw SPI
throughput is 2.06 Mbit/s at a 4 MHz clock (firmware/halow/README.md:12),
and the shipping overlay runs 1 MHz because 4 MHz went runtime-deaf
(README.md:64-66). Milestone (f) has no realistic harness on either side
today.

The measurement discipline the harness must satisfy is already written
down, in blood. mesh-v4 tools/measure.py:144-148 hard-refuses any delivery
record whose `counted_at` is not `"receiver"`, because sender counters lie:
at 75 ft an ESP-NOW sender reported `acked=1000 failed=0` while the receiver
logged 424 of 1000 arriving (mesh-v4 firmware/ADMIN-API.md:63-68,
measure.py:26-27). A tester that trusts what the node *claims* it delivered
— or lets a TCP layer silently retransmit the loss away — reproduces that
lie: iperf3-over-TCP measures what retransmission eventually salvaged, not
link delivery. Loss measurement needs raw datagrams and a counter at the
receiving end.

Timing matters: first association is imminent, blocked only on a bulk
decoupling capacitor and antenna confirmation (docs/feature-roadmap.md:113-118).
The node-side client has to be written and flashed *before* the link exists
— so the wire format must be published now, with the gateway responder live
and loopback-tested as the target. This is roadmap v2 item 19
(docs/feature-roadmap.md:148-156), adversarially verified 2026-08-05:
nothing UDP and nothing receiver-counted exists in either repo today.

## Current state

Verified this session, both repos:

**Gateway (this repo):**

- `systemd/halow-iperf3.service:6-10` — `ExecStart=/usr/bin/iperf3 -s`,
  `User=halow-ui`, `NoNewPrivileges=yes`. Cleartext measurement server,
  already shipped and accepted. This is the precedent the UDP responder
  follows (unprivileged, measurement-only, no secrets in reach).
- `ui/halow_ui.py:512` — `THROUGHPUT_LOG = /var/lib/halow/throughput.jsonl`;
  `:518-548` POST `/api/halow/throughput` shells `iperf3 -c <target> -J`,
  appends a result line to the jsonl; `:551-560` GET returns the last 50
  runs. This is the jsonl-history pattern to copy. It is TCP-only and
  gateway-initiated — useless for loss, impossible for the node to answer.
- `ui/halow_ui.py:1083-1099` — Debug tab: throughput card + station events +
  service logs. The new card lands here.
- `scripts/halow-mon:104-133` — per-minute `iw dev halow0 station dump`
  sampler into `/var/lib/halow/stations.jsonl` (per-MAC `signal_dbm`,
  `tx_mbps`, `rx_mbps`, packet/retry/fail counters), ring-pruned at
  `:128-130`. PHY rate is therefore already being sampled; the link tester
  reads it, never invents it.
- `scripts/halowctl:163-170` — `capture` clamps to 3-30 s AND 5000 frames.
  The bounding pattern every new operation must follow.
- `config/nftables-halow.conf:5-32` — table `ip halow` has `forward`,
  `prerouting`, `postrouting` chains only. There is **no input chain**, so a
  UDP listener on the gateway itself needs no firewall change (and gets no
  firewall protection — the responder must do its own ingress filtering).
- `config/sudoers-halow-ui:1-21` — the UI's entire privileged surface.
  Nothing in this issue needs a new entry (see privilege model).
- `scripts/deploy.sh:66-70,81` — install pattern for scripts + units,
  `/var/lib/halow` owned `halow-ui:halow-ui` (:70), enable list (:81).
- Repo-wide search: the only "udp" hits are the port-forward proto
  whitelist (`scripts/halowctl:323`, `ui/halow_ui.py:1139`). No UDP
  measurement machinery exists.

**Nodes (mesh-v4):**

- `tools/measure.py:119-154` — the `Delivery` class: `sent`, `received`,
  `counted_at`, `retries`, `duplicates`; `validate()` at `:144-148` refuses
  `counted_at != "receiver"`; `:151-154` refuses `received > sent`
  (duplicates go in `duplicates`). `:53-61` — `MIN_N_FOR_POINT_ESTIMATE = 3`
  and `SPREAD_REQUIRES_RANGE = 0.10`; `:241-245` — n<3 may be recorded only
  if the individual samples travel with it.
- `tools/baseline.py:14-16` — at n=8 one lost packet is 12.5%; `:60-73`
  Wilson CI for delivery; `:151-162` reports the tightest interval whose CI
  *floor* clears 95% and states what effect size is even detectable.
- `firmware/ADMIN-API.md:63-68` — the sender-counter lie, verbatim source of
  the receiver-side rule.
- `firmware/halow/README.md:403-405` — milestone (f). `:12` — 2.06 Mbit/s
  raw SPI at 4 MHz. `:64-66` — overlay ships 1 MHz SPI (so the *bus* ceiling
  today is roughly 0.5 Mbit/s; the tester must not assume line-rate arrival).
- No iperf client exists anywhere in the vendored halow component or the
  Meshtastic tree (searched; only the README milestone mentions it).

## Design

A single small daemon, `halow-linkd`, stdlib-Python, running unprivileged as
`halow-ui`, owning two UDP ports on the gateway:

| port | mode | what the gateway does |
|---|---|---|
| 5202/udp | **sink** | counts sequenced datagrams at the receiver, finalizes a jsonl record per session |
| 5203/udp | **echo** | returns *exactly* the datagram that arrived, to the source it arrived from, rate-bounded |

Sink measures uplink (node→AP) delivery and goodput with the count taken at
the receiver — the direction milestone (f) needs first. Echo gives the node
RTT and round-trip delivery, counted by the node at *its* receiving end;
the gateway keeps no per-session state for echo. Neither port carries a
control surface: no datagram can start, stop, or configure anything; an
echo reply is byte-identical to the request (no amplification), and
anything failing magic validation is dropped silently.

**Cleartext is deliberate, and bounded.** Roadmap v1 rejected a *cleartext
admin API* (docs/feature-roadmap.md:89-90); this responder is not one — it
has no verbs. Raw UDP is required because TLS/TCP retransmission masks the
loss being measured; the shipped cleartext iperf3 server is the accepted
precedent. The responder never reads `/etc/halow/halow.env`, never logs
payload bytes, and its records hold only IPs, MACs, counters, and rates.

### Wire format (v1 — published, frozen; the node client codes against this)

All integers little-endian. One 20-byte header on both ports:

```
offset  size  field        meaning
0       4     magic        ASCII "HLT1"
4       4     session_id   uint32, random nonzero, chosen by the sender per run
8       4     seq          uint32, 0-based, increments by 1 per datagram
12      4     total        uint32, declared datagrams in this session (1..5000)
16      4     sender_ms    uint32, sender millisecond clock, wraps; OPAQUE to
                           the gateway, echoed back verbatim on port 5203
20      0..1380  payload   arbitrary padding to reach the size under test
```

- Datagram size 20..1400 bytes (1400 keeps clear of IP fragmentation at
  MTU 1500 — a fragmented test measures reassembly, not link delivery).
- `sender_ms` exists so a RAM-tight ESP32 can compute RTT from an echo
  without keeping a per-seq TX-time table. It is never interpreted by the
  gateway and must never be used for one-way delay (no common clock).
- A sink session is keyed `(src_ip, session_id)` and finalized when the
  first of these occurs: all `total` unique seqs arrive; 10 s pass with no
  datagram; the session reaches 120 s of age.

Bounds (verifier requirement — every knob capped):

| knob | bound | why |
|---|---|---|
| datagram size | 20..1400 B | header + no fragmentation |
| `total` per session | 1..5000 | mirrors the 5000-frame capture cap (scripts/halowctl:167) |
| session idle timeout | 10 s | a 1 MHz-SPI, LoRa-culture sender paces hard |
| session max age | 120 s | baseline.py-shaped run length; state is a 625-byte seq bitmap, not a pcap, so the capture 30 s bound does not apply |
| concurrent sink sessions | 8 total, 2 per source | new sessions past the cap are dropped and counted |
| echo rate | token bucket 600 datagrams/s per source, burst 200 | ~6.7 Mbit/s at 1400 B — above anything the node can send; bounds abuse, not measurement |

### Binding and ingress discipline

Both sockets bind `0.0.0.0` with `IP_PKTINFO` enabled, and every datagram is
accepted only if its ingress interface is `halow0` (with destination
10.117.0.1) or `lo` (self-test). Everything else — e.g. a LAN host hitting
192.168.51.202:5202 — is dropped and counted in `drops.wrong_iface`. Naive
`bind(10.117.0.1)` was rejected: it races halow0 bring-up at boot (the
driver may not have probed), and it silently breaks the loopback self-test.
Per-packet enforcement plus a visible drop counter is stricter than a bind
and auditable. Since `config/nftables-halow.conf` has no input chain, this
check is the *only* ingress filter — it is load-bearing, and acceptance
tests exercise it.

### Records — measure.py-shaped jsonl

One line per finalized sink session, appended to
`/var/lib/halow/linktest.jsonl` (ring-pruned like stations.jsonl,
scripts/halow-mon:128-130 pattern; keep last 2000 lines):

```json
{"v": 1, "kind": "udp-sink", "t": 1754424000,
 "src_ip": "10.117.0.50", "src_mac": "aa:bb:cc:dd:ee:ff", "iface": "halow0",
 "session_id": 3735928559, "size_bytes": 200,
 "received_unique": 953, "duplicates": 2, "reordered": 11,
 "completed": false, "elapsed_s": 42.11, "mtu": 1500,
 "goodput_bps": 36110.0,
 "phy_rate_bps": 4000000.0, "phy_rate_source": "iw",
 "delivery": {"sent": 1000, "received": 953, "counted_at": "receiver",
              "ratio": 0.953, "retries": 0, "duplicates": 2}}
```

Field rules, each inherited from a mesh-v4 retraction:

- `delivery.counted_at` is always `"receiver"` — the gateway counted what
  arrived. `delivery.sent` is the header's declared `total`: a sender claim,
  and honestly labeled as such (`completed:false` records a sender that
  stopped early or a tail that never arrived; the arrival count is truth
  either way).
- `received` counts **unique** seqs; duplicates are recorded separately —
  measure.py:151-154 refuses `received > sent`.
- `goodput_bps` = unique payload bytes × 8 / elapsed, where elapsed is
  first-arrival to last-arrival **at the receiver**. Null when
  `received_unique < 50` or `elapsed_s < 0.5` — a rate from a handful of
  datagrams is the small-n trap (ui/halow_ui.py:620 already annotates ping
  this way).
- `phy_rate_bps` from `iw dev halow0 station dump` rx bitrate for the
  source MAC at finalize time (readable unprivileged — ui/halow_ui.py:195
  already does it as halow-ui), falling back to the newest stations.jsonl
  entry (≤60 s old, scripts/halow-mon:104-133); `phy_rate_source` records
  which. Null on loopback. Goodput and PHY rate stay separate fields, never
  conflated (measure.py:8 — the 620 bps retraction).
- `iface` makes a loopback bench number impossible to pass off as a HaLow
  number — context that cannot be omitted.
- These records are the *receiver-side half* of a full measure.py `Record`;
  `conditions`, `baseline_restored`, and `node_state` come from the
  node-side runner when it assembles one. The delivery/goodput vocabulary
  is shared with #20's windows — coordinate before renaming anything.

Responder state (active sessions, drop counters, start time) goes to
`/var/lib/halow/linktest-state.json`, written tmp+`os.replace` from day one
— do not import the non-atomic-write defect #22 is fixing
(scripts/halow-mon:144) into new code.

### API

- `GET /api/halow/linktest` — last 50 records + responder state + a
  per-source aggregate over 24 h. The aggregate enforces n-discipline: for a
  `(src_ip, size_bytes)` group with n < 3, no mean is printed — the raw
  samples are returned instead (measure.py:241-245); with relative spread
  > 0.10 the range is reported alongside the mean (measure.py:59-61).
- `POST /api/halow/linktest/selftest` — bounded loopback run: shells
  `halow-linkd send --target 127.0.0.1 --count 500 --size 200 --pps 400`
  (≈1.5 s), waits for the record, returns it. No sudo — sending UDP is
  unprivileged. Capped at count≤1000, size≤1400 so the threaded Flask
  worker (ui/halow_ui.py:1273) is never held long.

### Where each piece lives

| piece | home |
|---|---|
| responder daemon + reference client + selftest | `scripts/halow-linkd` (new, python3 stdlib only) |
| service unit | `systemd/halow-linkd.service` (new; clone of halow-iperf3.service shape: `User=halow-ui`, `NoNewPrivileges=yes`, `Restart=on-failure`) |
| wire-format doc | `docs/udp-linktest-protocol.md` (new — the contract the node team codes against) |
| records / state | `/var/lib/halow/linktest.jsonl`, `/var/lib/halow/linktest-state.json` (dir already `halow-ui`-owned, deploy.sh:70) |
| API + Debug card | `ui/halow_ui.py` |

### Privilege model

Everything runs as `halow-ui`. UDP ports 5202/5203 are unprivileged; the
records directory is already owned by `halow-ui`; `iw ... station dump` and
`ip neigh` read without root. **Zero additions to
`config/sudoers-halow-ui`** — the first roadmap-v2 feature to need none.
The unit gets `NoNewPrivileges=yes` like halow-iperf3.

## Implementation steps

Each step is one commit, in order.

1. **Publish the contract.** Write `docs/udp-linktest-protocol.md`: the
   20-byte header table, ports, bounds table, session lifecycle, the jsonl
   record schema with the JSON example above, the receiver-side rationale
   (quote the acked=1000/424-arrived incident with citations), and a "node
   client requirements" list: random nonzero session_id per run, monotonic
   seq from 0, declared total honest, pacing chosen by the node, echo RTT
   from `sender_ms`. Cross-link from `docs/feature-roadmap.md` item 19.
2. **`scripts/halow-linkd` — responder core.** `serve` subcommand (default):
   two sockets, `IP_PKTINFO` ingress check (accept halow0→10.117.0.1 or lo;
   count `wrong_iface` otherwise), header validation (magic, size 20..1400,
   total 1..5000 — count `bad_magic`, `too_big`, `bad_total`), sink session
   table keyed `(src_ip, session_id)` with a 625-byte seq bitmap per
   session, caps (8 sessions, 2/source — count `capacity`), finalize on
   complete/10 s idle/120 s age, echo path with per-source token bucket
   (600/s, burst 200 — count `echo_rate`). On finalize: resolve src MAC via
   `ip -j neigh`, PHY rate via `iw` then stations.jsonl fallback, read
   halow0 MTU from `ip -j link`, append the record, prune the ring at 2000
   lines, rewrite state atomically. Payload bytes are never logged.
3. **`scripts/halow-linkd` — reference client + selftest.** `send`
   subcommand: `--target --port --count --size --pps --session-id --drop P`
   (deliberately skip a fraction of seqs — for proving receiver-side
   counting) and `--mode sink|echo`; prints sender-side stats explicitly
   marked `[C] sender-side — not a result; confirm at the receiver`. This
   doubles as the executable specification for the ESP32 client.
   `selftest` subcommand: loopback sink run + echo run + bounds probes,
   exits nonzero on any failure (deployable as a bench check like
   measure.py selftest, measure.py:378-450).
4. **Unit + deploy.** Add `systemd/halow-linkd.service`; extend
   `scripts/deploy.sh`: install `scripts/halow-linkd` to
   `/usr/local/bin/` (:68-69 pattern), add the unit to the install list
   (:67) and to the enable line (:81).
5. **API.** In `ui/halow_ui.py`: `GET /api/halow/linktest` (history + state
   + n-disciplined aggregate) and `POST /api/halow/linktest/selftest`, next
   to the throughput endpoints (:518-560). Add `"halow-linkd"` to
   `LOG_UNITS` (:514-515) and to the `/api/diag` services list (:835-836).
6. **Debug tab card.** In `debug()` (ui/halow_ui.py:1083-1099): "UDP link
   test (receiver-counted)" card — recent sessions table (when, source,
   iface, size, received/declared, delivery %, goodput, PHY rate), responder
   drop counters, self-test button, and one dim line stating the ports and
   pointing at `docs/udp-linktest-protocol.md`.
7. **Bench verification pass.** Run every "testable today" criterion below
   on the Pi; record the loopback numbers (marked loopback) in the commit
   message; flip roadmap item 19 to MACHINERY DONE with item 1's wording
   discipline (docs/feature-roadmap.md:27: real [M] numbers await the
   first station).

## Surface changes

**New API endpoints**

| method | path | in | out |
|---|---|---|---|
| GET | `/api/halow/linktest` | — | `{sessions:[...], aggregate:{...}, responder:{active_sessions, drops:{bad_magic, too_big, bad_total, wrong_iface, capacity, echo_rate}, since}}` |
| POST | `/api/halow/linktest/selftest` | optional `count` (≤1000), `size` (≤1400) | the finalized loopback record, or `{error}` |

**Changed API endpoints**

| path | change |
|---|---|
| `/api/logs` | `halow-linkd` added to the unit whitelist |
| `/api/diag` | `halow-linkd` in the services block |

**New network listeners** (gateway-terminated, measurement-only)

| port | proto | mode |
|---|---|---|
| 5202 | udp | sink |
| 5203 | udp | echo |

**New commands** (not halowctl — no root involved)

| command | purpose |
|---|---|
| `halow-linkd serve` | the daemon (systemd runs this) |
| `halow-linkd send ...` | reference client / bench sender |
| `halow-linkd selftest` | bounded loopback verification, nonzero exit on failure |

**systemd units**

| unit | change |
|---|---|
| `halow-linkd.service` | new; `User=halow-ui`, `NoNewPrivileges=yes`, enabled by deploy.sh |

**UI**

| element | change |
|---|---|
| Debug tab | new "UDP link test" card: sessions, drop counters, self-test button |

**Config files**: none changed. `config/sudoers-halow-ui`: explicitly
unchanged. `config/nftables-halow.conf`: unchanged (no input chain exists;
ingress filtering is in the daemon).

## Testing & acceptance criteria

### Testable today (pre-association)

All receiver-side, all bounded; run on the Pi.

1. `halow-linkd selftest` exits 0 and its record shows: 500/500 unique
   received, `duplicates: 0`, `counted_at: "receiver"`, `iface: "lo"`,
   `phy_rate_bps: null`, goodput within sane bounds for 400 pps × 200 B.
2. **Receiver-counting proof** (the ADMIN-API.md:63-68 lesson as a test):
   `halow-linkd send --count 1000 --drop 0.05` declares `total=1000` but
   transmits ~950. The record must show `received_unique ≈ 950`,
   `delivery.ratio ≈ 0.95`, `completed: false` — the gateway counted
   arrivals, it did not believe the header.
3. Echo: a `send --mode echo` run receives byte-identical replies (compare
   payload hashes) and computes RTT from `sender_ms`; a flood above 600/s
   shows `drops.echo_rate` incrementing and replies capped; `echo -u`/`nc`
   garbage to 5203 gets **no** reply and increments `bad_magic`.
4. Bounds: a 1401-byte datagram → dropped, counted; `total=6000` → dropped,
   counted; a 9th concurrent session (script 9 session_ids in parallel) →
   dropped, `drops.capacity` counted; an abandoned session finalizes with
   `completed:false` after the 10 s idle timeout; nothing lives past 120 s.
5. Ingress discipline: from a LAN host, `send --target 192.168.51.202`
   records nothing and `drops.wrong_iface` increments; same for
   `--target 10.117.0.1` routed in via eth0 — the check is ingress
   interface, not destination.
6. Service: `halow-linkd.service` starts and stays up with halow0 absent
   (unload the module: `halowctl probe` covers reload), runs as `halow-ui`,
   and `sudo -l -U halow-ui` output is unchanged from before this issue.
7. Record compatibility: a script assembles a full measure.py `Record` from
   one jsonl line plus stub node-side fields, and
   `mesh-v4/tools/measure.py validate` passes it; flipping `counted_at` to
   `"sender"` makes validate refuse it.
8. API: `GET /api/halow/linktest` returns the loopback sessions; with only
   2 sessions for a group the aggregate contains samples and **no mean**;
   the selftest POST returns inside 10 s. Payload bytes appear nowhere in
   records, state, or journal (`journalctl -u halow-linkd | grep -c <payload
   marker>` = 0).

### Needs a joined station

9. First real run: node client fires ≥3 sink sessions (same size, same
   pacing) node→gateway. Records show `iface: "halow0"`, a real `src_mac`,
   `phy_rate_source: "iw"`. Delivery reported with n and range; goodput with
   spread — if relative spread > 0.10, the range is the headline
   (measure.py:59-61). These are the first `[M]` HaLow uplink numbers.
10. Echo from the node: RTT distribution over ≥100 datagrams, computed at
    the node from `sender_ms`.
11. Divergence check: compare the node's sender-side count against
    `received_unique` at range. They are *expected* to diverge (the
    424/1000 regime); the acceptance is that the record survives and labels
    it, not that the link is clean.
12. Milestone (f) hand-off: mesh-v4 gets gateway-verified numbers to set
    against its LoRa/ESP-NOW ladder figures. The comparison itself is
    mesh-v4 work (their harness, their baseline discipline).

## Out of scope

- **The ESP32 client implementation** — mesh-v4 firmware work. This issue
  ships its executable specification (`halow-linkd send`) and the frozen
  wire format, nothing more.
- **Downlink (AP→STA) sink** — needs a node-side sink; gateway `send` can
  blast at one the day it exists, and its sender-side stats stay marked
  `[C]` until the node's receiver count comes back.
- **TRANSPORT_HALOW rung cost, windowed delivery, damping** — issue #20;
  this issue only supplies the record vocabulary #20 windows over.
- **Replacing or removing the iperf3 unit** — TCP throughput between
  Linux-class hosts stays useful; the two coexist.
- **Any remote control surface** — no datagram starts, stops, or configures
  anything; parameters live in the sender.
- **One-way latency** — no common clock; `sender_ms` is for RTT at the
  node only.

## Risks & gotchas

- **`delivery.sent` is a sender claim.** Only `received_unique` is measured
  here; a sender that under-declares `total` inflates its ratio. The
  defense is mesh-v4's: the node-side runner publishes through measure.py,
  where its own conditions are recorded. The gateway record labels
  `completed` honestly and never extrapolates.
- **Loopback numbers are bench numbers.** The `iface` field exists so a
  `lo` goodput can never be quoted as a HaLow result. Reviewers: reject any
  `[M]` claim citing a record without `iface: "halow0"`.
- **The ingress check is the only filter.** No nftables input chain
  protects these ports (config/nftables-halow.conf has forward/nat only).
  If the per-packet check regresses, the responder is LAN-exposed —
  criterion 5 is not optional, and `drops.wrong_iface` staying visible in
  the Debug card is part of the design.
- **Do not assume line-rate arrival.** The node bus runs 1 MHz SPI today
  (~0.5 Mbit/s ceiling; 2.06 Mbit/s only at 4 MHz — mesh-v4
  firmware/halow/README.md:12,64-66) and mesh-v4 senders pace hard. Hence
  10 s idle / 120 s age instead of the capture-style 30 s; a session
  costs a 625-byte bitmap, not a pcap.
- **Fragmentation trap**: the 1400 B cap assumes MTU 1500 on halow0. The
  record carries `mtu` at finalize; if the driver reports smaller, sizes
  above `mtu-28` measure IP reassembly, not the link — the protocol doc
  says so and the Debug card shows the recorded mtu.
- **halow-mon file contention (#22)**: linktest.jsonl uses the same
  read-all/rewrite prune as stations.jsonl (scripts/halow-mon:128-130).
  The state file is atomic from day one; when #22 lands its shared atomic
  helper, adopt it for the prune too. Coordinate with #20/#22, which touch
  the same directory and conventions (docs/issues/README.md:52-54).
- **Record-shape coordination with #20**: `delivery`/goodput field names
  here become #20's window vocabulary. Rename before #20 starts or not
  at all.
- **Duplicates under retry**: 802.11 MAC retries can deliver duplicates
  upward; counting unique seqs with `duplicates` reported separately keeps
  `received ≤ sent` true (measure.py:151-154 refuses the alternative).
- **Port selects mode**: a sink-intended header sent to 5203 is valid by
  format and gets echoed — harmless, but the protocol doc must say port
  selects mode so nobody "discovers" mode negotiation that isn't there.
