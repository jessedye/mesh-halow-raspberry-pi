# Software stack: MM6108 HaLow on Raspberry Pi OS Trixie (kernel 6.18)

Established 2026-08-05. Every version below was chosen for a reason; change
them together, not individually.

## Target platform

- Raspberry Pi 4 Model B Rev 1.5 (8 GB), Raspberry Pi OS Lite 64-bit
  (Trixie, 2026-06-18 image), kernel `6.18.39+rpt-rpi-v8`.
- Gateway node: `192.168.51.202/23` static on eth0, user `pi`.

## Kernel status — read before upgrading anything

- The morse driver is **not in mainline** and not in Raspberry Pi's kernel
  (verified against torvalds/linux and rpi-6.18.y, Aug 2026).
- Morse Micro's kernel *patches* (mesh, powersave, S1G ECSA, NDP block-ack)
  exist only for their forks up to **rpi-6.12.21**. There is no 6.18 series.
- Per Morse staff: an **unpatched** kernel + out-of-tree driver gives a basic
  AP/STA HaLow network, without powersave or extended channel switch. That is
  what this gateway runs. If mesh-mode HaLow is ever needed, the path is
  Morse's rpi-linux fork at 6.12, not stock Trixie.

## Components (all pinned)

| Component | Repo | Version | Why |
|---|---|---|---|
| Driver (`morse.ko`, `dot11ah.ko`) | github.com/MorseMicro/morse_driver | tag `mm6108-2.0.1` | Only 2.0.x/main carry `KERNEL_VERSION(6,18,0)` compat guards; 1.17.x tops out ~6.12 |
| Chip firmware | github.com/MorseMicro/morse-firmware | branch `2.0` | README: match the driver's major version. Installs `mm6108.bin` → `/lib/firmware/morse/` |
| BCF (board calibration) | github.com/MorseMicro/bcf_binaries release **v1.15.3** | `bcf_mf08551.bin` | Heltec-specified for the HT-HC01P. **Not carried in the 2.0 firmware repo** — see caveat |
| S1G hostap (`wpa_supplicant_s1g`, `hostapd_s1g`) | github.com/MorseMicro/hostap | tag `mm6108-2.0.1` | Stock wpa_supplicant has no S1G keys (`ieee80211ah`, `op_class`, `s1g_*`); the fork is required |
| CLI | github.com/MorseMicro/morse_cli | tag `mm6108-2.0.1` | Chip/RF introspection (`morse_cli`), nl80211 transport |
| DT overlay | MorseMicro/openwrt `3.1-dev`, patch 991-0003 | `overlays/mm610x-spi-overlay.dts` | Matches the Morse HAT GPIO map, which is our wiring |

All upstream sources are archived as tarballs in `vendor/` — Morse already
archived their ESP32 SDK repo (July 2026); assume these can vanish too.

## Known caveats / open questions

- **BCF version mix.** `bcf_mf08551.bin` last shipped in the 1.15.3 release
  stream; the 2.0 firmware repo does not carry it. Running it under the
  2.0.1 driver is a version mix Morse's README advises against. It probed
  successfully here (see README state log) — if RF behaviour ever looks
  wrong (power, sensitivity), this is the first suspect. `bcf_mf28551.bin`
  in the 2.0 repo is a sibling board's file, not a drop-in.
- **No DKMS.** The driver tree has no dkms.conf; a kernel upgrade
  (`apt full-upgrade` pulling a new `linux-image-rpi-v8`) silently orphans
  `morse.ko`. Rebuild with `scripts/install.sh --driver-only` after any
  kernel bump, or hold the kernel packages.
- **Morse's two 999-* kernel patches are NOT applied** (SPI CS polarity flag,
  GPIO base 0). They target Morse's patched kernels; nothing in a stock
  kernel sets the flag they add. Archived in `patches/` in case a CS-polarity
  symptom ever appears (chip never responds despite correct wiring).
- **Trixie toolchain**: community reports of build failures from stricter
  compiler flags on Trixie. Record any flags we had to add in the README
  state log.

## Regulatory (US, 902–928 MHz)

Three layers must agree:

1. Driver module parameter: `country=US` (build default is AU) — set in
   `/etc/modprobe.d/morse.conf` (`config/morse.conf` here).
2. The BCF carries board RF limits.
3. `hostapd_s1g` / `wpa_supplicant_s1g` config: `country_code=US` plus an
   S1G `op_class`/`channel` pair valid for US.

`morse_cli country_code` only reads the OTP bank — it is not the selector.

## Interop notes (mesh-v4 bench)

- The two Heltec V4.2 nodes carry the same MM6108 module driven by the
  ESP32 `mm-iot` stack (chip ID 0x0406 verified both nodes, 2026-08-04).
  Their SDK is deprecated in favour of the `morsemicro/halow` ESP-IDF
  component. Either way, ESP32 STAs ↔ this Pi AP is the intended topology.
- US HaLow shares 902–928 MHz with the bench's LoRa (SX1262 at 915 MHz).
  Co-siting hazard: a keyed LoRa transmit puts ~+18 dBm into a co-sited
  HaLow front end at 0.1 m. The Pi gateway is not co-sited with the nodes'
  LoRa antennas, but the *nodes* are — mesh-v4 issue #119.

## Time

chrony serves the HaLow/mesh-2g subnets (`config/chrony-halow.conf`) with
`local stratum 10` holdover: an unsynchronized gateway (field power-cycle
with the upstream down) still serves its own clock — drilled 2026-08-05,
unfixed config refused clients ("Timeout reached"), fixed config answers
at stratum 10 within ~10s. Holdover is approximately right thanks to the
Debian driftfile + fake-hwclock (NOT on the stock Trixie image — deploy.sh
installs it; Trixie ships it as fake-hwclock-load/-save units with the
legacy unit masked). `/api/system` carries `time_sync` (synced/holdover/
unknown + raw stratum/ref_id/ref_age_s); metrics history tracks
`time_synced` per minute.

Coordination flag for mesh-v4: Meshtastic ignores DHCP option 42
(WiFiAPClient.cpp uses `config.network.ntp_server`) — the nodes must set
`network.ntp_server = 10.117.0.1` or they will chase the default pool
through NAT, which dies exactly when the upstream does.


## Kernel-upgrade interlock (roadmap 23)

No DKMS, by choice — the KCFLAGS workaround and mmrc pin live in
install.sh, and carrying them inside dpkg triggers means debugging them
blind. Instead `halow-kernel-guard` watches for orphaned modules (apt
Post-Invoke + boot unit), restores via prebuilt/ (4s [M]) or
install.sh --driver-only (18s warm [M]), and apt-mark holds the kernel
metapackages when it cannot. State: /var/lib/halow/kernel-guard.json;
`halowctl driver status|rebuild|hold|unhold`.
