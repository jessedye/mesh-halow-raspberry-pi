#!/bin/bash
# Deploy the HaLow gateway stack from this repo to the Pi. Run on the PC:
#   ./scripts/deploy.sh
# Needs: repo-root secrets.env (gitignored) with HALOW_PASSPHRASE=...
# Reuses mesh-v4's ADMIN_USER/ADMIN_PASS/ADMIN_TOKEN for the UI login and
# node proxy so the bench keeps one operator credential. No secret is ever
# echoed or committed; everything sensitive moves over ssh only.
set -euo pipefail

PI=${PI:-pi@192.168.51.202}
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MESH_SECRETS=${MESH_SECRETS:-$HOME/Desktop/mesh-v4/config/secrets.env}

[ -f "$REPO/secrets.env" ] || { echo "missing $REPO/secrets.env (HALOW_PASSPHRASE=...)"; exit 1; }
# shellcheck disable=SC1090,SC1091
. "$REPO/secrets.env"; : "${HALOW_PASSPHRASE:?secrets.env must set HALOW_PASSPHRASE}"
[ -f "$MESH_SECRETS" ] && . "$MESH_SECRETS"
: "${ADMIN_USER:?}" "${ADMIN_PASS:?}" "${ADMIN_TOKEN:?}"

echo "== sync repo"
rsync -a --delete --exclude .git --exclude secrets.env "$REPO/" "$PI:mesh-halow-raspberry-pi/"

echo "== packages"
ssh "$PI" 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y dnsmasq python3-flask isc-dhcp-client >/dev/null 2>&1; true'

echo "== /etc/halow"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT; umask 077
sed -e "s|^HALOW_PASSPHRASE=.*|HALOW_PASSPHRASE=${HALOW_PASSPHRASE}|" \
    "$REPO/config/halow.env.example" > "$T/halow.env"
SALT=$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')
HASH=$(python3 - "$SALT" "$ADMIN_USER:$ADMIN_PASS" <<'PY'
import hashlib,sys
print(hashlib.pbkdf2_hmac("sha256",sys.argv[2].encode(),bytes.fromhex(sys.argv[1]),100000).hex())
PY
)
SESSION_SECRET=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
printf 'AUTH_SALT=%s\nAUTH_HASH=%s\nAUTH_ITER=100000\nSESSION_SECRET=%s\n' "$SALT" "$HASH" "$SESSION_SECRET" > "$T/ui.conf"
sed -e "s|CHANGE-ME|${ADMIN_TOKEN}|g" "$REPO/config/nodes.json.example" > "$T/nodes.json"
scp -q "$T/halow.env" "$T/ui.conf" "$T/nodes.json" "$PI:/tmp/halow-deploy/" 2>/dev/null || {
  ssh "$PI" 'mkdir -p /tmp/halow-deploy && chmod 700 /tmp/halow-deploy'
  scp -q "$T/halow.env" "$T/ui.conf" "$T/nodes.json" "$PI:/tmp/halow-deploy/"
}

echo "== install on the Pi"
ssh "$PI" 'set -e
cd ~/mesh-halow-raspberry-pi
sudo mkdir -p /etc/halow
id halow-ui >/dev/null 2>&1 || sudo useradd -r -s /usr/sbin/nologin -G systemd-journal halow-ui
# Never clobber live operator state (profile/SSID/mode edits) on redeploy.
# Group halow-ui: the console reads SSID/profile/mode from this file.
[ -f /etc/halow/halow.env ] || sudo install -m640 -o root -g halow-ui /tmp/halow-deploy/halow.env /etc/halow/
sudo chgrp halow-ui /etc/halow/halow.env && sudo chmod 640 /etc/halow/halow.env
# ui.conf and nodes.json are read by the unprivileged UI user
sudo install -m640 -o root -g halow-ui /tmp/halow-deploy/ui.conf /tmp/halow-deploy/nodes.json /etc/halow/
rm -rf /tmp/halow-deploy
sudo install -m644 config/halow-profiles.json config/nftables-halow.conf /etc/halow/
sudo install -m644 config/dnsmasq-halow.conf /etc/dnsmasq.d/halow.conf
sudo install -m644 config/99-halow-unmanaged.conf /etc/NetworkManager/conf.d/
sudo install -m644 config/99-halow-net.rules /etc/udev/rules.d/
sudo install -m755 scripts/halowctl /usr/local/bin/
sudo install -m644 systemd/halow-ap.service systemd/halow-net.service systemd/halow-sta.service systemd/halow-ui.service /etc/systemd/system/
sudo install -m440 config/sudoers-halow-ui /etc/sudoers.d/halow-ui
sudo mkdir -p /usr/local/lib && sudo install -m644 ui/halow_ui.py /usr/local/lib/
sudo chgrp halow-ui /etc/halow/ui.conf && sudo chmod 640 /etc/halow/ui.conf
[ -f /etc/halow/ui-cert.pem ] || sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout /etc/halow/ui-key.pem -out /etc/halow/ui-cert.pem -days 3650 -nodes \
  -subj "/CN=halow-gw" 2>/dev/null
sudo chgrp halow-ui /etc/halow/ui-key.pem /etc/halow/ui-cert.pem && sudo chmod 640 /etc/halow/ui-key.pem
sudo udevadm control --reload
sudo systemctl daemon-reload
sudo systemctl enable halow-net halow-ap halow-ui dnsmasq >/dev/null 2>&1
sudo systemctl restart halow-net halow-ui
sudo systemctl restart dnsmasq || true
# The AP only starts once the radio interface exists; harmless to try.
sudo systemctl restart halow-ap || true
echo INSTALLED'

echo "== verify"
ssh "$PI" 'systemctl is-active halow-net halow-ui dnsmasq halow-ap || true;
curl -sk -o /dev/null -w "UI https: %{http_code}\n" https://localhost:8443/ || true'
