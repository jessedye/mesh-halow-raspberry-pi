# 17. Pinned-scan compatibility guard on profile/channel/SSID changes

> Tier 1 - first-association | Effort: small | Impact: high | Depends on: none

## Problem

The ESP32 Meshtastic nodes do not scan the full US S1G band. Their HaLow
firmware pins the STA scan list to exactly five channels — 926.0 MHz at
4 MHz width plus 1 MHz entries at 924.0 / 925.0 / 926.0 / 927.0 MHz —
because a full regulatory sweep does not fit inside the runtime bus's
15-45 s clean windows (mesh-v4
`firmware/halow/component-2.11.2/sta-diagnostic-app_main.c:71-90`). The
SSID is baked in at build time (`CONFIG_WIFI_SSID`, same file line 19);
the bench value is `mesh`, matching the AP. A node can only ever find the
gateway on one of those five frequency+width pairs under that SSID.

The gateway, meanwhile, offers four one-click tuning profiles
(`config/halow-profiles.json`) and free channel/width/SSID overrides, all
applied instantly with zero validation against what the nodes can see.
Frequency math against the US rows of `docs/morse-regdb-channels.csv`
shows **three of the four profiles land outside the pinned set**:
long-range (1 MHz ch45 = 924.5 MHz — off the node's integer-MHz 1 MHz
grid), mid-range (2 MHz ch46 = 925.0 MHz — frequency present but 2 MHz
width absent from the pinned list), and max-rate (8 MHz ch44 = 924.0 MHz
— width absent). Only `balanced` (4 MHz ch48 = 926.0 MHz) matches. One
click on any of the other three silently strands every ESP32 station:
the AP beacons happily on a frequency no node will ever scan.

This matters *now* because the failure is indistinguishable from the
hardware fault currently being debugged. First association is blocked
only on a bulk decoupling capacitor + antenna confirmation; the moment
that lands, the natural bench move is "try long-range to see if range
improves" — and the node then never associates again, with symptoms
identical to a dead radio (no SAE traffic, no station dump entry, no
DHCP). The bench would burn hours re-probing SPI and reflowing caps to
chase a config change. Client-side evidence: the pinned list exists in
*two* node files (`sta-diagnostic-app_main.c:77-83` and
`pio-library/halow_transport-step-c.cpp:51-57`), both strong overrides of
the weak hook in `mmhalow.c:180-183`, i.e. this is deliberate, durable
node behavior — the gateway must respect it, not assume a full scan.

The repo already has the right pattern for changes that strand stations:
`confirm=1` on SSID and passphrase changes (`ui/halow_ui.py:378-390`) and
on reboot (`ui/halow_ui.py:504-505`). Profile/channel/width changes are
strictly the same class of hazard — worse, actually, because SSID changes
at least *look* intentional — and today they carry no guard at all.

## Current state

Everything below re-verified against the working trees this session
(2026-08-05).

**Gateway — zero guard on the mutation path:**

- `scripts/halowctl` `set-profile` (lines 99-104): validates only that the
  profile name exists in the JSON, then `sed`s `/etc/halow/halow.env`,
  regenerates the hostapd conf, and restarts `halow-ap`. No station
  check, no confirm, no compatibility logic.
- `scripts/halowctl` `set` (lines 105-117): accepts `channel=N width=M
  ssid=S`, `sed`s the env, regenerates, restarts. The channel value is
  not validated at all (any string lands in `HALOW_CHANNEL`); width is
  validated only later inside `resolve()` (lines 38-45). `ssid=` here
  changes the network identity **without any confirm** — the CLI
  equivalent of the change the UI explicitly gates.
- `scripts/halowctl` `resolve()` (lines 31-46): effective channel/width =
  profile values, overridden by `HALOW_CHANNEL`/`HALOW_WIDTH` if set,
  `op_class` re-derived from width. Any guard must evaluate the
  *post-change effective* pair through this same precedence.
- `ui/halow_ui.py` `api_halow_profile` (lines 229-234): passes the form
  value straight into `halowctl set-profile` via `sh()` — an f-string
  shell invocation whose exit code is swallowed (`sh()` returns stdout,
  `''` on failure, lines 60-66). Even if halowctl refused, this endpoint
  would report `{"applied": name}` with HTTP 200.
- `ui/halow_ui.py` `api_halow_set` (lines 254-263): same pass-through,
  and it accepts an `ssid` form key (line 258) with **no confirm** — a
  live bypass of the confirm gate that `/api/config/halow` enforces for
  the identical change (lines 378-381).
- UI JS: `setProf` (lines 1220-1221) and `setOvr` (lines 1230-1233) post
  with no confirmation and never display the command output; only
  `cfgHalow` (lines 1156-1159) shows the browser-confirm + `confirm=1`
  pattern.
- `config/sudoers-halow-ui` lines 5-6 whitelist `halowctl set-profile *`
  and `halowctl set *` for the unprivileged `halow-ui` user — the sudoers
  `*` wildcard matches additional arguments, so a new `confirm=1` trailing
  argument needs **no sudoers change**.
- Deployment: `scripts/deploy.sh:60` installs `config/halow-profiles.json`
  to `/etc/halow/`; `halow.env` is root:halow-ui 640 (deploy.sh:55-56),
  which is why `halowctl` run *as halow-ui* passes its readability check
  (halowctl:25) without elevating — relevant to the read-only check below.
- `halowctl diff` (lines 199-201) compares three repo:deployed config
  pairs; `snapshot` (lines 214-221) archives `/etc/halow`. A new config
  file must join both.

**Node — the pinned list, and the channel-numbering trap:**

- `sta-diagnostic-app_main.c:75-90` — `bench_get_channel_list()` returns
  five `mmwlan_s1g_channel` entries with country code `"US"`:

  | centre freq | width | node chan # | op class (s1g/global) |
  |---|---|---|---|
  | 926.0 MHz | 4 MHz | 48 | 24 / 70 |
  | 924.0 MHz | 1 MHz | 44 | 22 / 50 |
  | 925.0 MHz | 1 MHz | 46 | 22 / 50 |
  | 926.0 MHz | 1 MHz | 48 | 22 / 50 |
  | 927.0 MHz | 1 MHz | 50 | 22 / 50 |

- `mmhalow.c:194-209` — if the strong override returns a list, it is
  passed to `mmwlan_set_channel_list()` verbatim, bypassing the node's
  own regdb-by-country lookup entirely.
- **Spec contradiction, follow the code:** the node file's comment says
  the entries were "copied verbatim from mmregdb's US table", but the
  Pi's regdb export (`docs/morse-regdb-channels.csv`) has **no**
  integer-MHz 1 MHz US channels — US 1 MHz channels are odd 1-51 at
  x.5 MHz (lines 565-590). The node's 1 MHz entries (44/46/48/50 at
  integer MHz, op classes 22/50) numerically match the CSV's *legacy
  Australia* grid (lines 73-76). The two stacks genuinely number channels
  differently, and the node's own provenance comment is unreliable.
  Conclusion the design must honor: **compatibility is defined by
  centre-frequency + width, never by channel number.** Channel number
  comparison fails both ways here: US 8 MHz "ch44" (924.0) would
  false-match node 1 MHz "ch44" (924.0, wrong width), and node 1 MHz
  "ch48" (926.0) has no US-grid 1 MHz counterpart at all.

**Frequency math for the four profiles** (profiles from
`config/halow-profiles.json:13-42`, frequencies from the CSV US rows —
`US,1,45,...,924.5` line 587; `US,2,46,...,925.0` line 602;
`US,4,48,...,926.0` line 609; `US,8,44,...,924.0` line 612):

| profile | width | US chan | centre freq | in pinned set? |
|---|---|---|---|---|
| long-range | 1 MHz | 45 | 924.5 | NO — node 1 MHz grid is 924.0/925.0/926.0/927.0 |
| mid-range | 2 MHz | 46 | 925.0 | NO — 925.0 exists only at 1 MHz in the pinned list |
| balanced | 4 MHz | 48 | 926.0 | YES — exact match, entry 1 |
| max-rate | 8 MHz | 44 | 924.0 | NO — 924.0 exists only at 1 MHz in the pinned list |

Useful closed form, verified this session against **all 51 US rows** of
the CSV with zero mismatches [M]:
`centre_freq_mhz = 902.0 + 0.5 * s1g_chan` (US only).

## Design

One guard, enforced at the single audited choke point (`halowctl`,
running as root), with a read-only query command so the UI can warn
*before* posting. The pinned set is data, not code, so a node-side repin
is a one-file gateway update.

**1. Pinned set as data — `config/pinned-scan.json`** (new, no secrets,
mode 644, deployed to `/etc/halow/pinned-scan.json`):

```json
{
  "_doc": [
    "The ESP32 nodes' pinned HaLow scan set - what a station can actually find.",
    "Compatibility is frequency+width, NEVER channel number: the node list",
    "uses integer-MHz 1 MHz channels (44/46/48/50) while the Pi US regdb 1 MHz",
    "grid is odd 1-51 at x.5 MHz. If the node firmware repins, update this",
    "file and redeploy - nothing else changes."
  ],
  "ssid": "mesh",
  "source": {
    "repo": "mesh-v4",
    "files": [
      "firmware/halow/component-2.11.2/sta-diagnostic-app_main.c bench_get_channel_list() lines 75-90",
      "firmware/halow/pio-library/halow_transport-step-c.cpp lines 49-64"
    ],
    "captured": "2026-08-05"
  },
  "channels": [
    {"centre_freq_mhz": 926.0, "width_mhz": 4},
    {"centre_freq_mhz": 924.0, "width_mhz": 1},
    {"centre_freq_mhz": 925.0, "width_mhz": 1},
    {"centre_freq_mhz": 926.0, "width_mhz": 1},
    {"centre_freq_mhz": 927.0, "width_mhz": 1}
  ]
}
```

**2. Read-only query — `halowctl check-compat [profile=NAME | channel=N] [width=M] [ssid=S]`.**
Computes the post-change effective (freq, width, ssid) with `resolve()`
precedence, evaluates it against the pinned set, and reports presence:
stations from `iw dev halow0 station dump` (same source as halowctl
`stations`, lines 118-120), reservations from
`/etc/dnsmasq.d/halow-reservations.conf` (the file `dhcp-reserve`
manages, lines 137-161). With no candidate args it evaluates all four
profiles plus the currently active config. JSON to stdout, exit 0:

```json
{
  "presence": {"stations": 1, "reservations": 2, "guard_armed": true},
  "profiles": {
    "long-range": {"channel": 45, "width_mhz": 1, "centre_freq_mhz": 924.5, "compatible": false},
    "mid-range":  {"channel": 46, "width_mhz": 2, "centre_freq_mhz": 925.0, "compatible": false},
    "balanced":   {"channel": 48, "width_mhz": 4, "centre_freq_mhz": 926.0, "compatible": true},
    "max-rate":   {"channel": 44, "width_mhz": 8, "centre_freq_mhz": 924.0, "compatible": false}
  },
  "active": {"profile": "balanced", "channel": 48, "width_mhz": 4,
             "centre_freq_mhz": 926.0, "compatible": true},
  "candidate": {"profile": "long-range", "channel": 45, "width_mhz": 1,
                "centre_freq_mhz": 924.5, "compatible": false,
                "reason": "924.5 MHz @ 1 MHz not in pinned set (1 MHz grid: 924.0/925.0/926.0/927.0; 4 MHz: 926.0)"}
}
```

`guard_armed = stations > 0 or reservations > 0` (the spec's scope: a
reservation means a node is *expected*, even if currently powered off —
retuning would strand it on return). `compatible` for an SSID candidate
means it equals the pinned `ssid`. US frequency derivation uses the
verified closed form above; if `HALOW_COUNTRY` is not US, or
`pinned-scan.json` is missing/unparseable, every candidate reports
`compatible: false` with a reason saying why — **fail closed**, since
`confirm=1` always remains available.

**3. Enforcement — inside `set-profile` and `set`, before any mutation.**
Both commands run the same check on their candidate. If
`not compatible and guard_armed` and no `confirm=1` argument was given:
print a refusal naming the consequence, exit 4, touch nothing (no sed, no
gen, no restart):

```
REFUSED: long-range (924.5 MHz @ 1 MHz) is outside the node pinned scan set
(4 MHz: 926.0 | 1 MHz: 924.0 925.0 926.0 927.0 - /etc/halow/pinned-scan.json).
1 station associated, 2 reservations on file: applying this strands every
pinned ESP32 STA with symptoms identical to a dead radio.
Re-run with confirm=1 to apply anyway.
```

`set` gains `confirm` as an accepted-and-consumed key (it is not
persisted to env); `set-profile` gains an optional trailing `confirm=1`.
Compatible changes, and any change while unarmed, apply exactly as today
— the bench keeps full pre-association freedom.

**4. API — `GET /api/halow/compat` (new) and confirm on the two POST
endpoints.** The GET endpoint shells `halowctl check-compat` **without
sudo**: everything the check reads is readable by `halow-ui`
(pinned-scan.json 644, profiles 644, env root:halow-ui 640 per
deploy.sh:55-56 so halowctl:25 does not elevate, reservations 644, `iw`
station dump needs no privilege — the UI already runs it unprivileged at
`ui/halow_ui.py:195`). No sudoers growth for the read path.
`api_halow_profile` and `api_halow_set` switch from `sh()` f-strings to
the existing list-form `halowctl()` helper (lines 306-313) — mandatory,
not cosmetic: `sh()` swallows exit codes, so a refusal would otherwise
come back as HTTP 200 "applied" — forward the `confirm` form field, and
return 400 with the refusal text when halowctl declines.

**5. UI.** The profiles table (JS `halow()`, lines 1197-1219) gains a
compatibility badge per row ("strands pinned nodes" on the three
incompatible profiles — shown *always*, armed or not, so the
pre-association foot-gun is labeled even before enforcement can trigger).
`setProf`/`setOvr` pre-flight `GET /api/halow/compat`; on an incompatible
candidate they show a browser `confirm()` naming the frequency math and
consequence (the `cfgHalow` pattern, lines 1156-1159), then re-post with
`confirm=1`.

**Privilege model.** Enforcement lives in root-side halowctl (the audited
sudo surface — both UI endpoints and the CLI funnel through it; a CLI
user cannot bypass Flask because Flask was never the gate). The read-only
check runs unprivileged. `config/sudoers-halow-ui` is untouched: lines
5-6 (`set-profile *`, `set *`) already match the extra `confirm=1`
argument, and no new root action is introduced. Secrets: the check never
reads or emits `HALOW_PASSPHRASE`; its output contains only frequencies,
widths, counts, and the (broadcast, non-secret) SSID.

## Implementation steps

Each step is one commit; a contributor can execute them top to bottom.

1. **Add `config/pinned-scan.json`** with the exact content in Design
   step 1. Before committing, re-read the two mesh-v4 source files and
   confirm the five (freq, width) pairs still match both copies of
   `bench_get_channel_list()`.
2. **Deploy it**: in `scripts/deploy.sh` line 60, extend the install list
   to `config/halow-profiles.json config/pinned-scan.json
   config/nftables-halow.conf`.
3. **`halowctl check-compat`**: new case in the `scripts/halowctl`
   dispatch (place it after `profiles`, before `set-profile`). Implement
   as an inline `python3` heredoc (the `jqp`/`profiles` pattern, lines
   29 and 91-97): load `/etc/halow/pinned-scan.json` and
   `/etc/halow/halow-profiles.json`; parse `iw dev "${HALOW_IF:-halow0}"
   station dump 2>/dev/null | grep -c '^Station'` for the station count
   and count `dhcp-host=` lines in
   `/etc/dnsmasq.d/halow-reservations.conf` (0 if absent) for
   reservations; compute candidate effective values with `resolve()`
   precedence (profile values unless `channel`/`width` args override);
   `centre = 902.0 + 0.5 * channel` when `HALOW_COUNTRY` is unset/US,
   else fail closed; compare (freq, width) pairs with a 1 kHz tolerance;
   emit the JSON shape from Design step 2. Update the usage header
   (lines 3-16).
4. **Guard `set-profile` and `set`**: factor the decision into a helper
   `guard_or_die CANDIDATE_ARGS...` that calls the same python check and,
   on `not compatible and guard_armed and confirm != 1`, prints the
   refusal block and `exit 4`. Call it at the top of `set-profile`
   (before line 102's sed) with `profile=$n`, and at the top of `set`
   with the collected `channel=/width=/ssid=` pairs (parse the arg list
   once, accept + strip `confirm=1`, *then* guard, then sed). Fail-closed
   rule: missing/corrupt pinned-scan.json while armed refuses without
   `confirm=1`.
5. **Drift + snapshot coverage**: add
   `"config/pinned-scan.json:/etc/halow/pinned-scan.json"` to the `diff`
   pair list (halowctl lines 199-201) and `/etc/halow/pinned-scan.json`
   to the `snapshot` copy list (line 217).
6. **Mark the listing**: `halowctl profiles` (lines 89-98) appends a
   `STRANDS-PINNED` tag to profiles whose (freq, width) is outside the
   pinned set, so the CLI view carries the same warning as the UI.
7. **`GET /api/halow/compat`** in `ui/halow_ui.py`: new authed endpoint
   that runs `subprocess.run(["/usr/local/bin/halowctl", "check-compat"]
   + args, ...)` **without sudo**, timeout 15, forwarding optional
   `profile`/`channel`/`width`/`ssid` query params (validate:
   profile name `[A-Za-z0-9-]+`, channel/width numeric) and returning the
   parsed JSON; 502 with stderr text on failure.
8. **Harden the two POST endpoints**: rewrite `api_halow_profile`
   (229-234) and `api_halow_set` (254-263) onto the `halowctl()` helper;
   validate the profile name / numeric fields as in step 7 (this also
   retires the f-string shell interpolation); append `confirm=1` when the
   form carries `confirm=1`; on `ok == False` return
   `jsonify({"error": out}), 400`.
9. **UI JS**: in `halow()`, fetch `/api/halow/compat` alongside
   `/api/halow`; render the badge column in the profiles table; rewrite
   `setProf(n)` and `setOvr()` to pre-flight the candidate, show
   `confirm("<reason> - N stations / M reservations will be stranded.
   Continue?")` when incompatible, include `confirm=1` on the re-post,
   and surface the response `error`/`output` text (today setProf shows
   nothing).
10. **Bench prep + docs**: reserve the two Heltec V4.2 MACs via
    `halowctl dhcp-reserve add mac=... ip=10.117.0.x name=node1|node2`
    (MACs are printed in the node boot log, `mmhalow.c:174`, no
    association needed) so the guard is armed before the first join
    attempt; note the guard + the repin procedure ("edit
    config/pinned-scan.json, redeploy") in `docs/README.md`, and tick
    item 17 in `docs/feature-roadmap.md` when the acceptance criteria
    below pass.

## Surface changes

**API endpoints**

| method/path | change |
|---|---|
| GET /api/halow/compat | NEW — pinned-set verdicts for all profiles, active config, optional candidate; unprivileged |
| POST /api/halow/profile | CHANGED — accepts `confirm=1`; 400 + refusal text when guard declines; list-form exec, name validated |
| POST /api/halow/set | CHANGED — same; closes the existing no-confirm `ssid` hole (ui/halow_ui.py:258) |

**halowctl**

| command | change |
|---|---|
| check-compat [profile= \| channel= width= ssid=] | NEW — read-only JSON verdict, exit 0 |
| set-profile NAME [confirm=1] | CHANGED — guarded; exit 4 + refusal when armed and incompatible without confirm |
| set channel= width= ssid= [confirm=1] | CHANGED — same guard; `confirm` consumed, never persisted |
| profiles | CHANGED — `STRANDS-PINNED` tag on incompatible profiles |
| diff / snapshot | CHANGED — cover /etc/halow/pinned-scan.json |

**UI elements**: profiles table badge ("strands pinned nodes", always
visible); pre-flight confirm dialogs on profile apply and channel/width
override; command output surfaced under the profiles card.

**systemd units**: none.

**Config files**: `config/pinned-scan.json` NEW (644, no secrets),
deployed to `/etc/halow/pinned-scan.json` by deploy.sh:60.
`config/sudoers-halow-ui` deliberately UNCHANGED.

## Testing & acceptance criteria

All bounded: every check below is a single command or click; nothing
loops or waits on RF.

**Testable today (pre-association)**

1. Frequency math [M]: `python3` one-liner over
   `docs/morse-regdb-channels.csv` confirms `902.0 + 0.5*chan` reproduces
   every US `centre_freq_mhz` exactly (re-run of this session's check;
   zero mismatches required).
2. `halowctl check-compat` with no args: `balanced` compatible true;
   long-range/mid-range/max-rate false with reasons naming freq+width
   (924.5@1, 925.0@2, 924.0@8). Verdicts must come from freq+width — as a
   negative control, max-rate (US ch44) must report incompatible even
   though the node list also contains a "ch44".
3. Guard dormant when unarmed: with zero stations and zero reservations,
   `halowctl set-profile long-range` applies without confirm. Then
   **restore**: `halowctl set-profile balanced` (a control that drifts is
   the bug).
4. Guard arms on a reservation: `halowctl dhcp-reserve add
   mac=aa:bb:cc:dd:ee:01 ip=10.117.0.99 name=guardtest`, then
   `halowctl set-profile long-range` → exit 4, refusal text names 924.5
   MHz @ 1 MHz, station/reservation counts, and `confirm=1`. Verify
   nothing moved: `sudo grep ^HALOW_PROFILE /etc/halow/halow.env` still
   `balanced`, and `systemctl show -p ExecMainStartTimestamp halow-ap`
   unchanged from before the attempt (no restart fired).
5. Confirm override works: same command with `confirm=1` applies; restore
   balanced; `halowctl dhcp-reserve del mac=aa:bb:cc:dd:ee:01`.
6. SSID path: while armed, `halowctl set ssid=other` refused;
   `halowctl set ssid=mesh confirm=1` not needed — `ssid=mesh` (equal to
   pinned) passes unguarded. API: `curl -k -u user:pass -X POST -d
   ssid=other https://localhost:8443/api/halow/set` → 400 (this is the
   verified hole closing); with `-d confirm=1` → 200.
7. API round-trip: GET `/api/halow/compat` returns the JSON shape above;
   POST `/api/halow/profile` `name=long-range` → 400 while armed, 200
   with `confirm=1`.
8. Fail-closed: `sudo mv /etc/halow/pinned-scan.json{,.bak}`, guard armed
   → any profile change refused with a "pinned set unavailable" reason;
   restore the file. `halowctl diff` flags the missing/drifted file.
9. Secrets: refusal text, check-compat output, and `/api/halow/compat`
   response contain no `HALOW_PASSPHRASE` fragment —
   `halowctl check-compat | grep -ci passphrase` is 0, and the same grep
   on the captured refusal output is 0. (The PSK has leaked twice via
   "harmless" echoes; this check is not optional.)
10. UI: three profiles carry the badge; clicking one shows the dialog
    with the frequency reason; cancel posts nothing (no env change).

**Needs a joined station**

11. Guard arms from association alone [M]: with a Heltec associated and
    the test reservation removed, `check-compat` reports `stations: 1`,
    `guard_armed: true`, and `set-profile mid-range` is refused.
12. Positive control, receiver-side [M]: with a station up, apply a
    pinned-compatible change (`halowctl set channel=48 width=4
    confirm=1` if prompted — same frequency) and confirm at the receiver
    that the node stays: gateway `iw dev halow0 station dump` still lists
    the MAC and the node's own log shows no `MMWLAN_STA_DISABLED`
    transition.
13. The destructive experiment that converts this issue's rationale from
    [C] frequency math to [M], run once, deliberately, time-boxed: with
    `confirm=1`, apply `long-range`; observe at the receiver that the
    node drops and does NOT rejoin within 2x its 75 s connect timeout
    (`WIFI_CONNECT_TIMEOUT_MS`, sta-diagnostic-app_main.c:23); restore
    `balanced`; confirm rejoin at the receiver (node log
    `MMWLAN_STA_CONNECTED`, gateway station dump). Record both timestamps
    in the bench log.

**Acceptance**: items 1-10 pass pre-association; 11-13 pass after first
join; no sudoers diff; `halowctl diff` clean after deploy.

## Out of scope

- **Blocking anything.** The guard never forbids — `confirm=1` always
  applies the change. Operators retune off-set deliberately (e.g. a
  future node build that scans more channels).
- **Enforcement while unarmed.** Per spec, zero stations + zero
  reservations means no confirm requirement (UI badges still show). The
  mitigation is procedural: step 10 reserves the Heltec MACs now.
- **Root editing `/etc/halow/halow.env` directly** bypasses the guard;
  that is the accepted contract for every guarded setting in this repo.
- **Country changes / non-US frequency math** — fail-closed only; no
  multi-country pinned sets.
- **`halowctl mode ap|sta`**, passphrase changes (already gated),
  `rate` fixed-MCS knobs (module params, do not retune the channel), and
  the 2.4 GHz `wifi-config` path.
- **Automatic sync of pinned-scan.json from the mesh-v4 tree** — the file
  is updated by hand on a node repin, by design (one file, reviewed).
- **Association forensics** (issue 16) — this issue prevents one class of
  no-show; 16 records the rest.

## Risks & gotchas

- **Channel numbers lie; only frequency+width is truth.** The node's
  integer-MHz 1 MHz entries match the CSV's legacy-AU grid despite the
  file's "US table" comment, and "ch44"/"ch48" exist in both stacks at
  different (freq, width) meanings. Any future contributor "simplifying"
  the guard to channel comparison reintroduces the exact silent-strand
  this issue kills. The `_doc` block in pinned-scan.json states this;
  keep it.
- **The pinned list lives in two node files.** A repin that touches only
  one of `sta-diagnostic-app_main.c` / `halow_transport-step-c.cpp`
  desynchronizes builds; when updating pinned-scan.json, check both and
  record which build is flashed.
- **The guard is only as armed as presence data.** `iw station dump`
  shows currently-associated MACs only; a node that is powered off and
  unreserved is invisible. Reservations are the "expected node" registry
  — losing them (SD death; see issue 27) silently disarms the guard.
- **`sh()` swallows failures** (ui/halow_ui.py:60-66). If step 8 is
  skipped, halowctl refusals return HTTP 200 "applied" and the UI shows
  success while nothing happened — arguably worse than today. Steps 4 and
  8 must land together or in that order within one deploy.
- **halow-mon interplay**: the self-healer restarts `halow-ap` when
  beaconing stops; the guard sits only on the env-mutation commands, so
  heal paths and `halowctl gen` are untouched. Do not move the guard into
  `gen` — that would break recovery.
- **Sudoers wildcard behavior** is load-bearing: `set *`/`set-profile *`
  match the extra `confirm=1` argument, so no sudoers edit is needed —
  and none should be added for `check-compat`, which runs unprivileged
  ("keep this list short on purpose", config/sudoers-halow-ui:3).
- **Bench lesson driving all of it**: a stranded station is
  symptom-identical to the decoupling-capacitor fault under active
  debug (SPI deafness bursts, no association, no ARP). Every hour this
  guard exists before the capacitor lands is an hour of misdiagnosis it
  can prevent — which is why it is first in M1's build order
  (docs/issues/README.md:19).
- The destructive test (item 13) deliberately strands a station; run it
  only after 16's forensics are recording, so the failure signature gets
  captured once, on purpose, as the reference transcript for the real
  thing.
