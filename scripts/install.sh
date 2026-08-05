#!/bin/bash
# Provision a Raspberry Pi (Pi OS Trixie 64-bit, Lite) as an MM6108 HaLow node
# for the Heltec HT-HC01P wired per docs/wiring.md.
#
# Run ON THE PI, as a user with sudo, from a checkout/copy of this repo:
#   ./scripts/install.sh              full install
#   ./scripts/install.sh --driver-only  rebuild+reinstall just the kernel modules
#                                       (needed after every kernel upgrade — no DKMS)
#
# Versions are pinned in docs/software-stack.md. Sources come from vendor/
# tarballs when present (survives upstream repo deletion), else GitHub.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK=~/halow
DRIVER_TAG=mm6108-2.0.1
FW_BRANCH=2.0
MMRC_SHA=da1425580bd5caa1a8fc926596f366bdd8d841d2

fetch() { # fetch <name> <giturl> <ref> <vendor-tarball-glob>
  local name=$1 url=$2 ref=$3 glob=$4 tb
  [ -d "$WORK/$name" ] && return 0
  tb=$(ls "$REPO_DIR"/vendor/$glob 2>/dev/null | head -1 || true)
  if [ -n "$tb" ]; then
    mkdir -p "$WORK/$name"
    tar -xzf "$tb" -C "$WORK/$name" --strip-components=1
  else
    git clone --depth 1 -b "$ref" "$url" "$WORK/$name"
  fi
}

driver_build() {
  cd "$WORK/morse_driver"
  # rate-control submodule is NOT in release tarballs and must match the pin
  if [ ! -f mmrc-submodule/src/core/mmrc.h ]; then
    rm -rf mmrc-submodule && mkdir -p mmrc-submodule
    local tb
    tb=$(ls "$REPO_DIR"/vendor/mm_rate_control-*.tar.gz 2>/dev/null | head -1 || true)
    if [ -n "$tb" ]; then
      tar -xzf "$tb" -C mmrc-submodule --strip-components=1
    else
      git clone https://github.com/MorseMicro/mm_rate_control.git mmrc-submodule
      git -C mmrc-submodule checkout -q "$MMRC_SHA"
    fi
  fi
  # KCFLAGS: stock kernels lack Morse's SPI_CONTROLLER_ENABLE_CS_GPIOD patch;
  # the driver #warns about it and Pi OS builds with warnings-as-errors.
  # The fallback (gpiolib handles CS polarity from the DT flag) is correct.
  make -j"$(nproc)" KERNEL_SRC=/lib/modules/"$(uname -r)"/build \
    KCFLAGS=-Wno-error=cpp \
    CONFIG_WLAN_VENDOR_MORSE=m CONFIG_MORSE_SDIO=y CONFIG_MORSE_SPI=y \
    CONFIG_MORSE_USER_ACCESS=y CONFIG_MORSE_VENDOR_COMMAND=y
  sudo make KERNEL_SRC=/lib/modules/"$(uname -r)"/build modules_install
  sudo depmod -a
}

if [ "${1:-}" = "--driver-only" ]; then driver_build; echo "driver reinstalled for $(uname -r)"; exit 0; fi

# Minutes-long restore from the repo's archived binaries (same kernel only)
if [ "${1:-}" = "--prebuilt" ]; then
  K=$(uname -r)
  [ -d "$REPO_DIR/prebuilt/$K" ] || { echo "no prebuilt modules for $K — full build required"; exit 1; }
  sudo install -D -m644 "$REPO_DIR/prebuilt/$K/morse.ko"   "/lib/modules/$K/updates/morse.ko"
  sudo install -D -m644 "$REPO_DIR/prebuilt/$K/dot11ah.ko" "/lib/modules/$K/updates/dot11ah.ko"
  sudo depmod -a
  sudo tar -C /usr/local/bin -xzf "$REPO_DIR/prebuilt/s1g-bins.tar.gz"
  echo "prebuilt modules + S1G binaries installed for $K"
  exit 0
fi

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  linux-headers-rpi-v8 build-essential git bison flex pkg-config \
  libssl-dev libnl-3-dev libnl-genl-3-dev libnl-route-3-dev \
  libusb-1.0-0-dev device-tree-compiler raspi-utils iputils-arping

# Kernel image and headers must be the same version (headers track the repo,
# a freshly flashed image may boot older) — reboot before rerunning if this trips.
HDR_VER=$(dpkg -l linux-headers-rpi-v8 | awk '/^ii/{print $3}')
if ! ls "/lib/modules/$(uname -r)/build" >/dev/null 2>&1; then
  echo "ERROR: no headers for running kernel $(uname -r) (headers pkg $HDR_VER)."
  echo "Run: sudo apt-get install -y linux-image-rpi-v8 && sudo reboot, then rerun."
  exit 1
fi

mkdir -p "$WORK"
fetch morse_driver  https://github.com/MorseMicro/morse_driver.git "$DRIVER_TAG" "morse_driver-*.tar.gz"
fetch morse-firmware https://github.com/MorseMicro/morse-firmware.git "$FW_BRANCH" "morse-firmware-*.tar.gz"
fetch morse_cli     https://github.com/MorseMicro/morse_cli.git    "$DRIVER_TAG" "morse_cli-*.tar.gz"
fetch hostap        https://github.com/MorseMicro/hostap.git       "$DRIVER_TAG" "hostap-*.tar.gz"

driver_build

# Chip firmware + Heltec board calibration file
sudo make -C "$WORK/morse-firmware" install
BCF_TAR=$(ls "$REPO_DIR"/vendor/morsemicro-bcf-rel_1_15_3.tar 2>/dev/null | head -1 || true)
if [ -n "$BCF_TAR" ]; then
  T=$(mktemp -d) && tar -xf "$BCF_TAR" -C "$T"
  sudo install -m644 "$(find "$T" -name bcf_mf08551.bin | head -1)" /lib/firmware/morse/
  rm -rf "$T"
fi
[ -f /lib/firmware/morse/bcf_mf08551.bin ] || { echo "ERROR: bcf_mf08551.bin missing"; exit 1; }

# CLI
make -C "$WORK/morse_cli" CONFIG_MORSE_TRANS_NL80211=1
sudo install -m755 "$WORK/morse_cli/morse_cli" /usr/local/bin/

# S1G wpa_supplicant / hostapd
for tool in wpa_supplicant hostapd; do
  cd "$WORK/hostap/$tool"
  [ -f .config ] || cp defconfig .config
  grep -q '^CONFIG_MESH=y' .config || echo CONFIG_MESH=y >> .config
  make -j"$(nproc)"
done
sudo install -m755 "$WORK/hostap/wpa_supplicant/wpa_supplicant" /usr/local/bin/wpa_supplicant_s1g
sudo install -m755 "$WORK/hostap/wpa_supplicant/wpa_cli"        /usr/local/bin/wpa_cli_s1g
sudo install -m755 "$WORK/hostap/hostapd/hostapd"               /usr/local/bin/hostapd_s1g
sudo install -m755 "$WORK/hostap/hostapd/hostapd_cli"           /usr/local/bin/hostapd_cli_s1g

# Device tree overlay + module options
dtc -@ -I dts -O dtb -o /tmp/mm610x-spi.dtbo "$REPO_DIR/overlays/mm610x-spi-overlay.dts"
sudo install -m644 /tmp/mm610x-spi.dtbo /boot/firmware/overlays/
sudo install -m644 "$REPO_DIR/config/morse.conf" /etc/modprobe.d/morse.conf
grep -q '^dtoverlay=mm610x-spi' /boot/firmware/config.txt || \
  echo 'dtoverlay=mm610x-spi' | sudo tee -a /boot/firmware/config.txt >/dev/null

echo "Install complete. Reboot, then run scripts/verify.sh"
