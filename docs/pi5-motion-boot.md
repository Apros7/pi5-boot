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
org, device, enrol code        easysort-firstboot:                   cameras -> motion -> 15-min mp4
  (checked with the Worker)       secrets -> /etc/easysort             -> upload via Worker -> R2
Raspberry Pi OS Lite              apt: python3-opencv, ffmpeg
  + first-boot files              Tailscale join (--ssh), key deleted
                                  enrol -> device token, code deleted
```

1. `flash.py` asks for the Wi-Fi, the Tailscale auth key, the organization,
   a device name and the enrolment code, checks the code and that the name
   is free with the Worker, then writes stock 64-bit
   Raspberry Pi OS Lite (Trixie, 2026-10-06, checksum pinned) and the
   first-boot files.
2. On first boot cloud-init creates the `easysort` user (no password; reach
   it with Tailscale SSH or an `--ssh-key`), sets the hostname to
   `<organization>-<device>` and installs two systemd units.
3. `easysort-firstboot.service` moves the enrolment file and the Tailscale
   key off the FAT boot partition into `/etc/easysort/`, installs
   `python3-opencv python3-numpy ffmpeg`, installs Tailscale, joins with
   `--ssh` and deletes the key. Then `enroll.py` enrols the device with the
   Worker, saves its token in `/etc/easysort/device-token` (0600, owned by
   `easysort`) and the enrolment code is deleted. It runs at every boot and
   retries every 30 s until every step has succeeded, so a Pi that starts
   without network catches up. A failed step does not stop the others, so
   the Pi still joins Tailscale when enrolment is stuck.
4. `easysort-motion.service` runs `motion_capture.py` at every boot and
   restarts it if it stops. On first boot it waits quietly until
   `easysort-firstboot` has installed the packages and written
   `/etc/easysort/packages-ready`. It does not wait for Tailscale or
   enrolment: it records anyway, and windows queue until the token exists.

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
Authorization: Bearer <device token>
```

`upload.py` reads the token from `/etc/easysort/device-token`. The token is
`esd1.<org>.<device>.<random>`, so it also gives the path. Until the Pi has
enrolled, windows wait in the queue. The mp4 goes up before the JSON, 409
with the same
sha256 counts as done, and a queue of up to 8 GB waits on the Pi while it is
offline (oldest windows are deleted beyond that or below 2 GB free).

The Pi never holds an R2 key: R2 tokens cannot be limited to a prefix. Its
token only lets it write below its own `<org>/<device>`.

## Enrolment (admin)

The Pi enrols itself on first boot with a shared **enrolment code**, so
nobody needs an account made for each device:

```
POST https://worker.easysort.org/device-storage/v1/enroll        {"enroll_code","org","device"}
  201 {"token","org","device"}   the device's own token, returned once
  409 that device name is already enrolled (nobody can take it over)
  403 wrong code, 503 enrolment not set up
POST https://worker.easysort.org/device-storage/v1/enroll/check  same body
  200 {"ok":true,"available"}    checks the code and the name, enrols nothing
```

The Worker keeps one record per device with only a hash of its token.
Easysort sets the enrolment code(s) as a Worker secret and gives the code to
the people who set up devices. The code can be changed at any time; devices
that already enrolled keep working. An admin can revoke a device, which stops
its uploads at once, or free its name so a replacement Pi can enrol under it.

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
3. **Device**: organization, a name for this device (e.g. `yard-01`) and the
   enrolment code (hidden). The script checks the code and that the name is
   free, and stops if not. With no internet on the computer, `--no-check`
   skips this; the Pi then finds out on first boot.
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
systemctl status easysort-firstboot easysort-motion easysort-preview
journalctl -u easysort-firstboot -b      # "Enrolled as <org>/<device>", "Joined Tailscale as ..."
journalctl -u easysort-motion -f         # every 5 min: frames, motion, kept, clips
ls /var/lib/easysort-motion/queue         # windows waiting to upload
```

Uploaded windows are in R2 under `reuse-detection/<org>/<device>/`.

### Camera check page

Open `https://<org>-<device>.<tailnet>.ts.net/` from a device on the tailnet
(the tailnet name is in the Tailscale admin console under DNS). It shows each
camera's newest frame (taken every 5 s) and the last 5 motion frames, with
local and UTC time and the camera's name, plus the recording service's state
and uptime, the last upload result, the queue size, CPU temperature,
`vcgencmd get_throttled` in words and the network interface in use (`eth1`
is the 4G dongle).

It is built for 4G data: no auto refresh, no scripts, no video; reload to
look again. The page is a few KB and never cached. Images are at most 960 px
wide at JPEG quality 70 (about 100 KB) and cached by the browser, so a reload
only fetches the new ones.

- `motion_capture.py` writes the images to `/run/easysort-motion` (RAM, no
  SD-card writes). Errors there are logged once and never stop recording.
- `easysort-preview.service` (`preview.py`, standard library only) is
  read-only, runs as `easysort` and listens on `127.0.0.1:8080` only.
  Recording does not depend on it.
- `firstboot.sh` shares it on the tailnet with
  `tailscale serve --bg --https=443 http://127.0.0.1:8080` at every boot, so
  it is never reachable from the LAN, Wi-Fi or 4G side. This needs HTTPS
  certificates turned on for the tailnet (they are for Easysort's) and an
  ACL that lets you reach the device on port 443. If serve is not set up:
  `tailscale ssh -L 8080:127.0.0.1:8080 easysort@<org>-<device>` (or plain
  `ssh -L`), then open `http://127.0.0.1:8080/`.

## Where secrets are

| Secret | Typed at | On the card | On the Pi after first boot |
|---|---|---|---|
| Wi-Fi password | flash.py (or read from this computer) | `network-config` | NetworkManager, and `network-config` stays on the boot partition |
| Tailscale auth key | flash.py | `easysort/tailscale-authkey` until first boot | deleted after joining |
| Enrolment code | flash.py | `easysort/enroll.json` until first boot | `/etc/easysort/enroll.json` (0600 root) until enrolled, then deleted |
| Device token | (made by the Worker) | never | `/etc/easysort/device-token` (0600 easysort) |

## Not done yet

- Tested on a real Pi 5 with a USB camera.
- No automatic code updates: a new version means a new card, or copying the
  files to `/opt/easysort-motion` and restarting the service.
- USB cameras only. Raspberry Pi camera modules (CSI) would need picamera2.
- 4G: NetworkManager should bring up a USB 4G dongle by itself, but it is
  not checked here.
