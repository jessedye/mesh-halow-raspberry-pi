# HT-HC01P (WiFi HaLow, Morse Micro MM6108) → Raspberry Pi 4 wiring

As-built 2026-08-05 on the Pi 4 gateway (`192.168.51.202`). This is the same
GPIO assignment as Morse Micro's own Pi HAT (MMECH06 / EKH01 eval kits) —
Heltec copied their map, so Morse's stock `mm610x-spi` device-tree overlay
matches this wiring with no edits.

## The pin map

| HT-HC01P signal | Module edge pad | Pi header pin | BCM GPIO | Purpose |
|---|---|---|---|---|
| 3V3 | 39 / 41 / 52 (any) | 1 or 17 | — | 3.3 V power (see budget note) |
| GND | many | 6 | — | Ground |
| SPI_CLK | 45 | 23 | GPIO11 | SPI0 clock |
| SPI_MOSI | 49 | 19 | GPIO10 | Pi → module |
| SPI_MISO | 47 | 21 | GPIO9 | Module → Pi |
| SPI_CS | 51 | 24 | GPIO8 (CE0) | Chip select, active-low, software CS via `cs-gpios` |
| SPI_INT / IRQ | J2 pad 10 | 22 | GPIO25 | Module interrupt (data ready) |
| RESET_N | J2 pad 22 | 29 | GPIO5 | Active-low reset |
| WAKEUP_IN | 33 | 5 | GPIO3 | Module wake (first `power-gpios` entry) |
| BUSY | 31 | 26 | GPIO7 | Module busy (second `power-gpios` entry) |

Module edge-pad numbers were verified on the bench during the ESP32 bring-up
(chip ID 0x0406 read on two modules) — see `mesh-v4/docs/halow-wiring.md`.

## Pin conflicts the overlay and config already handle

- **GPIO7 is SPI0 CE1.** The overlay rewrites the base dtb's `spi0_cs_pins`
  pinctrl group to contain only GPIO8, so the SPI controller never claims
  GPIO7 and the BUSY line owns it. Do not add `dtparam=spi=on` overrides that
  re-instate the two-CS group.
- **GPIO3 is I2C1 SCL** and has a fixed 1.8 kΩ pull-up to 3.3 V on the Pi.
  Fine as the driver-driven WAKE output, but **never enable `i2c_arm`** in
  `config.txt` while the module is wired — the I2C controller would fight the
  WAKE line. (GPIO3 is also the Pi's power-button input; a module driving it
  low while the Pi is halted would wake the Pi. Powered and running, it is
  just a GPIO.)
- **CS is active-low via `cs-gpios = <&gpio 8 1>`** (flag 1 = ACTIVE_LOW),
  handled by gpiolib, not the SPI block's hardware CS.

## Power budget

MM6108 TX at 21 dBm draws ~200–250 mA total; listen 30–75 mA (datasheet
figures, measured optimistic elsewhere in this project). That load is on the
Pi's 3.3 V rail, which shares the PMIC budget with the SD card and onboard
peripherals — spec guidance for the header's 3V3 pins is ~500 mA combined.
One module fits; do not hang anything else significant off 3V3.

## Fault localization 2026-08-05 (scripts/wire-census.sh)

Measured on the as-built harness while the probe was failing with MISO
constant 0xFF:

- Asserting RESET_N (GPIO5 low) drops the module's IRQ drive on GPIO25;
  releasing it and running a driver probe raises IRQ again. That proves the
  3V3/GND, RESET, and IRQ wires, module boot, and that probe-time SPI
  activity reaches the chip (so CLK and CS land on real module inputs).
- Yet no byte the module sends ever appears on the Pi's MISO (pull-follow
  0xFF at every clock rate).

Elimination first suggested a crossed data pair. **The swap disproved
that** (2026-08-05 12:14): with the two data wires exchanged, the module
stopped reacting to probe traffic entirely — no IRQ rise at all, where the
original arrangement always produced one. A crossed-but-healthy pair
cannot produce that asymmetry; a **dead wire** produces exactly it:

- Original position (Pi pin 21, MISO role): commands still reached the
  module on the good wire (IRQ reacted), replies died on the dead one.
- Swapped position (Pi pin 19, MOSI role): commands die on the dead wire,
  module hears nothing, zero reaction.

**Verdict: the jumper originally at Pi pin 21 (module pad 47) is broken —
bad conductor, bad crimp, or cold joint at the module pad.** Fix: restore
the original arrangement and replace that wire (or both data jumpers)
with fresh ones; if it persists, continuity-beep Pi pin 21 ↔ module pad
47 and reflow the pad-47 joint. The wire-census asymmetry (IRQ reacts vs
not) is the discriminator between a crossed pair and a dead wire — the
ESP32 bench never had this test, which is why its cross diagnosis
transferred here too eagerly.

## Traps already paid for on the ESP32 bring-up (do not rediscover)

- **MISO/MOSI crossed = total, perfect silence.** Commands enter the module's
  output pin; nothing drives anything, and every line pull-follows. No passive
  measurement can see it — only a clocked transaction (driver probe) or a
  pin-to-pad continuity beep tells the truth. Two modules were nearly
  condemned this way. If the driver probes nothing, swap MISO/MOSI first.
- **The chip locks up until RESET_N if a CS window carries a non-multiple-of-8
  clock count.** Chip-level behaviour, not host-specific.
- The ESP32 needed a one-bit realignment shim (responses arrived one bit late
  through the GPIO matrix). The Pi's dedicated SPI block should not need it —
  but if the driver reads garbage at high clock rates, drop
  `spi-max-frequency` in the overlay before suspecting the module.
- **A solder bridge from 3V3 to GND on the module side** collapses the rail:
  host plays dead / port overcurrent-cuts. Disconnect the module power wire to
  prove it; the host revives instantly.
