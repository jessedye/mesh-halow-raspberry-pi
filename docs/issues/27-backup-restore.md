# 27. Encrypted off-device backup/restore of gateway identity and operator state

> Tier 3 - fleet-ops | Effort: medium | Impact: high | Depends on: none

## Problem

The gateway is a Raspberry Pi 4 running a Morse Micro MM6108 802.11ah
(HaLow) AP — SSID `mesh`, WPA3/SAE, 10.117.0.1/24 — that serves ESP32
Meshtastic nodes (repo `mesh-v4`, two Heltec V4.2 boards with the same
MM6108 chip joining as stations). Everything that makes this gateway
*this* gateway lives on one SD card: the SAE passphrase and SSID
(`/etc/halow/halow.env`), the tuning profile, DHCP reservations, port
forwards, the web console's PBKDF2 auth hashes and API token hash
(`/etc/halow/ui.conf`), the TLS key+cert whose SANs the bench tooling
knows (`/etc/halow/ui-key.pem`/`ui-cert.pem`), and the bearer tokens for
the mesh nodes' admin APIs (`/etc/halow/nodes.json`). The only snapshot
mechanism, `halowctl snapshot` (scripts/halowctl:214-221), writes to
`/var/lib/halow` — the same SD card whose failure is the disaster it
would need to survive. There is no backup command, no restore command,
and no backup API route (verified absent: the only "restore" hits in the
repo are the prebuilt-driver path in install.sh:59 and a rate-knob note
in halowctl:176).

Why it matters now: first node association is imminent (blocked only on
a decoupling capacitor + antenna confirmation), and the client side has
already committed to this AP's exact identity. The nodes store
`halow_ssid`/`halow_psk` as WRITE-ONLY settings in NVS, reported solely
as set/unset booleans (mesh-v4 docs/transport-ladder-halow.md:284-285)
— a deliberate lesson from `wifi_ssid` poisoning settings snapshots. So
if the SD card dies and the AP is recreated with a fresh passphrase,
no node can tell you what it currently holds, and every node must be
individually rewritten over its admin HTTPS API (5-6 s per request and
one of ~6 scarce TLS sessions each, per the audit for roadmap item 25)
or over unreliable LoRa admin. Restoring the AP byte-for-byte means the
fleet rejoins untouched; anything less means touching every node.

Client-side evidence that the surrounding rules are real: the WiFi PSK
has been leaked twice in mesh-v4 through "harmless" echoes
(.claude/skills/mesh-v4/reference/workflow.md:104 — the pre-commit
secret scan exists because of it), including fourteen `--info` dumps
containing the PSK found sitting in /tmp (SKILL.md ~417-421). And
mesh-v4 nearly lost work to volatile storage: the only firmware build
tree lived in another session's prunable /tmp scratchpad, with the repo
snapshot as the sole durable copy (reference/traps.md, "The repo is NOT
the build tree", ~2063-2076). Encryption of the backup is therefore
mandatory, and the off-device copy is the entire point.

## Current state

Verified this session, both repos:

- `halowctl snapshot` (scripts/halowctl:214-221): copies
  `/etc/halow/halow.env`, `halow-profiles.json`, `nftables-halow.conf`,
  `forwards.json`, `/etc/modprobe.d/morse.conf`, and
  `/etc/dnsmasq.d/halow*.conf` into a root-only (chmod 700) directory
  under `/var/lib/halow`. Same card, unencrypted, and it misses
  `ui.conf`, `nodes.json`, and the TLS key/cert entirely.
- `halowctl diff` (scripts/halowctl:196-212): compares deployed config
  against the repo; the env file is compared by KEY SET only, never
  values (halowctl:207-210), because the passphrase lives there. Any new
  machinery must keep that property.
- `deploy.sh:53-55` deliberately never clobbers a live
  `/etc/halow/halow.env` on redeploy ("Never clobber live operator
  state"), so operator edits (profile, SSID, mode, passphrase changes
  via `halowctl set-passphrase`) exist ONLY on the card. deploy.sh:58,
  by contrast, reinstalls `ui.conf` and `nodes.json` from PC-minted
  copies on every deploy — relevant to restore ordering (see Risks).
- `deploy.sh:28-41`: `ui.conf` (AUTH_SALT/AUTH_HASH/AUTH_ITER,
  SESSION_SECRET, API_TOKEN_HASH) is minted fresh on the PC per deploy;
  `nodes.json` gets the real bearer token substituted
  (config/nodes.json.example is CHANGE-ME placeholders).
- `deploy.sh:74-78`: the TLS key/cert pair is generated once, only if
  absent, with SANs for 192.168.51.202, 10.117.0.1, 10.42.0.1, and
  halow-gw.local. Losing the key breaks any fingerprint the bench has
  recorded; restoring it keeps the cert story intact.
- `install.sh:59-68` (`--prebuilt`) already gives a minutes-long driver
  restore from repo-archived binaries — the *software* stack is
  recoverable from the repo. The *identity and operator state* is not.
- `scripts/verify.sh`: PASS/FAIL post-boot checks (module present,
  overlay, probe clean, interface, morse_cli answers, AP beaconing);
  exits nonzero on any FAIL — the natural final gate for a restore.
- UI privilege split: `halow-ui` runs unprivileged; every mutation
  shells to `sudo halowctl <subcmd>` through the whitelist in
  config/sudoers-halow-ui (21 lines, one per allowed action). The
  bounded pcap pattern to copy: `halowctl capture` (halowctl:163-170,
  3-30 s, 5000 frames) + `POST /api/diag/capture` +
  `GET /api/diag/capture` streaming the file with Content-Disposition
  (ui/halow_ui.py:480-498). Secrets-on-stdin pattern to copy:
  `halowctl set-passphrase` (halowctl:244-257, "stdin, never argv: argv
  is visible in ps and shell history") driven by the UI's `halowctl()`
  helper with `input=stdin` (ui/halow_ui.py:306-313). Destructive
  changes require `confirm=1` (SSID/passphrase: ui/halow_ui.py:378-394;
  reboot: 501-507).
- halowctl's preamble (scripts/halowctl:20-27) hard-exits if
  `/etc/halow/halow.env` is missing (line 23) and sources it. A restore
  onto a fresh card runs before that file exists — the preamble needs a
  carve-out (Implementation step 1).
- mesh-v4 side: docs/transport-ladder-halow.md:270-292 documents the
  addressing plan (SSID `mesh`, 10.117.0.0/24, nodes as STAs) and the
  write-only NVS credentials at :284-285.

## Design

One new pair of `halowctl` subcommands plus a thin API/UI surface.
Encryption is gpg symmetric (AES256) — gpg ships with Pi OS; `age` is
not preinstalled and adds a dependency for no property we need. The
archive passphrase arrives on stdin (CLI) or in a POST body over TLS
(API), never argv, never logged, never echoed back.

**Backup set** (identity + operator state; generated files excluded
because `halowctl gen` reproduces them from the env — halowctl:48-85):

| file | why | restored mode/owner |
|---|---|---|
| /etc/halow/halow.env | SSID, SAE passphrase, profile, overrides, IP plan | 0640 root:halow-ui |
| /etc/halow/ui.conf | auth salt+hash, session secret, API token hash | 0640 root:halow-ui |
| /etc/halow/nodes.json | node URLs + bearer tokens | 0640 root:halow-ui |
| /etc/halow/ui-key.pem | TLS private key (cert fingerprint continuity) | 0640 root:halow-ui |
| /etc/halow/ui-cert.pem | TLS cert (SANs: .202, 10.117.0.1, 10.42.0.1, halow-gw.local) | 0640 root:halow-ui |
| /etc/halow/halow-profiles.json | tuning profiles | 0644 root:root |
| /etc/halow/nftables-halow.conf | NAT/firewall base | 0644 root:root |
| /etc/halow/forwards.json | port forwards (if present) | 0640 root:halow-ui |
| /etc/modprobe.d/morse.conf | driver options | 0644 root:root |
| /etc/dnsmasq.d/halow.conf | DHCP range/lease/dns | 0644 root:root |
| /etc/dnsmasq.d/halow-reservations.conf | MAC→IP pins (if present) | 0644 root:root |

Excluded: `hostapd_s1g.conf` / `wpa_supplicant_s1g.conf` (regenerated by
`halowctl gen`), `/var/lib/misc/dnsmasq.leases` (runtime state — the
reservations file is what keeps leases stable), metrics/history JSONL
files (measurements, not identity), chrony/avahi/udev/NM configs
(reinstalled verbatim from the repo by deploy.sh:60-65).

**Archive format**: `gateway-backup-<YYYYmmdd-HHMMSS>.tar.gpg` — a gzip
tar of the files above (stored with paths relative to `/`, e.g.
`etc/halow/halow.env`) plus `manifest.json` at the archive root,
symmetric-gpg-encrypted. The manifest never exists outside the encrypted
archive:

```json
{
  "format": 1,
  "created_at": "2026-08-05T14:12:03-07:00",
  "hostname": "halow-gw",
  "kernel": "6.12.20+rpt-rpi-v8",
  "driver_tag": "mm6108-2.0.1",
  "files": [
    {"path": "etc/halow/halow.env", "sha256": "ab12…", "mode": "0640", "owner": "root:halow-ui"},
    {"path": "etc/halow/ui-key.pem", "sha256": "cd34…", "mode": "0640", "owner": "root:halow-ui"}
  ]
}
```

**CLI contracts**:

```
halowctl backup            # passphrase on stdin (>=12 chars), or HALOW_BACKUP_PASSPHRASE env
  -> /var/lib/halow/backup/gateway-backup-<ts>.tar.gpg  (0640 root:halow-ui)
  keeps the newest 5, prunes older; refuses if the archive exceeds 1 MB
  (the set is ~tens of KB — bigger means something wrong got included);
  finishes with a test-decrypt (gpg -d | tar -t) so a mistyped
  passphrase is caught at creation, not at disaster time.

halowctl restore <tarball> [--confirm]   # passphrase on stdin/env; root; CLI-only
  1. decrypt to a mktemp -d staging dir under /run (tmpfs — plaintext
     never touches the SD), umask 077
  2. verify every manifest sha256 and that every member path is on the
     backup-set whitelist (no absolute paths, no "..") — all-or-nothing
  3. compare each destination: if any existing file differs, REFUSE
     unless --confirm, printing the differing PATHS only (never
     contents — the env holds the passphrase)
  4. install each file with manifest mode/owner via tmp+mv in the
     destination directory (atomic; the issue-22 lesson)
  5. halowctl gen; systemctl restart dnsmasq halow-ui; restart halow-ap
     (|| true — starts when halow0 appears); halowctl forwards apply
  6. run scripts/verify.sh if present and print its PASS/FAIL table
  Idempotent: when every file already matches, prints "no changes" and
  exits 0 without touching services.
```

**API contract** (mirrors the capture POST/GET pair):

```
POST /api/system/backup    form: passphrase=<archive passphrase>
  -> 200 {"ok": true, "file": "gateway-backup-20260805-141203.tar.gpg",
          "bytes": 24576, "sha256": "9f2c…", "download": "/api/system/backup"}
  -> 400 {"error": "passphrase must be at least 12 chars"}

GET /api/system/backup
  -> streams the newest .tar.gpg, Content-Disposition: attachment
  -> 404 {"error": "no backup yet"}
```

The response carries the archive's sha256 so the bench PC can confirm
the download at the receiver. The passphrase field is never included in
any response or log line.

**Privilege model**: `halowctl backup` runs as root (sudoers gets one
new argument-free line — nothing to abuse via wildcards) and chgrps the
output to halow-ui 0640, exactly the forwards.json pattern
(halowctl:337). `halowctl restore` is deliberately NOT in sudoers: the
UI can never trigger it, remote restore of a live gateway is a footgun,
and the machine that needs restoring typically has no working UI anyway.
Restore is a console/SSH action by a root-capable operator.

**Off-device schedule**: lives on the bench PC, not the gateway — a
gateway cannot be the keeper of its own off-device copies. The archive
passphrase is a new `HALOW_BACKUP_PASSPHRASE` entry in the repo-root
`secrets.env` (already gitignored; deploy.sh:14 requires it to exist).
Documented cron example in the restore drill doc:

```bash
# bench PC, weekly — create then pull, confirm at the receiver
source secrets.env   # HALOW_BACKUP_PASSPHRASE, ADMIN_USER, ADMIN_PASS
SHA=$(curl -sk -u "$ADMIN_USER:$ADMIN_PASS" -X POST \
  -F "passphrase=$HALOW_BACKUP_PASSPHRASE" \
  https://halow-gw.local:8443/api/system/backup | python3 -c 'import json,sys;print(json.load(sys.stdin)["sha256"])')
curl -sk -u "$ADMIN_USER:$ADMIN_PASS" -o "backups/gateway-$(date +%F).tar.gpg" \
  https://halow-gw.local:8443/api/system/backup
sha256sum "backups/gateway-$(date +%F).tar.gpg"   # must equal $SHA
```

## Implementation steps

1. **halowctl preamble carve-out** — in scripts/halowctl, wrap the env
   guard/sourcing (lines 20-27) so `restore` skips it: restore must run
   on a card where `/etc/halow/halow.env` does not exist yet (line 23
   currently hard-exits). Keep the sudo-climb for every subcommand.
   Verify all existing subcommands still resolve `$ENV` as before.
2. **`halowctl backup` subcommand** — read passphrase
   (`HALOW_BACKUP_PASSPHRASE` env, else stdin `head -c 256` like
   set-passphrase at halowctl:246), enforce >=12 chars. Build
   `manifest.json` with a python3 heredoc (hostname, `uname -r`, driver
   tag from docs/software-stack.md pin, per-file sha256/mode/owner) in
   a `mktemp -d` under /run. Stream
   `tar -C / -cz manifest + files | gpg --batch --pinentry-mode loopback
   --passphrase-fd 3 --symmetric --cipher-algo AES256 -o "$OUT"` with
   the passphrase on fd 3 from a pipe (process substitution — never a
   herestring, herestrings can hit /tmp; never argv). Output to
   `/var/lib/halow/backup/` (mkdir -p, 0750 root:halow-ui), chmod 640
   chgrp halow-ui. Size cap 1 MB, retention: delete all but newest 5.
   Finish with the test-decrypt (`gpg -d | tar -t`, compare member
   count against manifest) and print file, bytes, sha256 — never file
   contents.
3. **`halowctl restore` subcommand** — as specified in Design: staging
   under /run, manifest sha256 + path-whitelist validation before any
   install, differing-paths confirm gate (`--confirm`), tmp+mv installs
   with manifest mode/owner, then `gen`, service restarts,
   `forwards apply`, and verify.sh. Second run on identical state must
   print "no changes" and exit 0.
4. **sudoers** — append to config/sudoers-halow-ui:
   `halow-ui ALL=(root) NOPASSWD: /usr/local/bin/halowctl backup`
   (no wildcard, no restore line). deploy.sh:71 already reinstalls this
   file each deploy.
5. **UI routes** — in ui/halow_ui.py add `POST /api/system/backup`
   (validate passphrase length, call
   `halowctl(["backup"], stdin=passphrase, timeout=60)`, parse the
   file/bytes/sha256 line into JSON) and `GET /api/system/backup`
   (newest file in /var/lib/halow/backup/, stream like the pcap
   download at :489-498, mimetype application/octet-stream). Both under
   `@authed`.
6. **UI element** — Config tab System card (next to the reboot button,
   PAGE js ~line 1143): a write-only passphrase input + "create backup"
   button posting to the new route, and a "download latest backup" link
   styled like the pcap download link (~line 1057).
7. **Restore drill doc** — `docs/restore-drill.md`: the end-to-end
   sequence (flash Pi OS → `install.sh` or `install.sh --prebuilt` →
   `deploy.sh` from the PC → copy tarball → `sudo halowctl restore
   <tarball> --confirm` → reboot → `verify.sh`), the bench-PC cron
   example above, where the archive passphrase lives, and the rule that
   identity changes (set-passphrase, SSID, reservations) should be
   followed by a fresh backup. State plainly that a backup whose
   passphrase is lost is a brick.
8. **Roadmap close-out** — mark feature-roadmap.md item 27 (lines
   215-222) DONE with the measured evidence from the acceptance drill.

## Surface changes

| API endpoint | method | new/changed | notes |
|---|---|---|---|
| /api/system/backup | POST | new | form `passphrase`; returns file/bytes/sha256; never echoes the passphrase |
| /api/system/backup | GET | new | streams newest .tar.gpg; 404 if none |

| halowctl command | new/changed | notes |
|---|---|---|
| `halowctl backup` | new | passphrase stdin/env; bounded (1 MB cap, keep 5); test-decrypts before reporting success |
| `halowctl restore <tar> [--confirm]` | new | root, CLI-only; idempotent; refuses on differing live state without --confirm |
| preamble (lines 20-27) | changed | env guard skipped for `restore` |

| UI element | location | notes |
|---|---|---|
| create backup + download latest | Config tab, System card | write-only passphrase field; mirrors pcap download link |

| systemd unit | change |
|---|---|
| (none) | deliberate — the schedule lives on the bench PC; no gateway timer |

| config file | change |
|---|---|
| config/sudoers-halow-ui | +1 line: `halowctl backup` (no restore) |
| secrets.env (PC, gitignored) | new key `HALOW_BACKUP_PASSPHRASE` |
| /var/lib/halow/backup/ | new dir, 0750 root:halow-ui |
| docs/restore-drill.md | new |

## Testing & acceptance criteria

All checks are [M] measured, receiver-side, bounded. Nothing here
depends on RF, so the whole core is testable before first association.

**Testable today (pre-association):**

1. `halowctl backup` with a 12+ char passphrase produces
   `/var/lib/halow/backup/gateway-backup-*.tar.gpg`, 0640 root:halow-ui;
   `gpg -d | tar -t` with the same passphrase lists exactly the backup
   set + manifest.json; a wrong passphrase fails decryption. [M]
2. Secret scan (the mesh-v4 discipline, workflow.md:104-115): after a
   backup + API round trip, `grep -F` for the SAE passphrase and the
   archive passphrase across `journalctl -u halow-ui`, the POST/GET
   JSON responses, and `strings` of the encrypted tarball — zero hits.
   [M]
3. Passphrase under 12 chars: CLI exits nonzero, API returns 400.
   Retention: run 7 backups, exactly 5 remain, newest kept. [M]
4. Confirm gate: change the live SSID (`halowctl set ssid=meshX`), then
   `restore` an older tarball WITHOUT `--confirm` → refuses, exit
   nonzero, output lists differing paths only (no values); WITH
   `--confirm` → proceeds and `halowctl status` shows the archived
   SSID again. [M]
5. Idempotency: run the same restore twice; second run reports no
   changes, exit 0, and service restart counters
   (`systemctl show -p NRestarts halow-ap dnsmasq`) do not increment on
   the second pass. [M]
6. Fresh-card drill on a spare SD: flash → `install.sh --prebuilt` (or
   full) → `deploy.sh` → `sudo halowctl restore <tarball> --confirm` →
   reboot → `verify.sh` exits 0. Then byte-for-byte identity:
   `sha256sum` of every file in the backup set on the restored card
   equals the manifest hash — in particular halow.env (SSID + SAE
   passphrase exact) and ui-key.pem. TLS continuity:
   `openssl s_client -connect <ip>:8443` fingerprint matches the
   pre-failure fingerprint recorded on the bench PC. AP identity:
   `iw dev halow0 info` shows ssid `mesh`. [M]
7. API round trip from the bench PC: POST creates, GET downloads,
   downloaded file's sha256 equals the POST response's sha256 (confirm
   at the receiver), and the file decrypts on the PC with the passphrase
   from secrets.env. [M]

**Needs a joined station:**

8. The real acceptance: with a node holding `halow_ssid`/`halow_psk`
   write-only in NVS (untouched throughout), the node associates to the
   RESTORED AP — `iw dev halow0 station dump` shows its MAC, and it
   receives the same 10.117.0.x address its restored reservation pins.
   Zero node-side reconfiguration is the pass condition; one touched
   node is a fail. [M]
9. Lease stability across the drill: the node's IP before SD swap and
   after restore are identical (reservations file round-tripped). [M]

## Out of scope

- Automatic scheduled backups running ON the gateway — the off-device
  copy is the point; the bench PC pulls (cron example in the drill doc).
- Restore over the API or UI, in any form.
- Full-SD image backup. The software stack is already recoverable from
  the repo (install.sh --prebuilt, install.sh:59-68; vendored tarballs);
  this issue covers identity and operator state only.
- Backing up measurement history (metrics.jsonl, stations.jsonl,
  throughput.jsonl, station-events.log, capture.pcap) — measurements
  are evidence of the past, not identity.
- age encryption, multi-recipient keys, key rotation — gpg symmetric
  with one passphrase in the PC's gitignored secrets store matches the
  bench's existing secret-handling model.
- Node-side NVS backup — that is mesh-v4's concern.
- Off-site (beyond bench PC) replication.

## Risks & gotchas

- **Restore ordering vs deploy.sh**: deploy.sh:58 reinstalls `ui.conf`
  and `nodes.json` from PC-minted copies on EVERY deploy (unlike
  halow.env, guarded at :53-55). Restore-then-deploy silently reverts
  those two to PC values — equivalent content only if the PC secrets
  are unchanged. The drill doc therefore fixes the order:
  deploy first, restore second. TLS material is safe either way
  (deploy.sh:74 only generates a cert when absent).
- **Plaintext must never touch the SD**: staging in /run (tmpfs) and
  streaming tar|gpg. No herestrings for the passphrase (bash may back
  them with temp files); fd-3 pipe or stdin only. The leak history
  (workflow.md:104; fourteen /tmp dumps, SKILL.md ~417) says the
  "harmless" path is exactly where the PSK escapes.
- **A lost archive passphrase is a bricked backup.** The mandatory
  test-decrypt at creation catches typos at backup time; the drill doc
  names the single storage location (PC secrets.env). Do not "help" by
  logging or echoing it anywhere — that is the leak vector again.
- **Stale backups drift from live state**: reservations or forwards
  added after the last backup vanish on restore. The confirm-gate's
  differing-paths list is the tell; the drill doc's rule is backup
  after every identity change. Roadmap item 22's atomic-write work and
  logrotate do not touch the backup set — no interaction.
- **Tar extraction is an attack surface**: the tarball may come back
  from a PC. Validate member paths against the whitelist and reject
  absolute paths/`..` before installing anything; extract only into
  staging, install file-by-file.
- **gpg on Pi OS**: use `--batch --pinentry-mode loopback` so gpg can
  never prompt a TTY inside the UI's subprocess call (the UI helper at
  ui/halow_ui.py:306-313 has a 40 s default timeout; pass 60 s).
- **Fresh-card clock**: a just-flashed Pi without NTP sync may carry a
  bogus date. Trust the manifest's `created_at` and hashes, never file
  mtimes, when deciding what differs.
- **Probe honesty** (mesh-v4 traps generalisation: "when a probe reports
  that something does not exist, test the probe against a case where it
  does"): test the confirm-gate on a KNOWN-different file before
  trusting a "no changes" no-op, and test the secret scan by planting a
  dummy string once.
- **Coordination with items 25/26**: nodes.json is carried verbatim;
  when item 25 adds a `mac` field the backup needs no change — format 1
  archives restore whatever schema was current at backup time.
- **Sudoers discipline**: the backup line is argument-free on purpose;
  adding a restore line later would hand the unprivileged UI a
  root-level file-write primitive fed by an uploaded archive. Keep
  restore off the whitelist.
