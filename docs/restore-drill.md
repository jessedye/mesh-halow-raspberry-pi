# Gateway restore drill

Everything that makes this gateway *this* gateway lives on one SD card:
the SAE passphrase + SSID, tuning profile, DHCP reservations, port
forwards, the console's auth hashes + API token hash, the TLS key/cert,
and the mesh nodes' bearer tokens. The nodes store `halow_ssid`/`halow_psk`
WRITE-ONLY in NVS (reported only as set/unset), so recreating the AP with
a *fresh* passphrase means reconfiguring every node by hand. Restoring the
AP **byte-for-byte** means the fleet rejoins untouched.

`halowctl snapshot` writes to the same card whose death is the disaster.
`halowctl backup` is the encrypted, off-device answer.

## Create a backup

```sh
# on the gateway (or via the Config-tab "encrypted backup" card)
printf '%s' "$HALOW_BACKUP_PASSPHRASE" | sudo halowctl backup
#   -> /var/lib/halow/backup/gateway-backup-<ts>.tar.gpg  (0640 root:halow-ui)
#   gpg symmetric AES256; identity + operator state only; keeps newest 5;
#   test-decrypts before reporting success (a mistyped passphrase is caught
#   NOW, not at disaster time). A backup whose passphrase is lost is a brick.
```

Back up after every identity change (`set-passphrase`, SSID change, new
reservation/forward). The archive passphrase lives ONLY in the bench PC's
gitignored `secrets.env` as `HALOW_BACKUP_PASSPHRASE`.

## Pull it off-device (bench PC, weekly cron)

```sh
source secrets.env   # HALOW_BACKUP_PASSPHRASE, ADMIN_USER, ADMIN_PASS
SHA=$(curl -sk -u "$ADMIN_USER:$ADMIN_PASS" -X POST \
  -F "passphrase=$HALOW_BACKUP_PASSPHRASE" \
  https://halow-gw.local:8443/api/system/backup \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["sha256"])')
curl -sk -u "$ADMIN_USER:$ADMIN_PASS" -o "backups/gateway-$(date +%F).tar.gpg" \
  https://halow-gw.local:8443/api/system/backup
[ "$(sha256sum backups/gateway-$(date +%F).tar.gpg | cut -d' ' -f1)" = "$SHA" ] \
  && echo "verified at the receiver" || echo "MISMATCH — retry"
```

## Restore onto a fresh card

**Order matters** — deploy first, restore second. `deploy.sh` reinstalls
`ui.conf`/`nodes.json` from PC-minted copies on every run; restoring after
deploy is what makes the restored identity win.

```sh
# 1. flash Pi OS Lite, boot, get on the LAN
# 2. on the Pi, from a repo checkout:
./scripts/install.sh            # or --prebuilt for the minutes-long driver restore
# 3. from the bench PC:
./scripts/deploy.sh
# 4. copy the newest tarball to the Pi, then on the Pi:
printf '%s' "$HALOW_BACKUP_PASSPHRASE" | sudo halowctl restore gateway-<date>.tar.gpg --confirm
#   staging in /run (tmpfs) — plaintext never touches the SD; every manifest
#   sha256 verified and every path whitelisted before anything installs;
#   refuses without --confirm if any live file differs (lists PATHS only);
#   idempotent — a second run says "no changes" and touches no services;
#   ends by running verify.sh.
sudo reboot
./scripts/verify.sh             # exits 0 = the stack is back
```

After restore: `halow.env` (SSID + SAE passphrase) and `ui-key.pem` are
byte-identical to the backup, so `iw dev halow0 info` shows ssid `mesh`,
the TLS fingerprint matches what the bench recorded, and a node holding
its write-only credentials associates and gets its reserved 10.117.0.x
address with zero node-side changes.

`restore` is deliberately NOT in the UI's sudoers whitelist — it is a
console/SSH action by a root operator. The UI can create backups, never
restore them.
