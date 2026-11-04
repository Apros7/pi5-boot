# Raspberry Pi 5 motion recorder

A micro-SD card for a Raspberry Pi 5 with one or more USB cameras. At every
power-on the Pi records motion and every 15 minutes uploads one mp4 (plus a
JSON list of frame times) to Easysort's storage.

Full description, admin setup and troubleshooting:
[`docs/pi5-motion-boot.md`](docs/pi5-motion-boot.md).

## Make a card (on site)

You need: a Mac or Linux computer on the site's Wi-Fi, a micro-SD card
(8-256 GB), a Tailscale auth key and the device's organization, device name
and password (from the Easysort admin who set up the device).

```bash
git clone https://github.com/Apros7/pi5-boot.git   # or: cd pi5-boot && git pull
cd pi5-boot
python3 flash.py
```

The script asks for the Wi-Fi (it suggests the one this computer uses), the
Tailscale key, the device login and the card, then writes the card. Put the
card in the Pi, plug in the camera(s), then power. The first start takes
about 10 minutes; after that the Pi records whenever it has power.

## Files

| File | Runs on | What it does |
|---|---|---|
| `flash.py` | your computer | Writes Raspberry Pi OS Lite and the first-boot setup to the card. |
| `firstboot.sh` | Pi, every boot | Moves the secrets off the boot partition, installs packages, joins Tailscale. |
| `motion_capture.py` | Pi, as `easysort-motion.service` | Cameras, motion check, 15-minute windows. |
| `windows.py` | Pi | Fuses each window's kept frames into one mp4 and a JSON sidecar. |
| `upload.py` | Pi | Uploads queued windows to R2 through the device-storage Worker. |

## Tests

From the repository root:

```bash
python3 -m pip install opencv-python-headless numpy   # once
python3 -m unittest discover -p 'test_*.py'
```

The window tests also need `ffmpeg` and `ffprobe` on the PATH and are skipped
without them. No camera, card or network is used.
