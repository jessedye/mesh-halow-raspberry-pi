# HT-HC01P (WiFi HaLow, Morse Micro MM6108) → Raspberry Pi 4 wiring

**OPERATIONAL as-built map, verified 2026-08-05** — chip probes at boot,
`mesh` AP beaconing at 926 MHz. The module sits on **Heltec's HT-HC01P
debug board**, so all connections are Dupont jumpers on its labeled
10-pin header — no soldering to the mini-PCIe edge pads. The GPIO
assignment matches Morse Micro's own Pi HAT (MMECH06 / EKH01), which is
why Morse's stock `mm610x-spi` overlay works unedited.

## The pin map

| Debug header | Pi physical pin | Pi BCM | Function |
|---|---|---|---|
| **3V3** | **1** (or 17) | — | 3.3 V power — see power note |
| **GND** | **6** | — | Ground |
| **INT** | **22** | GPIO25 | Interrupt, module → Pi |
| **RESET** | **29** | GPIO5 | Reset, active-low |
| **BUSY** | **26** | GPIO7 | Busy, module → Pi |
| **WAKE** | **5** | GPIO3 | Wake, Pi → module |
| **CLK** | **23** | GPIO11 | SPI0 SCLK |
| **MISO** | **21** | GPIO9 | SPI0 MISO, module → Pi |
| **MOSI** | **19** | GPIO10 | SPI0 MOSI, Pi → module |
| **CS** | **24** | GPIO8 (CE0) | Chip select, active-low (software CS) |

## Pi header orientation

Pin 1 is the corner pin nearest the SD-card end. Odd pins (1,3,…,39) are
the **inner** row (toward the CPU); even pins (2,4,…,40) the **edge** row.
Column by column, only these columns carry wires:

```
col:      1     3      10      11      12      13      15
inner:  1=3V3  5=WAKE  19=MOSI 21=MISO 23=CLK   -      29=RESET
edge:    -     6=GND     -     22=INT  24=CS   26=BUSY   -
```

**Pin 2 (edge row, column 1) is 5 V — it must stay empty.** The one
mis-seating that took the whole harness dark was the 3V3/WAKE pair
drifting one column (see fault history below).

## Mini-PCIe edge-pad reference (raw module, no debug board)

From the ESP32 bench bring-up (chip ID 0x0406 read on two modules):
CLK=45, MISO=47, MOSI=49, CS=51, INT=J2-10, RESET_N=J2-22, BUSY=31,
WAKE=33, 3V3=39/41/52, GND=many. See `mesh-v4/docs/halow-wiring.md`.

## Working SPI timing (do not "clean up")

`/etc/modprobe.d/morse.conf` (repo `config/morse.conf`):
`spi_clock_speed=4000000 spi_post_write_status_bytes=64
spi_inter_block_delay_bytes=64`. 50 MHz never answered over jumpers;
CMD53 bulk corrupted at boot-time 50 MHz; and the chip acks writes to its
0x40xx clock/PLL registers **later than the driver's default 4-byte scan
window** — identical at 1 and 4 MHz, so structural, fixed by the 64-byte
windows. The overlay also pins `spi-max-frequency = <4000000>`.

## Pin conflicts the overlay and config already handle

- **GPIO7 is SPI0 CE1.** The overlay rewrites `spi0_cs_pins` to contain
  only GPIO8, freeing GPIO7 for BUSY.
- **GPIO3 is I2C1 SCL** (fixed 1.8 kΩ pull-up). Fine as WAKE; **never
  enable `i2c_arm`** while the module is wired.
- **CS is active-low via `cs-gpios = <&gpio 8 1>`**, driven by gpiolib.
- Stock kernels ignore `SPI_CS_HIGH` for gpiod chip selects, which broke
  the driver's CS-high training burst — repo patch 0001 redoes it with
  `SPI_NO_CS`.

## Power

MM6108 TX at 21 dBm draws ~200–250 mA; the as-built harness feeds from
Pi 3V3 pin 1, inside but near the header's ~500 mA guidance — keep other
3V3 loads off. Heltec's diagram prefers an external 3.3 V regulator fed
from Pi 5 V (pins 2/4) with common ground; adopt that if TX-burst
brownouts ever appear. **Never feed Pi 5 V directly into module 3V3.**

## Fault history (2026-08-05, condensed — the method matters)

The chip was silent (MISO constant 0xFF) through three rework rounds.
`scripts/wire-census.sh` senses the module through its one passively
observable output (IRQ vs GPIO25's pull-down) while toggling the one
non-SPI input (RESET_N):

1. Original harness: IRQ obeyed RESET and rose on probe traffic — power,
   RESET, IRQ, CLK, CS all proven; replies still never arrived → reply
   path dead.
2. After swapping the data pair: zero IRQ reaction — commands now died on
   the same dead conductor. A crossed-but-healthy pair cannot produce
   that asymmetry; **a dead wire produces exactly it.**
3. Mid-rework the census went fully dark: the 3V3/WAKE connectors had
   drifted one column at the Pi end (power through the I²C pull-up).
4. **Resolution: replacing the dead MISO jumper (+ correct re-seating)
   brought first contact within seconds.**

Lesson recorded for the next silent SPI device: run the census before
borrowing a diagnosis — the ESP32 bench's "crossed pair" story fit the
symptom and was wrong here. Only a continuity beep or a clocked
transaction tells the truth about a wire.

## Traps already paid for on the ESP32 bring-up (still apply)

- **The chip locks up until RESET_N if a CS window carries a
  non-multiple-of-8 clock count.**
- A solder bridge from 3V3 to GND on the module side collapses the rail
  and makes the *host* play dead (port overcurrent). Disconnect the
  module power wire to prove it.
- The ESP32's one-bit-late read realignment did **not** reproduce on the
  Pi's dedicated SPI block; block-mode CMD53 works with the ack windows
  above. (A parallel bench effort ported byte-mode/bit-shift fixes as
  repo patch 0002 — reference only, not in the running build.)
