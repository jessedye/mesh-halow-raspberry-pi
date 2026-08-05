#!/bin/bash
# Post-boot verification of the MM6108 HaLow stack. Run on the Pi.
# Every check prints PASS/FAIL; exits nonzero if any FAIL.
set -u
PATH=/usr/sbin:/usr/local/bin:$PATH
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

chk "morse.ko exists for RUNNING kernel" \
    sh -c 'ls /lib/modules/$(uname -r)/updates/morse.ko* /lib/modules/$(uname -r)/extra/morse.ko* 2>/dev/null | head -1'
chk "overlay loaded (spi0 has mm6108 node)" \
    ls /proc/device-tree/soc/spi@7e204000/mm6108@0/compatible
chk "morse.ko loaded" sh -c 'lsmod | grep -E "^morse "'
chk "dot11ah.ko loaded" sh -c 'lsmod | grep -E "^dot11ah"'
chk "SPI device bound" sh -c 'ls -l /sys/bus/spi/drivers/morse_spi/ 2>/dev/null | grep spi0.0'
# Success signature unknown until first contact on this platform; the
# honest check is that the most recent probe attempt did not fail.
chk "last probe attempt clean" sh -c 'journalctl -k -q --no-pager -g "morse" -n 10 | grep -q "probe failed" && exit 1; echo "no probe failure in recent kernel log"'
chk "wlan interface exists" sh -c 'iw dev | grep -A1 "phy" | grep Interface'
chk "morse_cli answers (fw version)" sh -c 'sudo morse_cli -i halow0 version 2>/dev/null | grep "FW Version"'
chk "AP enabled (mesh beaconing)" sh -c 'iw dev halow0 info | grep -E "ssid|type AP"'
chk "2.4GHz AP (mesh-2g)" sh -c 'nmcli -t -f NAME,STATE con show --active | grep mesh-2g'
# chronyd -p prints the EFFECTIVE merged config — guards the conf.d
# ordering gotcha and future base-file drift, not just our drop-in text.
chk "chrony local holdover directive effective" sh -c 'sudo chronyd -p 2>/dev/null | grep -E "^local"'
# Trixie splits fake-hwclock into -load/-save units and MASKS the legacy
# monolithic unit — test the load unit, fall back to the old name.
chk "fake-hwclock enabled" sh -c 'systemctl is-enabled fake-hwclock-load.service 2>/dev/null | grep -E "enabled|static" || systemctl is-enabled fake-hwclock 2>/dev/null | grep -E "^enabled|^static"'
chk "chronyc answers (tracking)" sh -c 'chronyc -c tracking | head -1'
chk "kernel guard state ok" sh -c 'grep -o "\"state\": \"ok\"" /var/lib/halow/kernel-guard.json'
echo
journalctl -k -q --no-pager -g "morse" -n 10
exit $fail
