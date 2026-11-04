#!/usr/bin/env python3
"""Write a micro-SD card for a Raspberry Pi 5 that records motion and uploads it.

Run on a Mac or a Linux PC with the card inserted:

    python3 flash.py

The script asks for, in order:

1. The Wi-Fi the Pi should use. It reads the network this computer is on and
   its saved password, and asks you to confirm.
2. The Tailscale auth key.
3. The organization, a name for this device and the enrolment code (from
   an Easysort admin). The code and the name are checked with the Worker
   before anything is written.
4. Which card to erase.

The card gets stock 64-bit Raspberry Pi OS Lite plus a first-boot config. On
first boot the Pi installs its packages, joins Tailscale as
``<organization>-<device>``, enrols itself with the Worker (which gives it
its own upload token) and starts easysort-motion.service, which runs at
every boot from then on. No R2 key is ever on the card.

Secrets are asked for at hidden prompts and written only to the card, never
to this repository or the screen. The Pi moves them off the boot partition on
first boot and deletes the enrolment code once it has its token. Nothing but the Python standard library is needed.

``--dry-run DIR`` writes the boot files into DIR with placeholders, without
touching a disk, the network or asking for secrets.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import lzma
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
IMAGE_NAME = "2026-10-06-raspios-trixie-arm64-lite.img.xz"
IMAGE_URL = (
    "https://downloads.raspberrypi.com/raspios_lite_arm64/images/"
    f"raspios_lite_arm64-2026-10-06/{IMAGE_NAME}"
)
IMAGE_SHA256 = "483db18a48da399b5b7022ffae9a07bc6d99daab30e593aedac09b69ff422843"
LINUX = sys.platform.startswith("linux")
CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "easysort" / "raspios"
    if LINUX
    else Path.home() / "Library" / "Caches" / "easysort" / "raspios"
)

WORKER_BASE = "https://worker.easysort.org/device-storage/v1"
# Cloudflare's bot check answers the default Python-urllib agent with 403 1010.
USER_AGENT = "easysort-edge/1"

USER = "easysort"
APP_DIR = "/opt/easysort-motion"
BOOT_DIR = "easysort"  # on the boot partition, /boot/firmware/easysort on the Pi
WIFI_COUNTRY = "DK"
APP_FILES = ("motion_capture.py", "windows.py", "upload.py", "enroll.py", "firstboot.sh", "easysort-motion.service",
             "easysort-firstboot.service", "preview.py", "easysort-preview.service", "README.md")

MIN_CARD_BYTES = 8 * 1000**3
MAX_CARD_BYTES = 256 * 1000**3
DISK_RE = re.compile(r"^disk[0-9]+$")
LINUX_DISK_RE = re.compile(r"^(sd[a-z]+|mmcblk[0-9]+)$")
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[SWAP]"}
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
AUTH_KEY_RE = re.compile(r"tskey-[A-Za-z0-9_-]+")
SSH_KEY_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]+( \S+)?$")
REDACTED = {"", "<redacted>"}

Runner = Callable[..., subprocess.CompletedProcess]


class FlashError(RuntimeError):
    pass


def mask(text: str) -> str:
    return AUTH_KEY_RE.sub("tskey-…", text)


def _yaml(value: str) -> str:
    """A JSON string is a valid double-quoted YAML scalar."""
    return json.dumps(value, ensure_ascii=False)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "".join(f"{pad}{line}\n" if line else "\n" for line in text.splitlines())


# --- Device and enrolment -------------------------------------------------------


@dataclass(frozen=True)
class Enrolment:
    organization: str
    device: str
    code: str

    @property
    def hostname(self) -> str:
        return f"{self.organization}-{self.device}"


def check_name(value: str, what: str) -> str:
    value = value.strip().lower()
    if not NAME_RE.fullmatch(value):
        raise FlashError(f"{what} must be lowercase letters, digits and hyphens: {value!r}")
    return value


def check_enrolment(enrolment: Enrolment, http: Callable = urllib.request.urlopen) -> None:
    """Ask the Worker whether the code is right and the name is free, without enrolling."""
    body = json.dumps(
        {"enroll_code": enrolment.code, "org": enrolment.organization, "device": enrolment.device}
    ).encode()
    request = urllib.request.Request(
        f"{WORKER_BASE}/enroll/check",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    name = f"{enrolment.organization}/{enrolment.device}"
    try:
        with http(request, timeout=30) as reply:
            answer = json.loads(reply.read() or b"{}")
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise FlashError("The Worker refused the enrolment code. Check it with the admin.") from error
        if error.code == 400:
            raise FlashError(f"The Worker refused the name {name}") from error
        if error.code == 503:
            raise FlashError("Enrolment is not set up on the Worker yet (no ENROLL_CODE). Ask the admin.") from error
        raise FlashError(f"The Worker answered {error.code} when checking the enrolment code") from error
    except (urllib.error.URLError, OSError) as error:
        raise FlashError(
            f"Could not reach the Worker to check the enrolment code: {error}. "
            "Run again with --no-check to write the card anyway."
        ) from error
    if not answer.get("available"):
        raise FlashError(
            f"{name} is already enrolled. Choose another device name, or ask the admin to free it "
            "if this Pi replaces the old one."
        )


def render_enroll(enrolment: Enrolment) -> str:
    return json.dumps(
        {"enroll_code": enrolment.code, "org": enrolment.organization, "device": enrolment.device}, indent=2
    ) + "\n"


# --- Wi-Fi of this computer ------------------------------------------------------


def _nmcli_field(line: str) -> tuple[str, str]:
    """Split one terse nmcli line into its first field and the unescaped rest."""
    head, _, rest = line.partition(":")
    return head, rest.replace("\\:", ":").replace("\\\\", "\\")


def linux_wifi(run: Runner = subprocess.run) -> tuple[list[str], Callable[[str], str | None]]:
    """(SSIDs, newest first with the connected one first, and a password lookup) from NetworkManager."""
    def nmcli(*args: str) -> str:
        try:
            result = run(["nmcli", *args], capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return ""
        return result.stdout if result.returncode == 0 else ""

    active = [_nmcli_field(line) for line in nmcli("-t", "-f", "TYPE,NAME", "connection", "show", "--active").splitlines()]
    names = [name for kind, name in active if kind == "802-11-wireless"]
    ssids = []
    for name in names:
        values = nmcli("--escape", "no", "-g", "802-11-wireless.ssid", "connection", "show", name).splitlines()
        if values and values[0]:
            ssids.append(values[0])

    def password(ssid: str) -> str | None:
        for name in names or [ssid]:
            values = nmcli("--escape", "no", "-s", "-g", "802-11-wireless.ssid,802-11-wireless-security.psk",
                           "connection", "show", name).splitlines()
            if len(values) >= 2 and values[0] == ssid and values[1]:
                return values[1]
        return None

    return ssids, password


def mac_wifi_interface(run: Runner = subprocess.run) -> str | None:
    result = run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, check=False)
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if line.strip() in ("Hardware Port: Wi-Fi", "Hardware Port: AirPort") and index + 1 < len(lines):
            device = lines[index + 1].partition(":")[2].strip()
            if device:
                return device
    return None


def mac_wifi(run: Runner = subprocess.run) -> tuple[list[str], Callable[[str], str | None]]:
    """(SSIDs, the current one first if macOS shows it, then the saved ones) and a Keychain lookup.

    Recent macOS hides the current SSID from scripts, so the saved networks
    are offered and the person picks.
    """
    interface = mac_wifi_interface(run) or "en0"
    current = ""
    summary = run(["ipconfig", "getsummary", interface], capture_output=True, text=True, check=False).stdout
    for line in summary.splitlines():
        key, _, value = line.strip().partition(" : ")
        if key == "SSID" and value.strip() not in REDACTED:
            current = value.strip()
    preferred = run(
        ["networksetup", "-listpreferredwirelessnetworks", interface], capture_output=True, text=True, check=False
    ).stdout.splitlines()[1:]
    saved = [line.strip() for line in preferred if line.strip()]
    ssids = list(dict.fromkeys([current, *saved] if current else saved))

    def password(ssid: str) -> str | None:
        # macOS asks for permission (and your Mac login) before it hands it over.
        result = run(
            ["security", "find-generic-password", "-D", "AirPort network password", "-a", ssid, "-w"],
            capture_output=True, text=True, check=False,
        )
        value = result.stdout.rstrip("\n")
        return value if result.returncode == 0 and value else None

    return ssids, password


def choose_wifi(
    ssids: list[str],
    lookup: Callable[[str], str | None],
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
) -> tuple[str, str]:
    """Let the person confirm or pick the network, then fetch or ask its password."""
    print("\nWi-Fi for the Pi (it should be the network at this site).")
    shown = ssids[:9]
    if shown:
        for number, ssid in enumerate(shown, 1):
            print(f"  {number}. {ssid}")
        answer = ask(f"Which network? Press Enter for 1 ({shown[0]}), a number, or type a name: ").strip()
        if not answer:
            ssid = shown[0]
        elif answer.isdigit() and 1 <= int(answer) <= len(shown):
            ssid = shown[int(answer) - 1]
        else:
            ssid = answer
    else:
        ssid = ask("Wi-Fi network name (SSID): ").strip()
    if not ssid:
        raise FlashError("No Wi-Fi network given")
    password = lookup(ssid) if ssid in ssids else None
    if password:
        hint = f"{password[0]}{'•' * max(0, len(password) - 2)}{password[-1]}" if len(password) > 2 else "••"
        answer = ask(f"Found the saved password for {ssid} ({hint}, {len(password)} characters). Use it? [Y/n] ")
        if answer.strip().lower() in ("n", "no", "nej"):
            password = None
    if not password:
        password = ask_secret(f"Wi-Fi password for {ssid} (hidden): ")
    if not 8 <= len(password) <= 64:
        raise FlashError("A WPA Wi-Fi password is 8 to 64 characters")
    return ssid, password


# --- Card contents -------------------------------------------------------------


def render_user_data(hostname: str, ssh_public_keys: list[str]) -> str:
    keys = "".join(f"      - {_yaml(key)}\n" for key in ssh_public_keys)
    key_block = f"    ssh_authorized_keys:\n{keys}" if ssh_public_keys else ""
    return f"""#cloud-config
# Written by flash.py. No secrets in this file: they are in
# /boot/firmware/{BOOT_DIR}/ until first boot moves them to /etc/easysort.
hostname: {hostname}
manage_etc_hosts: true
timezone: Europe/Copenhagen
keyboard:
  model: pc105
  layout: dk
ssh_pwauth: false
users:
  - name: {USER}
    groups: [users, adm, video, plugdev, render, sudo]
    shell: /bin/bash
    lock_passwd: true
    sudo: "ALL=(ALL) NOPASSWD:ALL"
{key_block}runcmd:
  - [sh, -c, "install -d -m 0755 {APP_DIR} && cp /boot/firmware/{BOOT_DIR}/app/* {APP_DIR}/"]
  - [install, -m, "0755", {APP_DIR}/firstboot.sh, /usr/local/sbin/easysort-firstboot]
  - [install, -m, "0644", {APP_DIR}/easysort-firstboot.service, /etc/systemd/system/]
  - [install, -m, "0644", {APP_DIR}/easysort-motion.service, /etc/systemd/system/]
  - [install, -m, "0644", {APP_DIR}/easysort-preview.service, /etc/systemd/system/]
  - [systemctl, daemon-reload]
  - [systemctl, enable, easysort-firstboot.service, easysort-motion.service, easysort-preview.service]
  - [systemctl, start, --no-block, easysort-firstboot.service, easysort-motion.service, easysort-preview.service]
"""


def render_network_config(ssid: str, password: str) -> str:
    return f"""network:
  version: 2
  renderer: NetworkManager
  ethernets:
    eth0:
      dhcp4: true
      optional: true
  wifis:
    wlan0:
      dhcp4: true
      optional: true
      regulatory-domain: {_yaml(WIFI_COUNTRY)}
      access-points:
        {_yaml(ssid)}:
          password: {_yaml(password)}
"""


def render_meta_data(hostname: str, instance_suffix: str) -> str:
    return f"instance-id: easysort-{hostname}-{instance_suffix}\nlocal-hostname: {hostname}\n"


def card_files(
    enrolment: Enrolment, auth_key: str, ssid: str, wifi_password: str, ssh_keys: list[str], suffix: str,
    tag: str | None,
) -> dict[str, str]:
    """Boot-partition path -> text."""
    settings = {"hostname": enrolment.hostname}
    if tag:
        settings["tag"] = tag
    return {
        "user-data": render_user_data(enrolment.hostname, ssh_keys),
        "network-config": render_network_config(ssid, wifi_password),
        "meta-data": render_meta_data(enrolment.hostname, suffix),
        f"{BOOT_DIR}/enroll.json": render_enroll(enrolment),
        f"{BOOT_DIR}/tailscale-authkey": auth_key + "\n",
        f"{BOOT_DIR}/settings.json": json.dumps(settings, indent=2) + "\n",
    }


def write_card(boot: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = boot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass  # FAT: permissions come from the mount
    app = boot / BOOT_DIR / "app"
    app.mkdir(parents=True, exist_ok=True)
    for name in APP_FILES:
        shutil.copyfile(HERE / name, app / name)


def check_public_key(key: str, source: str) -> str:
    key = key.strip()
    if "PRIVATE KEY" in key:
        raise FlashError(f"{source} looks like a private key")
    if "\n" in key or not SSH_KEY_RE.match(key):
        raise FlashError(f"{source} must be one OpenSSH public key line")
    return key


# --- Prompts -------------------------------------------------------------------


def ask_auth_key(prompt: Callable[[str], str] = getpass.getpass) -> str:
    key = prompt("\nPaste the Tailscale auth key (hidden): ").strip()
    if not re.fullmatch(r"tskey-auth-[A-Za-z0-9_-]+", key):
        raise FlashError("That is not a Tailscale auth key (it starts with tskey-auth-)")
    return key


def ask_enrolment(
    organization: str | None,
    device: str | None,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
) -> Enrolment:
    print("\nDevice name and enrolment code (the code is from an Easysort admin).")
    organization = check_name(organization or ask("Organization (e.g. acme): "), "organization")
    device = check_name(device or ask("Name for this device (e.g. yard-01): "), "device name")
    if len(f"{organization}-{device}") > 63:
        raise FlashError("organization and device give a hostname over 63 characters")
    code = ask_secret("Enrolment code (hidden): ").strip()
    if not code:
        raise FlashError("No enrolment code given")
    return Enrolment(organization, device, code)


# --- Disk ------------------------------------------------------------------------


def disk_info(disk: str, run: Runner = subprocess.run) -> dict:
    result = run(["diskutil", "info", "-plist", f"/dev/{disk}"], capture_output=True, check=False)
    if result.returncode != 0:
        raise FlashError(f"diskutil does not know /dev/{disk}")
    return plistlib.loads(result.stdout)


def check_card(disk: str, info: dict) -> str:
    """Refuse anything that is not a whole, external, physical, card-sized disk."""
    if not DISK_RE.match(disk):
        raise FlashError("Give a whole disk such as disk4, not a partition")
    if info.get("DeviceIdentifier") != disk or not info.get("WholeDisk"):
        raise FlashError(f"/dev/{disk} is not a whole disk")
    if info.get("Internal") or info.get("OSInternalMedia"):
        raise FlashError(f"/dev/{disk} is an internal disk")
    if info.get("VirtualOrPhysical") not in (None, "Physical"):
        raise FlashError(f"/dev/{disk} is not a physical disk")
    if not (info.get("RemovableMediaOrExternalDevice") or info.get("Removable")):
        raise FlashError(f"/dev/{disk} is not removable or external")
    size = int(info.get("TotalSize") or info.get("Size") or 0)
    if not MIN_CARD_BYTES <= size <= MAX_CARD_BYTES:
        raise FlashError(f"/dev/{disk} is {size / 1000**3:.1f} GB; expected an 8-256 GB card")
    name = info.get("MediaName") or info.get("IORegistryEntryName") or "unknown"
    return f"/dev/{disk}: {name}, {size / 1000**3:.1f} GB"


def mac_cards(run: Runner = subprocess.run) -> list[tuple[str, str]]:
    """(disk, summary) for every disk that passes check_card."""
    result = run(["diskutil", "list", "-plist", "external", "physical"], capture_output=True, check=False)
    if result.returncode != 0:
        return []
    cards = []
    for disk in plistlib.loads(result.stdout).get("WholeDisks", []):
        try:
            cards.append((disk, check_card(disk, disk_info(disk, run))))
        except FlashError:
            continue
    return cards


def cached_image() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / IMAGE_NAME
    if not path.is_file():
        partial = path.with_suffix(".partial")
        print(f"Downloading {IMAGE_URL}")
        with urllib.request.urlopen(IMAGE_URL, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
        partial.replace(path)
    digest = hashlib.sha256()
    with path.open("rb") as image:
        for chunk in iter(lambda: image.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != IMAGE_SHA256:
        path.unlink()
        raise FlashError("Image checksum mismatch; the cached copy was removed")
    return path


def write_image(disk: str, image: Path) -> None:
    subprocess.run(["diskutil", "unmountDisk", f"/dev/{disk}"], check=True)
    print(f"Writing {image.name} to /dev/r{disk} (sudo may ask for your Mac password)")
    dd = subprocess.Popen(["sudo", "dd", f"of=/dev/r{disk}", "bs=4m"], stdin=subprocess.PIPE)
    assert dd.stdin is not None
    try:
        with lzma.open(image, "rb") as source:
            shutil.copyfileobj(source, dd.stdin, 4 * 1024 * 1024)
    finally:
        dd.stdin.close()
    if dd.wait() != 0:
        raise FlashError(f"dd failed with exit code {dd.returncode}")
    subprocess.run(["sync"], check=True)


def mount_boot_partition(disk: str) -> Path:
    subprocess.run(["diskutil", "mountDisk", f"/dev/{disk}"], check=False)
    info = disk_info(f"{disk}s1")
    mount = info.get("MountPoint") or ""
    if not mount or not Path(mount, "config.txt").is_file():
        raise FlashError(f"Could not find the boot partition on /dev/{disk}")
    return Path(mount)


def _flag(value) -> bool:
    return value in (True, 1, "1", "true")


def linux_disk_info(disk: str, run: Runner = subprocess.run) -> dict:
    result = run(
        ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,RM,HOTPLUG,TRAN,MODEL,RO,MOUNTPOINTS", f"/dev/{disk}"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(result.stdout)["blockdevices"][0]
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        raise FlashError(f"lsblk does not know /dev/{disk}") from exc


def linux_mounts(info: dict) -> list[tuple[str, str]]:
    """(device path, mount point) for the disk and its partitions."""
    found = []
    for node in [info, *info.get("children", [])]:
        for point in node.get("mountpoints") or []:
            if point:
                found.append((node["path"], point))
    return found


def check_linux_card(disk: str, info: dict) -> str:
    """Refuse anything that is not a whole, removable, card-sized disk."""
    if not LINUX_DISK_RE.match(disk):
        raise FlashError("Give a whole card disk such as sdb or mmcblk0, not a partition")
    if info.get("name") != disk or info.get("type") != "disk":
        raise FlashError(f"/dev/{disk} is not a whole disk")
    if _flag(info.get("ro")):
        raise FlashError(f"/dev/{disk} is read-only; check the card's lock switch")
    removable = (
        _flag(info.get("rm")) or _flag(info.get("hotplug"))
        or info.get("tran") in ("usb", "mmc") or disk.startswith("mmcblk")
    )
    if not removable:
        raise FlashError(f"/dev/{disk} is not a removable or USB disk")
    system = [point for _, point in linux_mounts(info) if point in SYSTEM_MOUNTS]
    if system:
        raise FlashError(f"/dev/{disk} holds this computer's {', '.join(system)}")
    size = int(info.get("size") or 0)
    if not MIN_CARD_BYTES <= size <= MAX_CARD_BYTES:
        raise FlashError(f"/dev/{disk} is {size / 1000**3:.1f} GB; expected an 8-256 GB card")
    name = (info.get("model") or "card").strip()
    return f"/dev/{disk}: {name}, {size / 1000**3:.1f} GB"


def linux_cards(run: Runner = subprocess.run) -> list[tuple[str, str]]:
    result = run(["lsblk", "-J", "-d", "-o", "NAME"], capture_output=True, text=True, check=False)
    try:
        names = [node["name"] for node in json.loads(result.stdout)["blockdevices"]]
    except (json.JSONDecodeError, KeyError):
        return []
    cards = []
    for name in names:
        try:
            cards.append((name, check_linux_card(name, linux_disk_info(name, run))))
        except FlashError:
            continue
    return cards


def boot_partition(disk: str) -> str:
    return f"/dev/{disk}p1" if disk.startswith("mmcblk") else f"/dev/{disk}1"


def linux_write_image(disk: str, image: Path, info: dict) -> None:
    for device, _ in linux_mounts(info):
        subprocess.run(["sudo", "umount", device], check=True)
    print(f"Writing {image.name} to /dev/{disk} (sudo may ask for your password)")
    dd = subprocess.Popen(
        ["sudo", "dd", f"of=/dev/{disk}", "bs=4M", "conv=fsync", "status=progress"],
        stdin=subprocess.PIPE,
    )
    assert dd.stdin is not None
    try:
        with lzma.open(image, "rb") as source:
            shutil.copyfileobj(source, dd.stdin, 4 * 1024 * 1024)
    finally:
        dd.stdin.close()
    if dd.wait() != 0:
        raise FlashError(f"dd failed with exit code {dd.returncode}")
    subprocess.run(["sync"], check=True)
    subprocess.run(["sudo", "blockdev", "--rereadpt", f"/dev/{disk}"], check=False)


def linux_mount_boot_partition(disk: str) -> Path:
    partition = boot_partition(disk)
    for _ in range(20):
        if Path(partition).exists():
            break
        time.sleep(0.5)
    else:
        raise FlashError(f"{partition} did not appear after writing; remove and reinsert the card")
    mount = Path(tempfile.mkdtemp(prefix="pi5-card-"))
    subprocess.run(
        ["sudo", "mount", "-t", "vfat", "-o", f"uid={os.getuid()},gid={os.getgid()},umask=077",
         partition, str(mount)],
        check=True,
    )
    if not (mount / "config.txt").is_file():
        linux_unmount(mount)
        raise FlashError(f"{partition} is not the Raspberry Pi OS boot partition")
    return mount


def linux_unmount(mount: Path) -> None:
    subprocess.run(["sync"], check=True)
    subprocess.run(["sudo", "umount", str(mount)], check=True)
    mount.rmdir()


def linux_eject(disk: str, run: Runner = subprocess.run) -> bool:
    """Unmount whatever the desktop auto-mounted from the new card and power it off."""
    run(["sync"], check=False)
    for device, _ in linux_mounts(linux_disk_info(disk, run)):
        if run(["udisksctl", "unmount", "-b", device], capture_output=True, check=False).returncode != 0:
            run(["sudo", "umount", device], check=False)
    for cmd in (["udisksctl", "power-off", "-b", f"/dev/{disk}"], ["sudo", "eject", f"/dev/{disk}"]):
        try:
            if run(cmd, capture_output=True, check=False).returncode == 0:
                return True
        except FileNotFoundError:
            continue
    return False


def choose_card(given: str | None, ask: Callable[[str], str] = input) -> tuple[str, str, dict | None]:
    """(disk, summary, lsblk info on Linux) for the card to erase."""
    if given:
        if LINUX:
            info = linux_disk_info(given)
            return given, check_linux_card(given, info), info
        return given, check_card(given, disk_info(given)), None
    cards = linux_cards() if LINUX else mac_cards()
    if not cards:
        raise FlashError("No micro-SD card found. Insert the card (8-256 GB) and run the script again.")
    print("\nCards found:")
    for number, (_, summary) in enumerate(cards, 1):
        print(f"  {number}. {summary}")
    if len(cards) == 1:
        disk = cards[0][0]
    else:
        answer = ask("Which card? Number: ").strip()
        if not answer.isdigit() or not 1 <= int(answer) <= len(cards):
            raise FlashError("No card chosen")
        disk = cards[int(answer) - 1][0]
    return choose_card(disk)


# --- CLI -------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a Raspberry Pi 5 motion-recording card.")
    parser.add_argument("disk", nargs="?", help="card disk: disk4 on a Mac, sdb or mmcblk0 on Linux (default: ask)")
    parser.add_argument("--organization", help="device's organization (default: ask)")
    parser.add_argument("--device", help="device name (default: ask)")
    parser.add_argument(
        "--ssh-key", action="append", default=[], metavar="KEY",
        help="an SSH public key line to let in as easysort (repeatable; Tailscale SSH works without one)",
    )
    parser.add_argument("--tag", help="Tailscale tag to advertise; only if the auth key is tagged")
    parser.add_argument("--no-check", action="store_true",
                        help="do not check the enrolment code with the Worker first (e.g. no internet here)")
    parser.add_argument("--dry-run", type=Path, metavar="DIR",
                        help="write the card files into DIR with placeholders; no disk, network or prompts")
    args = parser.parse_args(argv)
    if not LINUX and sys.platform != "darwin":
        parser.error("runs on macOS or Linux only")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        ssh_keys = list(dict.fromkeys(check_public_key(key, "--ssh-key") for key in args.ssh_key))
        suffix = secrets.token_hex(4)
        if args.dry_run:
            organization = check_name(args.organization or "example", "organization")
            device = check_name(args.device or "site-01", "device name")
            enrolment = Enrolment(organization, device, "enrolment-code-placeholder")
            args.dry_run.mkdir(parents=True, exist_ok=True)
            write_card(args.dry_run, card_files(
                enrolment, "tskey-auth-DRYRUN", "example-wifi", "wifi-password-placeholder", ssh_keys, suffix, args.tag
            ))
            print(f"Wrote the card files for {enrolment.hostname} to {args.dry_run}")
            return 0

        ssids, lookup = linux_wifi() if LINUX else mac_wifi()
        ssid, wifi_password = choose_wifi(ssids, lookup)
        auth_key = ask_auth_key()
        enrolment = ask_enrolment(args.organization, args.device)
        if args.no_check:
            print("Not checking the enrolment code (--no-check).")
        else:
            print("Checking the enrolment code with the Worker ...")
            check_enrolment(enrolment)
            print(f"Code OK and {enrolment.organization}/{enrolment.device} is free.")
        disk, summary, info = choose_card(args.disk)
        image = cached_image()
        print(
            f"\nAbout to ERASE {summary}\nand write a motion-recording card for {enrolment.hostname} "
            f"on Wi-Fi {ssid}."
        )
        if input(f"Type {disk} to continue: ").strip() != disk:
            print("Cancelled.")
            return 1
        if LINUX:
            linux_write_image(disk, image, info or {})
            boot = linux_mount_boot_partition(disk)
        else:
            write_image(disk, image)
            boot = mount_boot_partition(disk)
        try:
            write_card(boot, card_files(enrolment, auth_key, ssid, wifi_password, ssh_keys, suffix, args.tag))
            del auth_key, wifi_password, enrolment
        finally:
            if LINUX:
                linux_unmount(boot)
        if LINUX:
            ejected = linux_eject(disk)
        else:
            subprocess.run(["diskutil", "eject", f"/dev/{disk}"], check=True)
            ejected = True
    except FlashError as exc:
        print(f"flash: {mask(str(exc))}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"flash: {exc.cmd[0]} failed with exit code {exc.returncode}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1
    removal = "The card is ejected; you can remove it." if ejected else (
        "The card is written, but could not be ejected: eject it in your file manager before removing it."
    )
    print(
        f"\nDone. {removal}\nPut it in the Pi 5, plug in the camera(s), then the power. "
        "First start takes about 10 minutes (it installs packages); after that it records at every power-on."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
