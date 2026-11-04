#!/bin/sh
# Runs at every boot as easysort-firstboot.service, before easysort-motion.
# Each step checks whether it is already done, so a boot without network
# simply tries again (systemd restarts this until it succeeds).
#
# 1. Moves the secrets flash.py left on the boot partition to /etc/easysort.
# 2. Installs the packages the motion service needs.
# 3. Installs Tailscale and joins with the one-time auth key, then deletes it.
set -eu

BOOT=/boot/firmware/easysort
ETC=/etc/easysort
PACKAGES="python3-opencv python3-numpy ffmpeg curl"

install -d -m 0750 -o root -g easysort "$ETC"

if [ -f "$BOOT/device.env" ]; then
  install -m 0640 -o root -g easysort "$BOOT/device.env" "$ETC/device.env"
  shred -u "$BOOT/device.env" 2>/dev/null || rm -f "$BOOT/device.env"
fi
if [ -f "$BOOT/tailscale-authkey" ]; then
  install -m 0600 -o root -g root "$BOOT/tailscale-authkey" "$ETC/tailscale-authkey"
  shred -u "$BOOT/tailscale-authkey" 2>/dev/null || rm -f "$BOOT/tailscale-authkey"
fi
if [ -f "$BOOT/settings.json" ]; then
  install -m 0644 "$BOOT/settings.json" "$ETC/settings.json"
fi

missing=""
for package in $PACKAGES; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed" || missing="$missing $package"
done
if [ -n "$missing" ]; then
  echo "Installing$missing"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing
fi

if [ -f "$ETC/tailscale-authkey" ]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  systemctl enable --now tailscaled
  HOSTNAME=$(python3 -c 'import json; print(json.load(open("/etc/easysort/settings.json"))["hostname"])')
  TAG=$(python3 -c 'import json; print(json.load(open("/etc/easysort/settings.json")).get("tag", ""))')
  if [ -n "$TAG" ]; then
    tailscale up --auth-key="file:$ETC/tailscale-authkey" --hostname="$HOSTNAME" --ssh --advertise-tags="$TAG"
  else
    tailscale up --auth-key="file:$ETC/tailscale-authkey" --hostname="$HOSTNAME" --ssh
  fi
  shred -u "$ETC/tailscale-authkey"
  echo "Joined Tailscale as $HOSTNAME"
fi
