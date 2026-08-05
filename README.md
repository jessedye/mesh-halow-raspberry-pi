# mesh-halow-raspberry-pi

Raspberry Pi 4 WiFi HaLow (802.11ah) gateway for the mesh-v4 bench — a
Heltec **HT-HC01P** module (Morse Micro **MM6108**) on SPI0, running the
Morse out-of-tree driver on stock Raspberry Pi OS Trixie.

The intended topology: this Pi as S1G AP in the US 902–928 MHz band; the two
Heltec V4.2 bench nodes (same module, ESP32 `mm-iot` stack) join as STAs,
giving the mesh-v4 transport ladder its HaLow rung.

## Layout

| Path | What |
|---|---|
| `docs/wiring.md` | Debug-board header ↔ Pi pin map, SPI timing, fault history |
| `docs/software-stack.md` | Pinned versions, why each, caveats, regulatory |
| `overlays/mm610x-spi-overlay.dts` | Device-tree overlay for the SPI wiring |
| `config/morse.conf` | `/etc/modprobe.d` options (BCF + `country=US`) |
| `config/config.txt.snippet` | Boot config line |
| `scripts/install.sh` | Full provisioning of a fresh Pi (idempotent); `--driver-only` after kernel upgrades |
| `scripts/verify.sh` | Post-boot PASS/FAIL checks |
| `patches/` | Morse's RPi kernel patches (NOT applied — reference) |
| `vendor/` | Pinned source tarballs of every upstream (Morse archives repos) |

## Gateway node

- Pi 4B Rev 1.5 (8 GB), Pi OS Lite 64-bit Trixie, kernel 6.18.39+rpt-rpi-v8
- `192.168.51.202/23` static on eth0 (gw 192.168.50.1, DNS 1.1.1.1/8.8.8.8), user `pi`
- HT-HC01P on Heltec's debug-board 10-pin header, wired per `docs/wiring.md`
- Onboard 2.4 GHz WiFi AP `mesh-2g` on wlan0 (NM `shared`, 10.42.0.1/24, same passphrase; toggle: `halowctl wifi-ap on|off`)

### Pin locations (full detail in `docs/wiring.md`)

| Debug header | Pi pin | BCM | | Debug header | Pi pin | BCM |
|---|---|---|---|---|---|---|
| 3V3 | 1 | — | | BUSY | 26 | GPIO7 |
| GND | 6 | — | | WAKE | 5 | GPIO3 |
| INT | 22 | GPIO25 | | CLK | 23 | GPIO11 |
| RESET | 29 | GPIO5 | | MISO | 21 | GPIO9 |
| CS | 24 | GPIO8/CE0 | | MOSI | 19 | GPIO10 |

Pin 1 = corner nearest the SD card; odd pins are the inner row. **Pin 2
(5 V) stays empty.** SPI timing that works: 4 MHz + 64-byte ack windows
(`config/morse.conf`) — do not raise without re-testing firmware load.

## Quick start (fresh Pi)

```sh
scp -r . pi@192.168.51.202:mesh-halow-raspberry-pi
ssh pi@192.168.51.202
cd mesh-halow-raspberry-pi && ./scripts/install.sh
sudo reboot
./scripts/verify.sh
```

## State log

- **2026-08-05** — Pi flashed (Pi OS Lite arm64 2026-06-18), static IP up,
  stack built on-device: driver `mm6108-2.0.1` (+ pinned `mm_rate_control`
  submodule, absent from release tarballs), firmware branch `2.0`,
  `bcf_mf08551.bin` from the v1.15.3 BCF release, `morse_cli`, S1G hostap.
  Build friction recorded in `docs/software-stack.md` (KCFLAGS workaround).
- **2026-08-05 (later)** — Full gateway stack deployed and verified minus RF:
  profile system (`halowctl`, 4 width tiers), router (nftables NAT +
  dnsmasq `bind-dynamic` + forwarding), web console on :8443 (HTTPS, basic
  auth = mesh-v4 admin credential, node proxy verified against node1;
  node2 was down on the LAN independently). **Chip probe BLOCKED on
  hardware**: driver + overlay + CS-training patch all verified correct;
  MISO reads constant 0xFF (nothing driving) while the module drives IRQ
  high — the mesh-v4 crossed-MISO/MOSI signature. Swap the wires at Pi
  header pins 19/21 and rerun `scripts/verify.sh`. `halow-ap` is bound to
  the `halow0` device unit and will start itself the moment the probe
  succeeds. Temporary MISO hexdump still in the on-Pi driver copy
  (`~/halow/morse_driver/spi.c`) — remove after first contact.

## Disaster recovery

`prebuilt/` holds the compiled artifacts for the pinned kernel
(`6.18.39+rpt-rpi-v8`): `morse.ko` (unstripped), `dot11ah.ko`, and
`s1g-bins.tar.gz` (hostapd/wpa_supplicant S1G + morse_cli). A fresh SD
card needs only Pi OS + `scripts/install.sh` (which rebuilds), or these
binaries dropped in place for a minutes-long restore on the same kernel.

## First contact

- **2026-08-05 13:12 — FIRST CONTACT & FULL BRING-UP.** After the dead MISO
  jumper was replaced, the chip probed (fw `rel_mm6108_2_0_1`), and the
  `mesh` AP came up: S1G ch48, 926 MHz, 4 MHz wide, WPA3-SAE. Working SPI
  timing: 4 MHz + 64-byte ack windows (see `config/morse.conf` — the 0x40xx
  register block acks outside the driver's default 4-byte window; identical
  at 1 and 4 MHz, so structural). hostapd needed `hw_mode=a` (S1G presents
  as mapped 5 GHz) and `rsn_pairwise=CCMP`. Cold-reboot verified: 9/9
  verify.sh PASS, halow0 at 10.117.0.1/24, dnsmasq serving, both APs
  (`mesh` HaLow + `mesh-2g` 2.4 GHz) beaconing. Debug hexdump removed.

## Router console

`https://192.168.51.202` (standard port; an nftables redirect lands it on
the unprivileged listener at :8443, which also still answers directly).
Login = the mesh-v4 admin credential. Works from the LAN, HaLow clients
(10.117.0.1), and `mesh-2g` WiFi clients (10.42.0.1).

## Configuration API

Every UI mutation is equally scriptable. Auth: session cookie (browser),
HTTP Basic (`curl -u`), or `Authorization: Bearer <ADMIN_TOKEN>` (the
mesh-v4 token; only its sha256 is stored on the gateway).

| Endpoint | Form fields |
|---|---|
| `GET /api/config` | — (current halow/wifi/dhcp/forwards state) |
| `POST /api/config/halow` | `ssid`, `passphrase` (write-only), `mode=ap\|sta`; identity changes need `confirm=1` |
| `POST /api/config/wifi` | `ssid`, `channel` (1-11), `passphrase` (write-only), `enabled=on\|off` |
| `POST /api/config/dhcp` | `start`, `end` (10.117.0.x), `lease` (12h), `dns` (csv) |
| `POST /api/config/forwards` | `op=add\|del`, `proto=tcp\|udp`, `ext`, `dest=10.117.0.x:port` |
| `POST /api/system/reboot` | `confirm=1` |
| `POST /api/halow/profile` | `name` (long-range/mid-range/balanced/max-rate) |
| `POST /api/halow/probe` | — |

All mutations route through `halowctl` subcommands — the sudoers file is
the complete privileged surface. Passphrases travel on stdin, never argv.
