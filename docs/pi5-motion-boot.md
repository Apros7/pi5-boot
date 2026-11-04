# Raspberry Pi 5 motion recorder

Status: written 2026-11-04, tested off-device (unit tests, a dry-run card and
a capture-loop run with fake cameras). Not yet tested on a Pi 5.

A micro-SD card that turns a Raspberry Pi 5 with USB cameras into a motion
recorder. Someone on site runs one script with the card in their computer;
the Pi then records whenever it has power and uploads one mp4 per 15 minutes
to Easysort's R2 bucket (`reuse-detection`).

The code is in the root of this repository. It only uses the Python standard
library on the computer and Debian packages on the Pi, and holds no secrets.

## What happens

```
flash.py (Mac/Linux)            Pi 5, first boot                    Pi 5, every boot
--------------------            ----------------                    ----------------
Wi-Fi from this computer  ->    cloud-init: user, hostname,         easysort-firstboot (no-op once done)
Tailscale auth key              copies app to /opt/easysort-motion  easysort-motion.service:
device login (checked)          easysort-firstboot:                   cameras -> motion -> 15-min mp4
Raspberry Pi OS Lite              secrets -> /etc/easysort             -> upload via Worker -> R2
  + first-boot files              apt: python3-opencv, ffmpeg
                                  Tailscale join (--ssh), key deleted
```

1. `flash.py` asks for the Wi-Fi, the Tailscale auth key and the device
   login, checks the login against Supabase, then writes stock 64-bit
   Raspberry Pi OS Lite (Trixie, 2026-10-06, checksum pinned) and the
   first-boot files.
2. On first boot cloud-init creates the `easysort` user (no password; reach
   it with Tailscale SSH or an `--ssh-key`), sets the hostname to
   `<organization>-<device>` and installs two systemd units.
3. `easysort-firstboot.service` moves the device login and the Tailscale key
   off the FAT boot partition into `/etc/easysort/`, installs
   `python3-opencv python3-numpy ffmpeg`, installs Tailscale, joins with
   `--ssh` and deletes the key. It runs at every boot and retries every 30 s
   until it succeeds, so a Pi that starts without network catches up.
4. `easysort-motion.service` runs `motion_capture.py` at every boot and
   restarts it if it stops.

## Recording

- Every USB camera in `/dev/v4l/by-id/` is used (falls back to
  `/dev/v4l/by-path/*usb*`), as MJPG at its largest size, like the fleet's
  `capture.py`.
- Every 0.25 s (4 fps) the newest frame from each camera is taken. With more
  than one camera the frames are put side by side, scaled to a common height
  of at most 1080 px, so the device still uploads one mp4 per window. A
  camera that drops out shows black until it is back.
- Motion: grey-level difference against the previous frame on a 64 px wide,
  blurred copy; a frame has motion when at least 1 % of pixels changed by more
  than 25 grey levels. Motion
  in any camera keeps the frame. Frames from 2 s before motion to 2 s after it
  are kept; everything else is dropped.
- The kept frames of each UTC quarter hour are fused into one mp4 (4 fps
  playback, libx264 crf 28) plus a JSON sidecar with the real UTC time of
  each frame, whether it had motion, the clips and the camera layout. A
  quarter hour without motion uploads nothing (`windows.py`). A restart or
  power cut inside a quarter hour keeps what was recorded so far.

Tuning flags (`--pixel-delta`, `--min-fraction`, `--pre-s`, `--post-s`,
`--camera`) can be added to `ExecStart` in
`/etc/systemd/system/easysort-motion.service`.

## Upload and storage

Through Easysort's device-storage Worker:

```
PUT https://worker.easysort.org/device-storage/v1/reuse-detection/<org>/<device>/<YYYY-MM-DD>/<HHMM>.mp4|json
Authorization: Bearer <device's Supabase access token>
```

`upload.py` reads the login from `/etc/easysort/device.env`. The mp4 goes up
before the JSON, 409 with the same
sha256 counts as done, and a queue of up to 8 GB waits on the Pi while it is
offline (oldest windows are deleted beyond that or below 2 GB free).

The Pi never holds an R2 key: R2 tokens cannot be limited to a prefix. It
logs in as its own Supabase device user, and the Worker only accepts uploads
under that user's `<org>/<device>`.

## Making a device login (admin)

The person flashing needs the organization, device name and device password.
An Easysort admin makes the login once per device with Easysort's internal
provisioning tool (`provision.py create <organization> <device-name>`) and
sends the password to the person flashing through a password manager, not
chat. The email `recycling-<org>-<device>@devices.invalid` is derived by
`flash.py`, so it is not needed. The same tool revokes a lost device.

Also make a Tailscale auth key (admin console, Settings, Keys): reusable or
one-off, pre-approved, and tagged if your ACL uses tags (then pass `--tag`).
Turn off key expiry on the node after it joins.

## Making a card (on site)

```bash
git clone https://github.com/Apros7/pi5-boot.git
cd pi5-boot
python3 flash.py
```

1. **Wi-Fi.** The script lists the network this computer is on (Linux,
   NetworkManager) or this Mac's saved networks (macOS hides the current
   network name from scripts; the most recent is listed first). Press Enter
   for the first or pick one. It then reads the saved password (macOS asks
   for permission to read it from the Keychain) and shows only its first and
   last character to confirm. The password can also be typed.
2. **Tailscale auth key**, at a hidden prompt.
3. **Device login**: organization, device name, password (hidden). The
   script signs in once to check it and stops if it is wrong.
4. **Card.** Cards (removable, 8-256 GB) are listed; the system disk is never
   offered. Type the disk name to confirm erasing it. `sudo` asks for the
   computer's password to write the card.

Then: card in the Pi, cameras in, power in. The first boot takes about
10 minutes. The Pi shows up in Tailscale as `<organization>-<device>`.

`python3 flash.py --dry-run DIR` writes the card files to a
folder without touching a disk or asking anything.

## Checking a device

```bash
tailscale ssh easysort@<org>-<device>
systemctl status easysort-firstboot easysort-motion
journalctl -u easysort-motion -f         # every 5 min: frames, motion, kept, clips
ls /var/lib/easysort-motion/queue         # windows waiting to upload
```

Uploaded windows are in R2 under `reuse-detection/<org>/<device>/`.

## Where secrets are

| Secret | Typed at | On the card | On the Pi after first boot |
|---|---|---|---|
| Wi-Fi password | flash.py (or read from this computer) | `network-config` | NetworkManager, and `network-config` stays on the boot partition |
| Tailscale auth key | flash.py | `easysort/tailscale-authkey` until first boot | deleted after joining |
| Device password | flash.py | `easysort/device.env` until first boot | `/etc/easysort/device.env` (0640 root:easysort) |

The Supabase URL and publishable key in `flash.py` are public client values;
access depends on the device login.

## Not done yet

- Tested on a real Pi 5 with a USB camera.
- No automatic code updates: a new version means a new card, or copying the
  files to `/opt/easysort-motion` and restarting the service.
- USB cameras only. Raspberry Pi camera modules (CSI) would need picamera2.
- 4G: NetworkManager should bring up a USB 4G dongle by itself, but it is
  not checked here.
