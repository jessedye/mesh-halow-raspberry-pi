# Gateway audit and feature roadmap

Audited 2026-08-05 against what the mesh-v4 clients actually implement
(admin API, transport ladder, measurement discipline, health tooling).
The gateway is solid at what it does — driver, profiles, router, config
UI/API, recovery — but the clients set a higher bar in three areas:
**measurement, self-observability, and ladder integration.**

## Audit findings (current codebase)

| # | Finding | Severity |
|---|---|---|
| A1 | `prebuilt/morse.ko` still contains the debug MISO hexdump (verified via `strings`) — a restore from it floods the kernel log. Re-capture from the current clean build. | fix now |
| A2 | The console runs Flask's dev server (`app.run`), single-threaded: one slow node proxy call stalls the whole UI — the mesh-v4 "your own diagnostics are load" lesson applies. Move to waitress (apt: `python3-waitress`). | fix soon |
| A3 | UI TLS cert has no IP SAN, so browsers warn forever. Regenerate with `subjectAltName=IP:192.168.51.202`. | polish |
| A4 | Login has a 1 s failure delay but no failure counter/lockout; nodes run a real auth throttle. | recommended |
| A5 | `halow-sta.service` (STA mode) has never run against a real AP — untested, and the docs should say so until it is. | document |
| A6 | Kernel upgrades silently orphan `morse.ko` (no DKMS). Nothing detects the mismatch until the AP is dead after a reboot. | needed |

## Needed features (functional gaps for the mesh)

1. **Measured link numbers** — every profile speed is a vendor claim `[C]`.
   Run iperf3 on the gateway (`iperf3 -s` as a unit) + an API/UI harness
   to test against associated stations and record results
   (`results/halow-throughput-*.json`, mesh-v4 `baseline.py` culture:
   same harness, adequate n, report spread). The profile table then
   carries `[M]` numbers.
2. **Transport-ladder support** — the nodes' future `TRANSPORT_HALOW`
   rung wants a metric "derived from measured rate and delivery"
   (transport-ladder doc). Expose `GET /api/halow/link/<mac>`: current
   rate, RSSI, retry/fail counters, and a short history — shaped so a
   node or the operator can derive the rung cost without guessing.
3. **Station lifecycle events** — hostapd_cli hook logging
   AP-STA-CONNECTED/DISCONNECTED with timestamps; `GET /api/halow/events`;
   UI surfaces joins/leaves. The first real node join should
   self-document the way first contact did.
4. **Gateway metrics with history** — nodes expose `/api/metrics` with
   reboot reasons and low-water marks. Pi equivalent: ring of CPU, temp,
   mem, **`vcgencmd get_throttled` undervoltage flags** (this bench has
   brownout history and the module TX-bursts on the 3V3 rail), station
   count, per-service restart counts. Read the low-water mark, not the
   current value.
5. **Logs API + Debug tab** — `GET /api/logs?unit=halow-ap&n=200`
   wrapping journalctl, so remote debugging doesn't need SSH. The nodes
   had this from day one.
6. **Kernel/module mismatch guard (A6)** — boot-time check comparing
   `uname -r` against the built module; UI warning banner + halowctl
   warning. Pair with an `install.sh --driver-only` hint.
7. **Self-healing health monitor** — timer that verifies the AP is
   *beaconing* (iw), DHCP answering, and the upstream gateway reachable
   (ping 192.168.50.1 — test reachability, not driver state: the bench's
   "alive, associated, and unreachable" lesson), with bounded restarts
   and counters. Detect the event, classify separately.

## Recommended features (parity and leverage)

8. **Config snapshot/diff** — `halowctl snapshot|diff`: /etc/halow (sans
   secrets) vs repo defaults; the bench's config-drift lesson says the
   drift is found only when someone diffs.
9. **S1G channel scan / survey** — `iw dev halow0 scan` via UI to see
   band occupancy before pinning channels; the LoRa co-siting hazard
   lives in the same 902–928 MHz.
10. **Fixed-MCS test knobs** — the driver already exposes
    `enable_fixed_rate`/`fixed_mcs`/`fixed_bw`; surface via
    `halowctl rate` for range testing, mirroring the nodes'
    modem-preset experiments. Restore the baseline explicitly after
    (the ESP-NOW rate-harness lesson: a control that drifts is the bug).
11. **Packet capture helper** — bounded tcpdump on halow0 via API
    (rotating, size-capped) for association-failure debugging: "confirm
    at the receiver" needs receiver-side eyes.
12. **NTP for the HaLow net** — chrony serving 10.117.0.0/24; nodes
    without GPS lock skip time-based pruning; cheap log-correlation win.
13. **mDNS** — avahi announcing `halow-gw.local` so tools stop
    hardcoding the IP.
14. **Auth throttle (A4)** — failure counter with a penalty window;
    keep the mesh-v4 rule in mind (a penalty shorter than the hash cost
    is invisible).
15. **DHCP reservations** — pin node MACs to fixed 10.117.0.x addresses
    once nodes join, so the node proxy and ladder metrics have stable
    targets.

## Explicitly not needed (checked against the clients)

- **Cleartext API for mesh-side peers** — the nodes do HTTP-over-LoRa
  because TLS costs tens of frames there; HaLow carries TLS fine.
- **Store-and-forward queue** — a router forwards; queuing is the
  nodes' delay-tolerant job.
- **GPS/track features** — the gateway is stationary infrastructure.
- **PKI nodedb machinery** — WPA3-SAE + the nodes' own PKI covers it.
