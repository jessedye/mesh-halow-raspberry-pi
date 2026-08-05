#!/bin/bash
# Runs automatically when halow0 appears (halow-first-contact.service).
# Captures what the working chip actually reports, once per boot, into
# /var/log/halow-first-contact.log — the data verify.sh's success
# signature and the profile table's [M] annotations need.
set -u
PATH=/usr/sbin:/usr/local/bin:$PATH
LOG=/var/log/halow-first-contact.log

sleep 5   # let the driver finish firmware load and interface setup

{
  echo "===== halow first contact $(date -Is) kernel $(uname -r)"
  echo "--- kernel probe lines"
  journalctl -k -q --no-pager -g "morse" -n 30
  echo "--- interfaces"
  iw dev
  echo "--- phy capabilities (S1G section)"
  PHY=$(iw dev halow0 info 2>/dev/null | awk '/wiphy/{print $2}')
  [ -n "${PHY:-}" ] && iw phy "phy$PHY" info | head -80
  echo "--- morse_cli"
  morse_cli -i halow0 version 2>&1
  morse_cli -i halow0 stats 2>&1 | head -20
  echo "===== end first contact"
} >> "$LOG" 2>&1

chmod 644 "$LOG"
