# Gateway audit and feature roadmap

Audited 2026-08-05 against what the mesh-v4 clients actually implement
(admin API, transport ladder, measurement discipline, health tooling).
The gateway is solid at what it does — driver, profiles, router, config
UI/API, recovery — but the clients set a higher bar in three areas:
**measurement, self-observability, and ladder integration.**

## Audit findings (current codebase)

| # | Finding | Severity |
|---|---|---|
| A1 | ~~prebuilt hexdump~~ FIXED: re-captured clean 2026-08-05 | done |
| A2 | ~~dev server single-threaded~~ FIXED: threaded=True | done |
| A3 | ~~no IP SAN~~ FIXED: SAN covers .202, 10.117.0.1, 10.42.0.1, halow-gw.local | done |
| A4 | Login has a 1 s failure delay but no failure counter/lockout; nodes run a real auth throttle. | recommended |
| A5 | `halow-sta.service` (STA mode) has never run against a real AP — untested, and the docs should say so until it is. | document |
| A6 | ~~no kernel/module mismatch detection~~ FIXED: verify.sh check + halowctl status warning | done |

## Needed features (functional gaps for the mesh)

1. **Measured link numbers** — MACHINERY DONE 2026-08-05 (iperf3 server unit + POST /api/halow/throughput + jsonl history + Debug tab; loopback-verified). Real `[M]` numbers await the first station. — every profile speed is a vendor claim `[C]`.
   Run iperf3 on the gateway (`iperf3 -s` as a unit) + an API/UI harness
   to test against associated stations and record results
   (`results/halow-throughput-*.json`, mesh-v4 `baseline.py` culture:
   same harness, adequate n, report spread). The profile table then
   carries `[M]` numbers.
2. **Transport-ladder support** — DONE 2026-08-05 (halow-mon samples per-station signal/rate/retries each minute; GET /api/halow/link[/<mac>] serves min/avg/max rate, signal, delivery%% and retry%% — populates at first join). — the nodes' future `TRANSPORT_HALOW`
   rung wants a metric "derived from measured rate and delivery"
   (transport-ladder doc). Expose `GET /api/halow/link/<mac>`: current
   rate, RSSI, retry/fail counters, and a short history — shaped so a
   node or the operator can derive the rung cost without guessing.
3. **Station lifecycle events** — DONE 2026-08-05 (hostapd_cli hook → /var/lib/halow/station-events.log, GET /api/halow/events, Debug tab; ctrl socket group-granted). — hostapd_cli hook logging
   AP-STA-CONNECTED/DISCONNECTED with timestamps; `GET /api/halow/events`;
   UI surfaces joins/leaves. The first real node join should
   self-document the way first contact did.
4. **Gateway metrics with history** — DONE 2026-08-05 (halow-mon timer: 1/min ring, /api/metrics with low-water marks + uptime%, Overview card). — nodes expose `/api/metrics` with
   reboot reasons and low-water marks. Pi equivalent: ring of CPU, temp,
   mem, **`vcgencmd get_throttled` undervoltage flags** (this bench has
   brownout history and the module TX-bursts on the 3V3 rail), station
   count, per-service restart counts. Read the low-water mark, not the
   current value.
5. **Logs API + Debug tab** — DONE 2026-08-05 (GET /api/logs?unit=..., whitelisted units incl. kernel). — `GET /api/logs?unit=halow-ap&n=200`
   wrapping journalctl, so remote debugging doesn't need SSH. The nodes
   had this from day one.
6. **Kernel/module mismatch guard (A6)** — boot-time check comparing
   `uname -r` against the built module; UI warning banner + halowctl
   warning. Pair with an `install.sh --driver-only` hint.
7. **Self-healing health monitor** — DONE 2026-08-05 (same daemon: AP-beaconing + dnsmasq + upstream ICMP checks, bounded restarts with counters; heal path live-tested). — timer that verifies the AP is
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
12. **NTP for the HaLow net** — DONE 2026-08-05 (chrony allow 10.117/10.42, DHCP option ntp-server 10.117.0.1; synced stratum 3). — chrony serving 10.117.0.0/24; nodes
    without GPS lock skip time-based pruning; cheap log-correlation win.
13. **mDNS** — DONE 2026-08-05 (avahi host-name halow-gw; PC resolves halow-gw.local, matches cert SAN). — avahi announcing `halow-gw.local` so tools stop
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

## Diagnostic tools (added 2026-08-05, prioritized)

| Pri | Tool | Motivating lesson |
|---|---|---|
| 1 | ~~Ping suite~~ DONE | small-n loss is not a rate |
| 1 | ~~Neighbor/ARP view~~ DONE | "associated but answers no ARP" trap |
| 1 | ~~One-shot diag bundle~~ DONE | the nodes most-used endpoint |
| 2 | TCP service check via HTTP HEAD (never bare connects) | bare :443 connects crashed nodes |
| 2 | Channel utilization (`iw survey dump`) | LoRa co-sited in 902–928 |
| 2 | morse_cli chip counters via API | confirm at the receiver |
| 2 | Undervoltage/throttle decode + history | two boards browned out on this bench |
| 3 | conntrack flow view, nft rule hit counters | "is traffic hitting this rule" |
| 3 | DNS resolution check | resolver misconfig looks like an outage |
| 3 | Service flap counters (systemd NRestarts) | quiet hourly restarts are invisible |
