# 29. Trail-cam image ingest sink: bounded, sha256-verified, browsable terminal endpoint

> Tier 4 - product-path | Effort: medium | Impact: high | Depends on: none (full value gated on the node TX datapath; coordinates with #22 on disk low-water)

## Problem

The trail cam is one of the two product use cases this gateway exists to serve,
and the LoRa numbers are final: a 160x120 q35 JPEG (~1.4 KB) moves in ~17 s,
so **a frame every 20-30 seconds is the measured ceiling**, and "every couple
of seconds" needs ~10x that no modem preset delivers — the per-packet floor is
~2.4 s regardless of settings, so SHORT_TURBO is *slower* end-to-end than
LONG_FAST despite 21x the on-air rate (mesh-v4 `HANDOVER.md:163-168`; sustained
link ~620 bps, `HANDOVER.md:146`). HaLow is the escape hatch: even the 1 MHz
long-range profile is three orders of magnitude more link than LoRa.

But nothing exists for a frame to land on. Grep of this repo's `ui/`, `scripts/`
and `docs/` for image/upload/ingest machinery finds exactly one hit: the roadmap
entry itself (`docs/feature-roadmap.md:235-241`). The node side is equally
greenfield: mesh-v4's HaLow work is an SPI transport layer
(`firmware/halow/README.md:7-12`), every image-related hit in the tree is inside
the vendored Meshtastic firmware snapshot (graphics/screen code), and RadioLib's
SSTV support is explicitly compiled out (`firmware/idf/platformio-idf.ini:70`,
`-DRADIOLIB_EXCLUDE_SSTV=1`). The whole camera-to-operator pipeline is
unbuilt, and the gateway at 10.117.0.1 is its natural terminal destination.

Why now: first association is imminent. Node2's pinned scan has already decoded
this AP's beacons over the air (`AP 'mesh' rssi -8, 4 MHz BW, SAE` —
`firmware/halow/README.md:46`, the first HaLow RF reception on the bench), and
association waits only on a bulk decoupling capacitor plus antenna confirmation
(`firmware/halow/README.md:205-212, 308`). The moment TX works, the node team
needs a wire contract to code the camera POST against. Publishing that contract
is most of this issue's value today; the sink itself is LAN-testable with curl
before any station joins.

The bounding requirements are not optional decoration. This gateway's culture
caps every operation (packet capture: 3-30 s and 5000 frames,
`scripts/halowctl:163-170`), and a camera is precisely the client that will
misbehave: a runaway loop POSTing frames must not fill the SD card that also
holds the AP's config, metrics ring, and self-healer state. Verifier
requirements folded in below: max body size in single-digit MB, per-source rate
limit, disk low-water integration with #22.

## Current state

Verified in this session, both repos.

**Gateway — no ingest surface, but every needed pattern exists:**

- No image/upload/ingest code anywhere: grep across `ui/`, `scripts/`, `docs/`
  hits only the roadmap entry `docs/feature-roadmap.md:235-241`. No
  `/var/lib/halow/images`, no endpoint, no CLI verb.
- Auth already serves both consumers the contract needs: `ui/halow_ui.py:116-137`
  (`check_auth`) accepts HTTP Basic *and* `Bearer` tokens — the Bearer token is
  the mesh-v4 ADMIN_TOKEN, stored only as its sha256 (`API_TOKEN_HASH` in
  `/etc/halow/ui.conf`). The `@authed` decorator (`ui/halow_ui.py:140-150`)
  returns JSON 401 for `/api/` paths. A node can POST with the same header it
  already uses for its own admin API.
- The UI process can already write the destination unprivileged:
  `/var/lib/halow` is chowned `halow-ui:halow-ui` at `scripts/deploy.sh:70`, and
  `halow_ui.py` appends `/var/lib/halow/throughput.jsonl` as that user today
  (`ui/halow_ui.py:544-545`). `halow-ui.service` runs `User=halow-ui` with
  `ProtectSystem=true` (`systemd/halow-ui.service:10,15`), which leaves `/var`
  writable. **No sudoers change, no new unit, no root.**
- The bounded-artifact pattern to copy: capture writes one capped pcap to
  `/var/lib/halow/capture.pcap` (`scripts/halowctl:163-170`) and the UI serves
  it back through an authed GET (`ui/halow_ui.py:489-498`).
- Destructive/identity actions gate on `confirm=1`: SSID/passphrase
  (`ui/halow_ui.py:373-394`), reboot (`ui/halow_ui.py:501-507`). A ring purge
  must do the same.
- There is **no request body cap anywhere** in `halow_ui.py` — no
  `MAX_CONTENT_LENGTH`, no `content_length` check (grep verified). Every
  existing POST takes small form fields, so this was never load-bearing; a raw
  binary endpoint must bring its own bound.
- Config hygiene constraint: `halowctl diff` compares `/etc/halow/halow.env`
  against `config/halow.env.example` **by key set only** — never values,
  because the SAE passphrase lives in that file (`scripts/halowctl:207-210`).
  New env keys must land in the example file or every diff flags drift.

**mesh-v4 — the discipline and the gap:**

- sha256 end-to-end is the established transfer discipline: the LoRa image
  tool hashes at the sender (`tools/meshdata.py:315`), verifies the reassembled
  bytes at the receiver and prints `COMPLETE`/`CORRUPT` accordingly
  (`tools/meshdata.py:421-432`), and keys its resume state on the digest
  (`tools/meshdata.py:137-141`). The gateway sink must speak the same language:
  hash verified on receipt, mismatch rejected loudly.
- Receiver-side counting is doctrine: a sender once reported acked=1000/failed=0
  while 424/1000 arrived (roadmap item 19). The sink is the receiver — loss
  accounting belongs here, from sequence numbers.
- The node firmware has no camera, no image capture, no bulk HTTP client. This
  issue does not build those; it publishes the exact contract they will target.

One spec correction, following the code: the audit brief said the only node-side
image hits were "vendored RadioLib SSTV examples". In the tree as it stands,
SSTV is *excluded from the build* (`platformio-idf.ini:70`) and the image hits
are the vendored Meshtastic display code. Same conclusion — greenfield — but
the evidence is the exclusion flag, not example sketches.

## Design

Terminal sink. Frames stop here for the operator to view or pull. **Rejection
boundary, stated plainly: no onward queueing, no relay API, no push to
anywhere.** Store-and-forward was rejected in roadmap v1 ("a router forwards;
queuing is the nodes' delay-tolerant job") and stays rejected. A camera POSTs;
the operator GETs; nothing else moves.

Everything lives in `ui/halow_ui.py` running as the unprivileged `halow-ui`
user. No new systemd unit, no sudoers entry, no root path — the storage
directory is already owned by the service user (`deploy.sh:70`).

### Upload contract (this section is the node-side spec)

```
POST https://10.117.0.1:8443/api/ingest/image
Authorization: Bearer <ADMIN_TOKEN>          (or HTTP Basic; ui/halow_ui.py:116-137)
Content-Type: image/jpeg
X-SHA256: <64 lowercase hex chars — sha256 of the exact body bytes>   REQUIRED
X-Node-Id: node2                             optional; [A-Za-z0-9-]{1,32}; default "unknown"
X-Sequence: 1417                             optional; integer 0..4294967295
X-Timestamp: 1754407200                      optional; sender's unix seconds, stored as a claim
Body: raw JPEG bytes (no multipart, no base64)
```

Metadata falls back to query parameters of the same lowercase names
(`?node=node2&seq=1417&ts=...`) for clients that cannot set headers; headers
win when both are present. TLS is the gateway's self-signed cert (SAN covers
10.117.0.1 — audit A3); nodes skip verification the same way the gateway
already does toward them (`ui/halow_ui.py:874`).

Response codes — each condition distinct, so the node can classify failures:

| Code | Meaning | Body |
|---|---|---|
| 200 | stored (or exact duplicate — idempotent retry) | `{"ok":true,...}` below |
| 400 | missing/malformed `X-SHA256`, bad node-id/seq/ts, empty body | `{"error":"..."}` |
| 401 | auth missing/wrong (throttled per source like all auth) | `{"error":"auth required"}` |
| 413 | body exceeds `INGEST_MAX_BYTES` | `{"error":"body exceeds cap","cap_bytes":4194304}` |
| 415 | body does not begin `FF D8` (JPEG SOI) | `{"error":"not a JPEG"}` |
| 422 | **sha256 mismatch** — link corrupted the frame | `{"error":"sha256 mismatch","header_sha256":"...","received_sha256":"...","bytes":N}` |
| 429 | per-source rate cap exceeded | `{"error":"rate limited","retry_after_s":N}` |
| 507 | disk below free floor — ring intact, ingest paused | `{"error":"disk below floor","free_mb":N,"floor_mb":512}` |

422 vs 400 matters: 400 means the sender built the request wrong (fix code);
422 means the bytes changed in flight (retry is correct). Nothing is stored on
any non-200. Success body:

```json
{"ok": true,
 "id": "20260805T191203Z-node2-001417-9f3c1a2b.jpg",
 "bytes": 1453,
 "sha256": "9f3c1a2b...64 hex",
 "duplicate": false,
 "evicted": 0,
 "ring": {"count": 212, "bytes": 14818304, "cap_count": 2000, "cap_mb": 256, "free_mb": 11834}}
```

`duplicate: true` (same node+seq+digest already stored) returns 200 without a
second write — a node retrying after a lost response is idempotent.

### Storage: bounded on-disk ring

- Directory: `/var/lib/halow/images/` (created by the UI at startup with
  `os.makedirs(..., exist_ok=True)`; parent already `halow-ui`-owned).
- Filename is the id and carries all metadata, so the directory alone can
  rebuild the index after a bad shutdown:
  `<UTC %Y%m%dT%H%M%SZ received>-<node>-<seq zero-padded 6>-<sha256 first 8>.jpg`
  (when the sender omits `X-Sequence`, the filename field is `000000` and the
  index records `seq: null`). Validation regex for the download path:
  `^[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9-]{1,32}-[0-9]{6}-[0-9a-f]{8}\.jpg$`
  — regex-then-join is the traversal guard; client text never touches a path
  unvalidated.
- Index: `/var/lib/halow/images/index.jsonl`, one record per stored frame:

```json
{"id": "20260805T191203Z-node2-001417-9f3c1a2b.jpg",
 "node": "node2", "seq": 1417, "bytes": 1453,
 "sha256": "9f3c1a2b...",
 "received_at": 1754407923,
 "claimed_ts": 1754407200}
```

  `received_at` is the measured fact (gateway clock, chrony-served net, roadmap
  12/21); `claimed_ts` is the sender's assertion and is stored as exactly that —
  the [M]/[C] split baked into field names. Index rewrites (eviction, purge) go
  through tmp + `os.replace` — the #22 atomic-write convention, adopted here
  from day one rather than retrofitted.
- Ring caps, both enforced on every accept, oldest-by-`received_at` evicted
  until both hold: `INGEST_RING_COUNT` (default 2000 frames) and
  `INGEST_RING_MB` (default 256 MB). The `capture.pcap` philosophy at gallery
  scale: a runaway camera converges to a bounded window, never a full card.
- Disk floor: before accepting, `os.statvfs` on the images dir; free space
  below `INGEST_MIN_FREE_MB` (default 512) → 507 and nothing written. This is
  the ingest-side half of #22's disk low-water; when #22 lands its
  `/api/system` field, both read the same number.
- A `threading.Lock` serializes accept/evict/purge — the app runs
  `threaded=True` (`ui/halow_ui.py:1273`) and two concurrent POSTs must not
  interleave an index rewrite.
- Startup reconciliation: if index and directory disagree (brownout
  mid-eviction), rebuild the index from filenames + `stat` — this is why the
  filename carries the metadata.

### Body bound — belt and braces

Fast path: reject `request.content_length > INGEST_MAX_BYTES` (default
4 MiB — verifier: single-digit MB) with 413 before reading. Real guard: read
`request.stream` in chunks to at most cap+1 bytes and 413 if it overflows —
never trust a client-supplied Content-Length, and never buffer an unbounded
body (there is currently no cap anywhere in the app; this endpoint brings its
own rather than imposing a global `MAX_CONTENT_LENGTH` on unrelated routes).

### Rate limit per source

Sliding 60 s window per `request.remote_addr`, in-process (the `_FAILS` auth
throttle pattern, `ui/halow_ui.py:96-113`): more than `INGEST_RATE_PER_MIN`
(default 60) accepts-or-attempts in the window → 429 with `retry_after_s`.
Default reasoning: the product bar is "a frame every couple of seconds"
(`HANDOVER.md:167`) = 30/min; 60/min gives the target cadence 2x headroom while
still strangling a tight loop. Restart clears the window — same accepted
tradeoff as the auth throttle.

### Read side

- `GET /api/ingest/images?n=50&node=node2` — newest-first index slice
  (`n` capped at 500) plus ring stats and per-node receiver-side accounting:

```json
{"images": [ {...index records...} ],
 "ring": {"count": 212, "bytes": 14818304, "cap_count": 2000, "cap_mb": 256, "free_mb": 11834},
 "nodes": {"node2": {"frames": 210, "last_seq": 1417, "seq_gaps": 3}}}
```

  `seq_gaps` counts missing sequence numbers across the retained window —
  measured, receiver-side loss, the number the sender cannot lie about
  (mesh-v4 lesson: sender reported 1000/1000 while 424 arrived).
- `GET /api/ingest/images/<id>` — the JPEG, authed like the pcap download
  (`ui/halow_ui.py:489-498`), `Content-Type: image/jpeg`,
  `Cache-Control: private, max-age=31536000, immutable` (ids embed the digest;
  the 15 s UI auto-refresh must not re-transfer every frame).
- `POST /api/ingest/purge` — empties the ring; destructive, so `confirm=1`
  required (the `ui/halow_ui.py:373-394` pattern) else 400.
- **Camera tab** in the console: ring stats header, per-node seq/gap summary,
  newest 24 frames as an `<img>` grid with node/seq/time/bytes captions, purge
  button with the usual confirm dialog. No thumbnailing — trail-cam frames are
  small by construction and the browser scales.
- `halowctl images` — read-only CLI summary (count, bytes, newest id, per-node
  frames) for SSH debugging. Reads the world-readable ring; not in sudoers, not
  called by the UI.

### Privilege model — explicit

| Piece | Runs as | Why |
|---|---|---|
| POST/GET/purge endpoints, ring, eviction | `halow-ui` (existing service) | `/var/lib/halow` already `halow-ui:halow-ui` (`deploy.sh:70`) |
| `halowctl images` | invoking user | read-only `ls`/`stat` over 0644 files |
| sudoers changes | **none** | no root action exists in this feature |
| new systemd units | **none** | lives inside `halow-ui.service` |

Secrets: nothing in this feature touches the PSK or tokens, and nothing may
echo them. Responses contain only ingest fields; the ingest code never logs
request headers (the Authorization header transits every upload). New env keys
are non-secret tunables, added to `config/halow.env.example` so the
key-set-only diff (`scripts/halowctl:207-210`) stays clean.

## Implementation steps

Each step is one commit, in order.

1. **Config keys.** Add to `config/halow.env.example` (and document defaults in
   a comment): `INGEST_MAX_BYTES=4194304`, `INGEST_RING_COUNT=2000`,
   `INGEST_RING_MB=256`, `INGEST_MIN_FREE_MB=512`, `INGEST_RATE_PER_MIN=60`.
   Values read at request time via the existing `load_kv(ENV_CONF)`
   (`ui/halow_ui.py:69-80`) with these defaults when absent — no restart needed
   to tune, consistent with how `HALOW_IF` is read.

2. **Ring store.** In `ui/halow_ui.py`, add module constants
   (`IMAGES_DIR = "/var/lib/halow/images"`, `IMAGES_INDEX`, `IMAGE_ID_RE`) and
   functions: `_ingest_conf()` (env keys + defaults), `_ring_load()` /
   `_ring_rebuild()` (index read; reconcile against directory on mismatch),
   `_ring_stats()`, `_ring_accept(data, sha, node, seq, claimed_ts)` (write
   file, append index, evict oldest until both caps hold, tmp+`os.replace` for
   every index rewrite) — all under a module-level `threading.Lock`. Create
   `IMAGES_DIR` at import time with `os.makedirs(exist_ok=True)`. No routes yet.

3. **`POST /api/ingest/image`.** The full ladder in order: auth (`@authed`) →
   rate window (429) → header/query metadata validation (400) →
   `content_length` fast reject (413) → bounded stream read cap+1 (413) →
   empty body (400) → JPEG SOI magic (415) → sha256 compare against `X-SHA256`
   (422 with both digests) → disk floor via `statvfs` (507) → duplicate check
   (200 `duplicate:true`) → `_ring_accept` (200). Response shapes exactly as in
   Design.

4. **Read + purge routes.** `GET /api/ingest/images` (list, `n` cap 500,
   optional `node` filter, ring stats, per-node `frames`/`last_seq`/`seq_gaps`),
   `GET /api/ingest/images/<id>` (regex-validate id, serve bytes with
   immutable cache headers, 404 JSON when absent — the pcap-download pattern),
   `POST /api/ingest/purge` (`confirm=1` or 400).

5. **Camera tab.** Add `"Camera"` to `TABS` (`ui/halow_ui.py:1015`) and a
   `camera()` renderer beside `diag()`/`debug()`: stats header from the list
   endpoint, per-node table (frames / last_seq / seq_gaps), 24-image grid
   (`<img src="/api/ingest/images/<id>" loading="lazy">` with caption), purge
   button wired through the existing `post()` helper with a `confirm()` dialog.

6. **`halowctl images`.** New subcommand in `scripts/halowctl` (beside
   `capture`): print count, total bytes, newest id, per-node frame counts from
   `/var/lib/halow/images/`. Read-only; update the usage header (lines 2-17).
   No sudoers change — assert that in the commit message.

7. **Test JPEG + curl runbook.** Commit the pre-association acceptance script
   as `scripts/verify-ingest.sh` (or fold into `scripts/verify.sh` if review
   prefers): generates a tiny valid JPEG, runs the full response-code ladder
   from the Testing section against a target host, exits nonzero on any
   mismatch. This doubles as the node team's living reference client.

8. **Docs.** Mark roadmap item 29's machinery DONE with date and one-line
   status (`docs/feature-roadmap.md:235-241`, the established convention:
   "machinery DONE, `[M]` numbers await the first station"). Point mesh-v4 at
   this file's Upload contract section as the wire spec.

## Surface changes

**API endpoints (all authed, Basic or Bearer):**

| Method | Path | New/changed | Purpose |
|---|---|---|---|
| POST | `/api/ingest/image` | new | raw JPEG upload; sha256-verified; codes 200/400/401/413/415/422/429/507 |
| GET | `/api/ingest/images` | new | newest-first list + ring stats + per-node seq accounting |
| GET | `/api/ingest/images/<id>` | new | download one frame (immutable-cached) |
| POST | `/api/ingest/purge` | new | empty the ring; requires `confirm=1` |

**halowctl:**

| Command | New/changed | Purpose |
|---|---|---|
| `halowctl images` | new | read-only ring summary over SSH; not in sudoers |

**UI:**

| Element | Change |
|---|---|
| `TABS` | + `"Camera"` |
| Camera tab | ring stats, per-node seq/gap table, 24-frame gallery, purge (confirm) |

**systemd units:** none added, none changed.

**Config files:**

| File | Change |
|---|---|
| `config/halow.env.example` | + 5 `INGEST_*` keys (non-secret tunables; keeps `halowctl diff` key-set clean) |
| `config/sudoers-halow-ui` | **unchanged** — feature needs no root |

**On-disk:**

| Path | Purpose |
|---|---|
| `/var/lib/halow/images/*.jpg` | frame ring, both-caps bounded, oldest evicted |
| `/var/lib/halow/images/index.jsonl` | metadata index, atomic rewrites, rebuildable from filenames |

## Testing & acceptance criteria

Bench rules apply: every pass condition is measured at the receiver, every
operation bounded. `H=https://192.168.51.202:8443` (LAN address; SAN also
covers 10.117.0.1 and halow-gw.local), `-k` for the self-signed cert, auth via
`-u user:pass` or `-H "Authorization: Bearer ..."`.

### Testable today (pre-association, curl over LAN)

1. **Happy path, verified at the receiver.**
   `sha=$(sha256sum test.jpg | cut -d' ' -f1)`;
   `curl -k -u u:p --data-binary @test.jpg -H "Content-Type: image/jpeg" -H "X-SHA256: $sha" -H "X-Node-Id: bench" -H "X-Sequence: 1" $H/api/ingest/image`
   → 200, `ok:true`. Then on the Pi: `sha256sum /var/lib/halow/images/<id>`
   equals `$sha` **[M]** — receipt is confirmed by re-hashing stored bytes, not
   by trusting the 200.
2. **Corruption rejected distinctly.** Flip one byte
   (`printf '\x00' | dd of=bad.jpg bs=1 seek=100 conv=notrunc`), send with the
   original digest → 422, body carries both digests, ring count unchanged.
3. **Contract errors are 400.** Missing `X-SHA256`; 63-char digest; digest with
   `G` in it; `X-Node-Id` of `../evil`; empty body → all 400, nothing stored.
4. **Auth.** No credentials → 401 JSON. Repeated bad Bearer values trip the
   existing per-IP throttle.
5. **Body cap.** `dd if=/dev/urandom bs=1M count=5` (prefix `FF D8`) → 413;
   also 413 when Content-Length lies low (chunked/absent) — the bounded read
   catches it. Ring untouched.
6. **Not a JPEG.** Text body with a correct sha → 415.
7. **Rate cap.** 70 rapid POSTs from one host → first 60 processed, then 429
   with `retry_after_s`; counted at the receiver from ring count + responses.
8. **Eviction, both caps.** Set `INGEST_RING_COUNT=5` in `/etc/halow/halow.env`,
   upload 8 distinct frames → ring count is exactly 5, the 3 oldest ids are
   gone from disk *and* index, `evicted` fields in responses sum to 3.
   Repeat with `INGEST_RING_MB` set below the total. Restore both keys after —
   a control that drifts is the bug.
9. **Disk floor.** Set `INGEST_MIN_FREE_MB=10000000` → 507 with real `free_mb`;
   existing frames still listable/downloadable (paused, not purged). Restore.
10. **Idempotent retry.** Re-send test 1's exact request → 200 `duplicate:true`,
    ring count unchanged.
11. **List/download/gallery.** List shows newest-first with correct per-node
    `last_seq`; upload seq 1,2,4,7 → `seq_gaps` is 2 (receiver-side count).
    Download round-trips byte-identical (`cmp`). Camera tab renders the grid.
12. **Purge gate.** Purge without `confirm` → 400 and nothing deleted; with
    `confirm=1` → empty ring, empty index.
13. **Concurrency.** 10 parallel curl uploads → all 200, index line count ==
    directory count, every line valid JSON (the lock held).
14. **Index recovery.** Delete `index.jsonl`, restart `halow-ui` → index
    rebuilt from filenames; list matches directory exactly.
15. **No secret leakage.** `journalctl -u halow-ui` during all of the above
    contains no Authorization header, no env values; grep responses for the
    same.

### Needs a joined station

16. **First frame over HaLow [M].** Node at 10.117.0.x POSTs a real frame over
    `halow0` → 200; stored sha matches node-side sha. This is the pipeline's
    first end-to-end measurement; record it the way first-contact was recorded.
17. **Cadence vs the LoRa ceiling [M].** Same-size frame as the LoRa benchmark
    (1453 B, 17 s over LoRa — `HANDOVER.md:150`): measure POST wall time over
    HaLow at each profile. Then sustained: one frame every 2 s for 5 minutes;
    pass = `seq_gaps == 0` at the receiver and the ring shows 150 frames.
    "Every couple of seconds" moves from ambition to `[M]` here, or it doesn't
    — report the spread either way.
18. **Ingest under load.** Frames arriving during an iperf3 run and during a
    30 s capture → no 5xx, no console stall (`threaded=True` earning its keep).
19. **Long-range profile.** Repeat 17 on the 1 MHz profile — the trail cam's
    realistic deployment radio. Numbers go in the profile table as `[M]`.

## Out of scope

- **Any onward movement of frames.** No relay, no forwarding, no
  queue-for-later, no push notifications. Store-and-forward was rejected in
  roadmap v1 and this issue does not reopen it; the sink is terminal.
- **Node-side work.** Camera driver, JPEG capture, the HTTP client that speaks
  this contract — all mesh-v4. This issue ships the contract and a curl
  reference client, nothing that runs on an ESP32.
- **Image processing.** No thumbnails, transcoding, EXIF parsing, motion
  detection. Frames are stored and served byte-exact.
- **Off-device retention.** The ring is the retention policy. Archival export
  belongs with #27 (backup) — which deliberately excludes bulk image data.
- **Chunked/resumable upload.** A ≤4 MiB body on a link measured in Mbit/s
  needs no resume machinery; meshdata.py-style chunk repair stays a LoRa tool.
- **A dedicated ingest identity.** Uploads use the existing gateway
  credentials; per-node client certs or tokens are future work if the fleet
  grows past two nodes.

## Risks & gotchas

- **The PSK has leaked twice via "harmless" echoes.** The ingest code reads
  `/etc/halow/halow.env` (for `INGEST_*` keys) — the same file that holds
  `HALOW_PASSPHRASE`. Return only the five ingest keys' values, never the
  parsed dict; never log headers (Authorization transits every upload); keep
  `halow.env.example` key-synced so `halowctl diff`'s key-set comparison
  (`scripts/halowctl:207-210`) stays meaningful.
- **SD-card wear vs SD-card fill.** The ring caps bound *space*, not *writes*.
  At the target cadence a 50 KB frame every 2 s is ~2 GB/day of writes — fine
  for bench bring-up, worth a wear-leveling think before months of unattended
  deployment. Deliberately no fsync per frame: a brownout may lose the newest
  frame, and the index rebuilds from filenames (test 14). #22's atomic-write
  discipline covers the index itself.
- **Brownout mid-eviction** leaves index and directory disagreeing; the
  startup rebuild is the recovery path, and it only works because filenames
  carry full metadata. Do not "simplify" the filename format later without
  keeping that property.
- **In-memory rate window and duplicate cache** reset on service restart —
  same accepted tradeoff as the auth throttle (`_FAILS`). A restart admits one
  burst; the ring caps still bound the damage.
- **Werkzeug buffers what you read.** The cap+1 stream read bounds per-request
  memory at ~4 MiB; with `threaded=True` and the 60/min limiter, worst-case
  concurrent buffering is bounded too. Do not raise `INGEST_MAX_BYTES` to tens
  of MB without revisiting this.
- **UI auto-refresh re-renders every 15 s** (`ui/halow_ui.py:1261`). Without
  the immutable cache headers on image GETs the gallery would re-download 24
  frames per refresh over the LAN — set them, and verify with the browser
  devtools network panel once.
- **Pinned-scan interaction (#17):** the camera node lives on the pinned
  channel set; a profile change that strands stations also strands the camera
  mid-burst. Nothing to build here — #17's confirm gate covers it — but expect
  "camera stopped uploading" to sometimes mean "someone changed the channel."
- **Brownout policy (#24):** ingest is receive-side and cheap, but the
  undervoltage ledger should eventually gate the *gallery-heavy* UI refresh
  the same way it gates long captures. Note it in #24's decline list, don't
  build it here.
- **Association forensics (#16):** when the camera "goes quiet", the first
  question is whether the station is even associated. The per-node `seq_gaps`
  and `last_seq` here plus #16's join log answer dead-vs-quiet together; keep
  `X-Node-Id` equal to the `nodes.json` name and the DHCP reservation name so
  the three stores correlate without a fourth inventory (#25's rule).
