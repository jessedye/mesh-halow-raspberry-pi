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
| `docs/wiring.md` | Module-pad ↔ Pi-header pin map, conflicts, hardware traps |
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
- `192.168.51.201/23` static on eth0 (gw 192.168.50.1, DNS 1.1.1.1/8.8.8.8), user `pi`
- HT-HC01P wired per `docs/wiring.md` (Morse HAT-compatible map)

## Quick start (fresh Pi)

```sh
scp -r . pi@192.168.51.201:mesh-halow-raspberry-pi
ssh pi@192.168.51.201
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
