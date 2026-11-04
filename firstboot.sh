#!/bin/sh
# Runs at every boot as easysort-firstboot.service, before easysort-motion.
# Each step checks whether it is already done, and a failed step does not
# stop the others (Tailscale should come up even if enrolment is stuck, so
# the Pi can be reached). If any step failed the script exits 1 and systemd
# runs it again in 30 s.
#
# 1. Moves the files flash.py left on the boot partition to /etc/easysort.
# 2. Installs the packages the motion service needs.
# 3. Installs Tailscale and joins with the one-time auth key, then deletes it.
# 4. Enrols with the Worker, saves the device token, deletes the enrolment code.
set -u

BOOT=/boot/firmware/easysort
ETC=/etc/easysort
APP=/opt/easysort-motion
PACKAGES="python3-opencv python3-numpy ffmpeg curl"
failed=0

move_secret() {  # boot file, target, mode, owner:group
  if [ -f "$BOOT/$1" ]; then
    install -m "$3" -o "${4%:*}" -g "${4#*:}" "$BOOT/$1" "$2" &&
      { shred -u "$BOOT/$1" 2>/dev/null || rm -f "$BOOT/$1"; }
  fi
}

install -d -m 0750 -o root -g easysort "$ETC"
move_secret enroll.json "$ETC/enroll.json" 0600 root:root || failed=1
move_secret tailscale-authkey "$ETC/tailscale-authkey" 0600 root:root || failed=1
if [ -f "$BOOT/settings.json" ]; then
  install -m 0644 "$BOOT/settings.json" "$ETC/settings.json" || failed=1
fi

missing=""
for package in $PACKAGES; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed" || missing="$missing $package"
done
if [ -n "$missing" ]; then
  echo "Installing$missing"
  { apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing; } || failed=1
fi

if [ -f "$ETC/tailscale-authkey" ]; then
  joined=0
  if command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh; then
    systemctl enable --now tailscaled
    HOSTNAME=$(python3 -c 'import json; print(json.load(open("/etc/easysort/settings.json"))["hostname"])')
    TAG=$(python3 -c 'import json; print(json.load(open("/etc/easysort/settings.json")).get("tag", ""))')
    if [ -n "$TAG" ]; then
      tailscale up --auth-key="file:$ETC/tailscale-authkey" --hostname="$HOSTNAME" --ssh --advertise-tags="$TAG" && joined=1
    else
      tailscale up --auth-key="file:$ETC/tailscale-authkey" --hostname="$HOSTNAME" --ssh && joined=1
    fi
  fi
  if [ "$joined" = 1 ]; then
    shred -u "$ETC/tailscale-authkey"
    echo "Joined Tailscale as $HOSTNAME"
  else
    failed=1
  fi
fi

if [ ! -s "$ETC/device-token" ]; then
  python3 "$APP/enroll.py" --config "$ETC/enroll.json" --token "$ETC/device-token" || failed=1
fi
if [ -s "$ETC/device-token" ] && [ -f "$ETC/enroll.json" ]; then
  # The enrolment code is shared by every device: do not keep it.
  shred -u "$ETC/enroll.json"
fi

exit "$failed"
