#!/bin/bash
# Post-boot verification of the MM6108 HaLow stack. Run on the Pi.
# Every check prints PASS/FAIL; exits nonzero if any FAIL.
set -u
fail=0
chk() { # chk <label> <command...>
  local label=$1; shift
  if out=$("$@" 2>&1) && [ -n "$out" ]; then
    echo "PASS  $label: $(echo "$out" | head -1)"
  else
    echo "FAIL  $label"
    fail=1
  fi
}

chk "overlay loaded (spi0 has mm6108 node)" \
    ls /proc/device-tree/soc/spi@7e204000/mm6108@0/compatible
chk "morse.ko loaded" sh -c 'lsmod | grep -E "^morse "'
chk "dot11ah.ko loaded" sh -c 'lsmod | grep -E "^dot11ah"'
chk "SPI device bound" sh -c 'ls -l /sys/bus/spi/drivers/morse-spi/ 2>/dev/null | grep spi0.0'
chk "chip probed (dmesg)" sh -c 'dmesg | grep -iE "morse.*(chip|fw|firmware)" | tail -3'
chk "wlan interface exists" sh -c 'iw dev | grep -A1 "phy" | grep Interface'
chk "morse_cli answers" sh -c 'IF=$(iw dev | awk "/Interface/{print \$2; exit}"); morse_cli -i "$IF" version'
echo
dmesg | grep -i morse | tail -10
exit $fail
