#!/bin/bash
# Wire-level census for the HT-HC01P harness, no test equipment needed.
# Exploits the one module output we can sense passively (IRQ, GPIO25 with
# the Pi's pull-DOWN under it) and the one input we control outside SPI
# (RESET_N, GPIO5). Run on the Pi; safe to repeat.
#
# Interpretation:
#   IRQ drops when RESET asserted  -> power, RESET wire, IRQ wire, module OK
#   IRQ rises after a driver probe -> SPI activity reaches the chip (CLK/CS OK)
#   both true + MISO constant 0xFF -> the data pair is miswired: swap the
#                                     MOSI/MISO wires (Pi header pins 19/21)
set -eu
PATH=/usr/sbin:/usr/local/bin:$PATH

echo "1) baseline:            $(pinctrl get 25)"
sudo pinctrl set 5 op dl
sleep 1
echo "2) RESET_N held low:    $(pinctrl get 25)   (drop = reset+irq wires good)"
sudo pinctrl set 5 op dh
sleep 2
echo "3) after reset release: $(pinctrl get 25)   (low = idle after clean boot)"
sudo pinctrl set 5 ip
sudo sh -c "rmmod morse 2>/dev/null; modprobe morse" >/dev/null 2>&1
sleep 3
echo "4) after driver probe:  $(pinctrl get 25)   (rise = SPI activity reaches chip)"
journalctl -k -q --no-pager -g "morse" -n 3 | grep -q "probe failed" \
  && echo "probe: FAILED (see halowctl probe)" || echo "probe: OK"
