# Gateway audit and feature roadmap

Roadmap v1 (below) is closed — every item DONE or explicitly rejected.
**Roadmap v2 starts at item 16** at the end of this file: 15 features
derived from a second audit of the mesh-v4 clients (2026-08-05, all
approved), prioritized for the imminent first ESP32 association.

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
| A4 | ~~no auth throttle~~ FIXED: per-IP exponential lockout | done |
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

8. **Config snapshot/diff** — DONE 2026-08-05 (halowctl diff compares deployed vs repo, env by KEY SET only so no secret is ever compared; halowctl snapshot archives root-only. Found real drift on first run). — `halowctl snapshot|diff`: /etc/halow (sans
   secrets) vs repo defaults; the bench's config-drift lesson says the
   drift is found only when someone diffs.
9. **S1G channel scan / survey** — `iw dev halow0 scan` via UI to see
   band occupancy before pinning channels; the LoRa co-siting hazard
   lives in the same 902–928 MHz.
10. **Fixed-MCS test knobs** — DONE 2026-08-05 (halowctl rate show/set/auto over the live module params; effect on this build UNVERIFIED until a station exists, and the restore path is explicit). — the driver already exposes
    `enable_fixed_rate`/`fixed_mcs`/`fixed_bw`; surface via
    `halowctl rate` for range testing, mirroring the nodes'
    modem-preset experiments. Restore the baseline explicitly after
    (the ESP-NOW rate-harness lesson: a control that drifts is the bug).
11. **Packet capture helper** — DONE 2026-08-05 (halowctl capture N, 3-30s and 5000-frame capped; POST /api/diag/capture, GET downloads the pcap). — bounded tcpdump on halow0 via API
    (rotating, size-capped) for association-failure debugging: "confirm
    at the receiver" needs receiver-side eyes.
12. **NTP for the HaLow net** — DONE 2026-08-05 (chrony allow 10.117/10.42, DHCP option ntp-server 10.117.0.1; synced stratum 3). — chrony serving 10.117.0.0/24; nodes
    without GPS lock skip time-based pruning; cheap log-correlation win.
13. **mDNS** — DONE 2026-08-05 (avahi host-name halow-gw; PC resolves halow-gw.local, matches cert SAN). — avahi announcing `halow-gw.local` so tools stop
    hardcoding the IP.
14. **Auth throttle (A4)** — DONE 2026-08-05 (3 free tries then exponential lockout to 5 min, per source IP, success clears; verified by locking myself out). — failure counter with a penalty window;
    keep the mesh-v4 rule in mind (a penalty shorter than the hash cost
    is invisible).
15. **DHCP reservations** — DONE 2026-08-05 (halowctl dhcp-reserve + /api/config/reservations + Config tab; add/del round-trip verified). — pin node MACs to fixed 10.117.0.x addresses
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

## Roadmap v2 (2026-08-05) — client-driven, all 15 approved

Second audit against mesh-v4 as it stands now: the ESP32 HaLow driver is
proven below RF (chip boots fw 1.17.8 inside Meshtastic), TRANSPORT_HALOW
ladder scaffolding is flashed, and first association waits only on the
bulk-decoupling capacitor + antenna confirmation. Every item below was
adversarially verified against both codebases (nothing already built,
nothing on the v1 rejected list); verifier corrections are folded in.

### Tier 1 — before the capacitor goes on (first-association payoff)

16. **Association forensics** — DONE 2026-08-05 pre-association scope
    (halow-join-log parser/verdict engine + selftest fixtures, hostapd_s1g
    logger keys accepted by the Morse fork [M], log-dhcp in BOTH dnsmasq
    templates, halow-join-watch daemon rides BindsTo restarts, 3 API
    routes + Debug-tab card; secret check measured 0 PSK hits at the new
    logger level. Marker strings stay [C] until runs 7-9 capture them from
    a real attempt — wrong-PSK, RF-death, clean join — which need a
    station). — per-MAC join transcript with
    stage-of-failure verdict (SAE commit/confirm → assoc → 4-way EAPOL →
    DHCP → first ARP/ICMP), including FAILED attempts; auto-capture an
    evidence bundle on first sight of a new MAC (first-contact pattern).
    Today halow-sta-events logs only successful connects; a half-completed
    SAE handshake leaves no record anywhere — node-side evidence vanishes
    when RF bring-up wedges its SPI slave, and halow_psk is write-only in
    node NVS, so only gateway-side SAE forensics separates wrong-PSK from
    RF-died-mid-handshake. Needs hostapd logger level raised and dnsmasq
    log-dhcp; `GET /api/halow/join-log/<mac>`. [medium/high]
17. **Pinned-scan compatibility guard** — DONE 2026-08-05 (halowctl
    check-compat + guard on set/set-profile/ssid, GET /api/halow/compat,
    UI badges + preflight confirms, STRANDS-PINNED tags, fail-closed on
    missing pinned file or non-US country; 20/20 CLI + 9/9 API acceptance
    on the Pi, verify.sh 9/9 after; both node HaLow MACs reserved so the
    guard is armed before first join — node1's MAC is derived [C], confirm
    at first boot. Incidental fix: `halowctl status` kernel-orphan warning
    false-fired on every boot — `ls A* B*` fails when either glob is
    empty; now compgen). — the node STA pins 926 MHz @4 MHz
    plus 1 MHz at 924/925/926/927; three of our four profiles (long-range
    ch45=924.5 @1 MHz, mid-range ch46 @2 MHz, max-rate ch44 @8 MHz) leave
    that set and silently strand every station, indistinguishable from the
    hardware fault. Extend confirm=1 to profile/channel/width changes that
    exit the pinned set while stations/reservations exist. Compare by
    frequency+width, NOT channel number — the two stacks number channels
    differently (Pi regdb 1 MHz = odd 43-51 at x.5 MHz; node list uses
    44/46/48/50 at integer MHz). [small/high — best ratio in the list]
18. **Per-station health ladder in halow-mon** — DONE 2026-08-05
    pre-association scope (classifier unit matrix 7/7 incl. the
    quiet/lost identical-silence pair; zero-station pass 0.20s [M];
    reserved node MACs visible as never-seen with known=true; tcpdump
    proved zero probe frames to absent stations; secrets audit clean in
    state file and API; event-log stays halow-ui-writable after root
    appends; /api/halow/link carries health with null-telemetry MACs and
    degrades to the old shape without the state file. Probe-path runs
    8-11 — first join classified, the pulled-power trap, sleep-awareness
    end-to-end, walk bound — need a station). — walk each associated MAC
    through assoc → lease → ARP → real ICMP each minute; classified
    transitions (assoc-no-lease, leased-no-arp) into station-events. The
    "associated but answers no ARP" trap stops being a manual Diag hint.
    Sleep-aware freshness at 3x the node's own expected interval
    (nodewatch rule) — interval source: /api/nodes proxy or a reservation
    field. [small/high]
19. **ESP32-class link tester** — MACHINERY DONE 2026-08-05 (halow-linkd
    sink :5202 / echo :5203, wire format v1 FROZEN in
    docs/udp-linktest-protocol.md; receiver-counting proven on-device:
    declared 1000, transmitted ~950, gateway counted 945 arrivals with
    completed=false [M loopback]; capacity/echo-rate/size/total bounds
    all exercised; LAN ingress datagrams dropped+counted 50/50 with zero
    record (the per-packet check IS the firewall); measure.py Delivery
    vocabulary validates and counted_at=sender is refused; zero sudoers
    entries. Real [M] HaLow numbers await the first station — reject any
    [M] claim citing a record without iface=halow0.) — sequenced-UDP
    sink/echo on 10.117.0.1
    with RECEIVER-side counting (mesh-v4 discipline: a sender once
    reported acked=1000/failed=0 while 424/1000 arrived), measure.py-
    shaped JSONL records + history endpoint. A full iperf3 client inside
    Meshtastic on a 1 MHz SPI link is unrealistic; this is milestone (f)'s
    real harness. Publish the wire format now so the node client is
    written against a live target. Measurement-only responder (echo
    returns exactly what arrived, no control surface), bound to halow0.
    [medium/high]
20. **Measured TRANSPORT_HALOW rung-cost endpoint** — DONE 2026-08-05
    pre-association scope (halow_rungcost pure logic 15/15 selftest:
    anchors exact — 604 kbps [M] -> cost 10 ties ESP-NOW at ESP-NOW's
    measured rate, 9 kbps [M] -> 99, floor 6 never outranks wifi=5;
    vip.py hysteresis proven incl. absent-counts-bad live on the real
    timer; corrupt state -> fresh start, monitor survives; 20x kill -9
    -> zero torn files; staleness fail-safe verified — a dead monitor
    serves healthy:false/cost:null; single-MAC body 391 B [M], inside
    the node's one-TLS-session budget. First real windows + the
    forced-bad antenna run + the iperf3 ordering cross-check need a
    station. tx_failed presence on this driver is UNVERIFIED until
    then — if absent, every window is no-counters and health is never
    earned, by design.) — `GET
    /api/halow/rungcost/<mac>`: per-MAC cost + healthy boolean from
    windowed measured rate/delivery (current link API delivery_pct is
    lifetime-cumulative — windowing is the new work), damped vip.py-style
    (demote after 2 bad windows, promote after 3 good). Closes the ladder
    doc's "never weight a rung on a datasheet number"; node consumes it at
    halowPeriodicCheck cadence within its one affordable TLS session.
    [medium/high]

### Tier 2 — unattended reliability (16 and 17 here are live defects)

21. **Time holdover DEFECT** — DONE 2026-08-05 (defect reproduced [M]:
    chrony restarted with NTP egress blocked = stratum 0, client query
    "Timeout reached"; with `local stratum 10` the same drill serves an
    accepted stratum-10 answer in ~10s. All four T3 transitions observed
    receiver-side; ref_time FREEZES at holdover entry [M] so ref_age_s
    is honest and shipped; epoch drill: 2-day offset corrected by a
    STEP (172800s) not a slew — default makestep 1 3 suffices; chrony
    stopped -> API 200 state:unknown; controlled holdover sample ->
    time_synced false in metrics [M]. Findings: fake-hwclock was NOT
    installed on this image (now installed + enabled via deploy);
    Trixie splits it into -load/-save units and masks the legacy name —
    verify.sh tests fake-hwclock-load. verify.sh 13/13. Node-side flag:
    Meshtastic ignores DHCP option 42 — node network.ntp_server must be
    set to 10.117.0.1 for T8/T9.) — chrony-halow.conf is two allow lines; with
    no `local` directive chrony refuses to serve when the gateway is
    unsynced (field power-cycle with upstream down = nodes get no time,
    skip GPS-log pruning, fill flash). Add `local stratum 10`, verify
    driftfile/fake-hwclock present, expose time_sync (real vs holdover,
    last-sync age) in /api/system + Overview. [small/medium]
22. **Storage discipline DEFECT** — DONE 2026-08-05 (both defect modes
    reproduced on the OLD code first [M]: truncated state -> silent
    counter reset; valid-partial state + AP down -> KeyError wedging the
    healer at the exact moment healing was needed. Fix: atomic_write
    (tmp+fsync+replace) at all three rewrite sites, defaults-merge
    load_state, logrotate drop-in, journald 100M/32M caps (8M in use),
    disk low-water sampling with edge-triggered events — verified exactly
    one event across two crossing runs, recovery edge after restore.
    20-run torn-file watch: zero observations, no tmp litter; ring prune
    exact at 2880 with the fresh sample as tail; /api/system disk within
    0.002%% of df. Appends stay appends by design — torn trailing lines
    are reader-tolerated and rewrites would multiply SD wear.) —
    halow-mon rewrites stations.jsonl,
    metrics.jsonl, and mon-state.json in place with open(w) every minute;
    mon-state.json is the self-healer's own state, so a brownout mid-write
    can wedge the healing loop itself. tmp+os.replace atomic writes
    (meshdata.py convention), logrotate for the unbounded
    station-events.log, journald SystemMaxUse, disk low-water in
    /api/system. [medium/medium]
23. **Kernel-upgrade safety interlock** — DONE 2026-08-05 (halow-kernel-guard:
    apt Post-Invoke hook (421ms detection, never blocks apt) + boot unit +
    restore ladder prebuilt->rebuild->hold. Drilled [M]: fake 6.99.0-test
    kernel -> orphaned -> no-source -> held with both metapackages;
    dead-kernel (morse.ko removed) -> boot check -> prebuilt restore 4s ->
    modprobe -> AP active; full driver rebuild 18s with warm work tree
    (52->58C); concurrency lock holds vs an active rebuild (and a
    lingering FAILED transient unit no longer blocks dispatch —
    reset-failed first); undervoltage gate refuses builds during
    brownout; zero PSK hits in journal+state. Two bugs found by the
    drills: pipefail double-emit corrupted the state JSON; install.sh
    run as root looked for /root/halow — the guard now runs it with the
    repo owner's HOME. verify.sh 14/14. End-to-end unattended-kernel-bump
    run (A9) awaits a joined station.) — no DKMS; every kernel bump
    orphans morse.ko and the only guard is a warning inside `halowctl
    status` a human must run — while deploy.sh apt-get installs on every
    deploy. apt Post-Invoke hook or /lib/modules path unit: auto-rebuild
    against new headers, or hold the kernel + first-class alert (UI
    banner, /api/system field, halow-mon event). [medium/high]
24. **Hardware watchdog + brownout policy** — dtparam=watchdog=on +
    RuntimeWatchdogSec so kernel hangs reboot autonomously (the
    self-healer is itself a timer-fired oneshot and dies with the timer).
    Pet-able unit is halow-ui (convert to Type=notify + sd_notify);
    halow-mon is oneshot — give its run a timeout instead. Persist a
    brownout transition ledger from the already-sampled get_throttled
    values and DECLINE high-draw ops (max-rate apply, long captures)
    while undervoltage is active. [medium/high]

### Tier 3 — fleet operations through the gateway

25. **Gateway-resident fleet health watcher** — DONE 2026-08-05
    (halow-watch daemon: serialized nodewatch-semantics poller, cheap
    plain-HTTP /json/report path + every-Nth HTTPS deep poll; /api/nodes
    now a cache read — 0.014s vs 3.6s live, 253x [M]; 20 viewer requests
    over 140s generated ZERO node SYNs [M], the only 4 being the
    watcher's own scheduled deep polls. Both real nodes healthy with
    learned reboot_count/battery (node2: 1066 lifetime reboots); broken
    token stays healthy on the auth-free cheap path — can never read as
    down; dead-url drill -> down + exponential backoff; watcher_stale
    computed by the UI from cache age; token grep 0 in files+journal;
    nodes.json schema v2 (mac + expected_interval_s, shared with 26/30)
    via halowctl node add/del/list with stdin tokens; the deploy-clobber
    amnesia bug fixed and drilled. Adopt flow + assoc cross-checks await
    a joined station.) — /api/nodes live-fetches
    every node per request (5-6 s + one of ~6 scarce TLS sessions each;
    two tabs double-tap). Port nodewatch semantics into a cached daemon:
    one node at a time, prefer plain-HTTP /json/report (costs no
    session), classify healthy/stale/wifi-down/down/unknown, 401/404/429
    = alive, reboot loops via reboot_count deltas. Key by MAC on the
    EXISTING stores — dhcp-reserve file + a new mac field in nodes.json;
    no fourth parallel inventory, nothing cryptographic (PKI nodedb stays
    rejected). One-click onboarding proposes reservation + nodes.json
    entry when an unknown MAC associates. [medium/high]
26. **Node-proxy failover + reach matrix** — DONE 2026-08-05 (two-leg
    node_get: LAN url then live HaLow lease by MAC, 5s/leg; vip.py
    damping drilled [M] — black-holed LAN -> per-leg 502s, demote after
    2, promote after 3 on restore; X-Node-Path/Via headers. /api/reach:
    ICMP + HEAD per family (plain-HTTP first, never bare connects),
    route guards — both nodes' device-stated LoRa/VIP harvested from
    live /api/ip passes and honestly no-route; 0.7s matrix, 20s
    budget/cache. Zero new privilege. HaLow-leg traversal awaits a
    leased station.) — proxy knows only static LAN
    URLs, so the Nodes tab goes blind exactly when HaLow is being the
    last-reaching rung. On LAN failure resolve the node's current
    10.117.0.x from leases by MAC (same mac field as 25 — implement
    once), retry with vip.py-style damping, report which path answered.
    `GET /api/reach`: per-node matrix across LAN IP / HaLow lease /
    firmware VIP, ICMP + HTTP-HEAD-never-bare-connect. [medium/medium]
27. **Encrypted off-device backup/restore** — snapshot/diff lives on the
    same SD card whose death is the disaster; profile, SSID, SAE
    passphrase, reservations, forwards, auth hashes, TLS key, node
    tokens exist only there. `halowctl backup` = age/gpg-encrypted
    tarball fetchable over the authenticated API; restore reproduces
    SSID+passphrase BYTE-FOR-BYTE so nodes (which can only compare
    set/unset) rejoin untouched. Encryption mandatory — the PSK has
    leaked twice via "harmless" echoes. [medium/high]

### Tier 4 — product-path groundwork

28. **Battery-ready AP session knobs** — vendored hostapd supports
    ap_max_inactivity/bss_max_idle/max_acceptable_idle_period with
    per-STA S1G long-idle; halowctl gen writes none (300 s default).
    Not urgent today (hostapd polls before deauth; nodes pin PS off) —
    becomes load-bearing the day node power-save lands for the asset-tag
    budget (~1.7 y @ 3000 mAh). Battery overlay in halowctl gen + DTIM,
    deauth REASON log parsed from the hostapd journal (the action-script
    hook never sees reason codes), per-STA PS state in /api/halow/link.
    [small/medium]
29. **Trail-cam image ingest sink** — terminal destination for frames:
    authenticated HTTPS POST (raw JPEG + sha256 verified on receipt),
    bounded on-disk ring under /var/lib/halow (capture.pcap-style cap),
    list/download API + simple gallery. Terminal sink for the operator,
    explicitly NOT the rejected store-and-forward relay — no onward
    queueing. LAN-testable with curl today; full value gated on the node
    TX datapath. [medium/high]
30. **Station presence ledger + check-in contract** — per-node expected
    interval (nodes.json), adherence computed over recorded
    joins/leases/stations.jsonl with dead-vs-quiet discipline, plus
    authenticated `POST /api/checkin` (push-on-schedule is the proven
    pattern; polling remote nodes times out). Scope honestly: serves
    mains/solar HaLow nodes (trail cam). Does NOT unblock asset-tag
    sleep validation — that watcher is a LoRa base node, and a ~5 mAh/day
    tag will never pay for association + TLS POST per wake; a base-node
    relay report is node-side work. [medium/medium]
