# UDP link-test protocol v1 (frozen)

The wire contract between HaLow stations (mesh-v4 ESP32 nodes) and the
gateway's `halow-linkd` responder. Published before first association so
the node client can be written against a live target — the gateway side
is deployed and loopback-verified.

**Why raw UDP with receiver-side counting:** sender counters lie — at
75 ft an ESP-NOW sender reported `acked=1000 failed=0` while the receiver
logged 424 of 1000 arriving (mesh-v4 `firmware/ADMIN-API.md:63-68`), and
mesh-v4 `tools/measure.py` hard-refuses any delivery record whose
`counted_at` is not `"receiver"`. TCP (iperf3) retransmits the loss away
and measures what salvage achieved, not link delivery. So: raw datagrams,
counted where they arrive.

## Ports

| port | mode | gateway behavior |
|---|---|---|
| 5202/udp | sink | counts sequenced datagrams at the receiver; one jsonl record per session |
| 5203/udp | echo | returns exactly the datagram that arrived, to its source, rate-bounded |

**Port selects mode.** There is no mode negotiation; a sink-shaped header
sent to 5203 is simply echoed. Neither port carries any control surface —
no datagram starts, stops, or configures anything.

## Header (20 bytes, all integers little-endian)

| offset | size | field | meaning |
|---|---|---|---|
| 0 | 4 | magic | ASCII `HLT1` |
| 4 | 4 | session_id | uint32, random **nonzero**, chosen by the sender per run |
| 8 | 4 | seq | uint32, 0-based, +1 per datagram, `< total` |
| 12 | 4 | total | uint32, declared datagrams in this session (1..5000) |
| 16 | 4 | sender_ms | uint32 sender millisecond clock, wraps; OPAQUE to the gateway, echoed verbatim on 5203 |
| 20 | 0..1380 | payload | arbitrary padding to reach the size under test |

- Datagram size 20..1400 bytes. 1400 stays clear of IP fragmentation at
  MTU 1500 — a fragmented test measures reassembly, not link delivery.
  The finalize record carries the interface `mtu`; sizes above `mtu-28`
  are measuring the wrong thing.
- `sender_ms` lets a RAM-tight ESP32 compute RTT from an echo without a
  per-seq TX-time table. Never use it for one-way delay: no common clock.

## Sink session lifecycle

Keyed `(src_ip, session_id)`. Finalized at the first of: all `total`
unique seqs arrive (`completed: true`); 10 s with no datagram; 120 s age
(both `completed: false`). Caps: 8 concurrent sessions, 2 per source;
past the cap datagrams are dropped and counted (`drops.capacity`).

Echo: token bucket 600 datagrams/s per source, burst 200. Replies are
byte-identical (no amplification).

**Ingress**: only datagrams arriving on `halow0` addressed to 10.117.0.1,
or on loopback (self-test), are accepted. Anything else is dropped and
counted in `drops.wrong_iface`.

## The record (one jsonl line per finalized sink session)

`/var/lib/halow/linktest.jsonl`, served at `GET /api/halow/linktest`:

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

Field rules (each inherited from a mesh-v4 retraction):

- `delivery.counted_at` is always `"receiver"`. `delivery.sent` is the
  header's declared `total` — a sender claim, honestly labeled;
  `received_unique` is what actually arrived.
- `received` counts unique seqs; duplicates (802.11 MAC retries can
  deliver them) are separate, keeping `received <= sent` true.
- `goodput_bps` = unique payload bytes x 8 / (first-to-last arrival at
  the receiver); null when `received_unique < 50` or `elapsed_s < 0.5`
  (a rate from a handful of datagrams is the small-n trap).
- `phy_rate_bps` from `iw station dump` for the source MAC (fallback:
  newest stations.jsonl entry <= 60 s), source recorded. Goodput and PHY
  rate are separate, never conflated.
- `iface` makes a loopback bench number impossible to quote as a HaLow
  result. Reject any [M] claim citing a record without `iface: "halow0"`.

## Node client requirements

1. Random **nonzero** `session_id` per run; monotonic `seq` from 0;
   `total` declared honestly.
2. Pacing is the sender's choice (the gateway never assumes line rate —
   the node bus runs 1 MHz SPI today, ~0.5 Mbit/s ceiling).
3. Echo RTT computed at the node from `sender_ms`; echo delivery counted
   at the node (it is the receiver of the replies).
4. For measure.py-grade results: >= 3 sessions per condition, report
   spread; the gateway record is the receiver-side half of a full
   `Record` — `conditions`, `baseline_restored`, `node_state` come from
   the node-side runner.
5. Reference implementation: `halow-linkd send` (this repo) — the
   executable specification, including `--drop P` for proving
   receiver-side counting end-to-end.
