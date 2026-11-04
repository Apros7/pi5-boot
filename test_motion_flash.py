"""Motion filter, camera layout and the card writer. No camera, disk or network is used."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import numpy as np

import flash
from flash import DeviceLogin, FlashError
from motion_capture import (
    CameraReader,
    MotionDetector,
    MotionFilter,
    MotionSettings,
    compose,
    find_cameras,
    tile_sizes,
)

HERE = Path(__file__).resolve().parent


class Recorder:
    def __init__(self) -> None:
        self.frames: list[tuple[int, float, bytes, bool]] = []

    def frame(self, clip, ts, jpeg, motion=False) -> None:
        self.frames.append((clip.number, ts, jpeg, motion))


def block(x: int) -> np.ndarray:
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    frame[40:80, x : x + 40] = 255
    return frame


class MotionDetectorTests(unittest.TestCase):
    def test_still_frames_are_not_motion(self) -> None:
        detector = MotionDetector(MotionSettings())
        self.assertEqual([detector.update(block(10)) for _ in range(5)], [False] * 5)

    def test_moving_block_is_motion(self) -> None:
        detector = MotionDetector(MotionSettings())
        detector.update(block(10))
        self.assertTrue(detector.update(block(60)))

    def test_sensor_noise_is_not_motion(self) -> None:
        detector = MotionDetector(MotionSettings())
        rng = np.random.default_rng(1)
        base = np.full((120, 160, 3), 128, dtype=np.uint8)
        results = [detector.update((base + rng.integers(0, 6, base.shape)).astype(np.uint8)) for _ in range(5)]
        self.assertFalse(any(results))


class MotionFilterTests(unittest.TestCase):
    def run_filter(self, motion_at: set[int], count: int = 80, **settings) -> Recorder:
        sink = Recorder()
        motion_filter = MotionFilter(sink, MotionSettings(**settings))
        for i in range(count):
            motion_filter.process(i * 0.25, bytes([i]), i in motion_at)
        return sink

    def test_no_motion_keeps_nothing(self) -> None:
        self.assertEqual(self.run_filter(set()).frames, [])

    def test_keeps_pre_and_post_roll(self) -> None:
        sink = self.run_filter({40})
        times = [ts for _, ts, _, _ in sink.frames]
        self.assertEqual(times[0], 8.0)  # 2 s before
        self.assertEqual(times[-1], 12.0)  # 2 s after
        self.assertEqual(len(times), 17)
        self.assertEqual([m for *_, m in sink.frames].count(True), 1)

    def test_close_motions_share_a_clip(self) -> None:
        sink = self.run_filter({20, 27})  # 1.75 s apart, inside the 2 s post-roll
        self.assertEqual(len({clip for clip, *_ in sink.frames}), 1)

    def test_far_apart_motions_are_two_clips(self) -> None:
        sink = self.run_filter({10, 60})
        self.assertEqual(len({clip for clip, *_ in sink.frames}), 2)

    def test_long_motion_is_split(self) -> None:
        sink = self.run_filter(set(range(80)), max_clip_s=5)
        self.assertGreaterEqual(len({clip for clip, *_ in sink.frames}), 3)
        self.assertEqual(len(sink.frames), 80)

    def test_buffer_is_bounded(self) -> None:
        motion_filter = MotionFilter(Recorder(), MotionSettings(buffer_s=1))
        for i in range(100):
            motion_filter.process(i * 0.25, b"x", False)
        self.assertLessEqual(len(motion_filter._buffer), 5)


class LayoutTests(unittest.TestCase):
    def test_one_camera_is_capped_at_1080(self) -> None:
        self.assertEqual(tile_sizes([(3840, 2160)]), [(1920, 1080)])
        self.assertEqual(tile_sizes([(1280, 720)]), [(1280, 720)])

    def test_cameras_share_the_smallest_height(self) -> None:
        self.assertEqual(tile_sizes([(1920, 1080), (1280, 720)]), [(1280, 720), (1280, 720)])

    def test_compose_side_by_side_with_a_missing_camera(self) -> None:
        tiles = [(64, 48), (32, 48)]
        image = compose([np.full((96, 128, 3), 200, dtype=np.uint8), None], tiles)
        self.assertEqual(image.shape, (48, 96, 3))
        self.assertTrue((image[:, :64] == 200).all())
        self.assertTrue((image[:, 64:] == 0).all())

    def test_find_cameras(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("usb-B-video-index0", "usb-A-video-index0", "usb-A-video-index1"):
                (Path(tmp) / name).touch()
            self.assertEqual(
                [Path(p).name for p in find_cameras(f"{tmp}/*-video-index0")],
                ["usb-A-video-index0", "usb-B-video-index0"],
            )


class FakeCapture:
    def __init__(self, fail_after: int | None = None) -> None:
        self.reads = 0
        self.fail_after = fail_after
        self.released = False

    def read(self):
        self.reads += 1
        time.sleep(0.005)
        if self.fail_after is not None and self.reads > self.fail_after:
            return False, None
        return True, np.zeros((48, 64, 3), dtype=np.uint8)

    def release(self) -> None:
        self.released = True


class CameraReaderTests(unittest.TestCase):
    def test_reads_newest_frame_and_size(self) -> None:
        reader = CameraReader("/dev/fake", opener=lambda _: FakeCapture())
        reader.start(timeout_s=2)
        try:
            self.assertEqual(reader.size, (64, 48))
            self.assertIsNotNone(reader.latest())
        finally:
            reader.stop()

    def test_reopens_after_a_failed_read(self) -> None:
        opened = []

        def opener(_):
            opened.append(FakeCapture(fail_after=2))
            return opened[-1]

        with self.assertLogs("pi5-motion", "WARNING"):
            reader = CameraReader("/dev/fake", opener=opener)
            reader.start(timeout_s=2)
            deadline = time.monotonic() + 3
            while len(opened) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            reader.stop()
        self.assertGreaterEqual(len(opened), 2)
        self.assertTrue(opened[0].released)

    def test_camera_that_never_opens(self) -> None:
        def opener(_):
            raise OSError("could not open camera /dev/fake")

        reader = CameraReader("/dev/fake", opener=opener)
        with self.assertLogs("pi5-motion", "WARNING"), self.assertRaises(OSError):
            reader.start(timeout_s=0.3)
        reader.stop()


LOGIN = DeviceLogin("acme", "yard-09", flash.device_email("acme", "yard-09"), "pw 'quoted'")


class CardTests(unittest.TestCase):
    def files(self, **kwargs) -> dict[str, str]:
        args = dict(login=LOGIN, auth_key="tskey-auth-SECRET1", ssid='Site "Wi-Fi"', wifi_password="hunter22",
                    ssh_keys=[], suffix="abcd", tag=None)
        args.update(kwargs)
        return flash.card_files(**args)

    def test_secrets_stay_out_of_cloud_init_user_data(self) -> None:
        files = self.files()
        self.assertNotIn("tskey", files["user-data"])
        self.assertNotIn("quoted", files["user-data"])
        self.assertNotIn("hunter22", files["user-data"])
        self.assertEqual(files["easysort/tailscale-authkey"], "tskey-auth-SECRET1\n")

    def test_device_env_round_trips(self) -> None:
        from upload import read_env

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "device.env"
            path.write_text(self.files()["easysort/device.env"])
            env = read_env(path)
        self.assertEqual(env["DEVICE_PASSWORD"], "pw 'quoted'")
        self.assertEqual(env["DEVICE_EMAIL"], "recycling-acme-yard-09@devices.invalid")
        self.assertEqual(env["SUPABASE_URL"], flash.SUPABASE_URL)

    def test_hostname_and_settings(self) -> None:
        files = self.files(tag="tag:pi")
        self.assertIn("hostname: acme-yard-09\n", files["user-data"])
        self.assertEqual(json.loads(files["easysort/settings.json"]), {"hostname": "acme-yard-09", "tag": "tag:pi"})
        self.assertIn("local-hostname: acme-yard-09", files["meta-data"])

    def test_wifi_is_quoted(self) -> None:
        config = self.files()["network-config"]
        self.assertIn('"Site \\"Wi-Fi\\"":', config)
        self.assertIn('password: "hunter22"', config)
        self.assertIn("renderer: NetworkManager", config)

    def test_ssh_keys_are_optional(self) -> None:
        self.assertNotIn("ssh_authorized_keys", self.files()["user-data"])
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample me@laptop"
        self.assertIn(json.dumps(key), self.files(ssh_keys=[key])["user-data"])

    def test_user_data_parses_as_yaml_if_available(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        data = yaml.safe_load(self.files(ssh_keys=["ssh-ed25519 AAAA me"])["user-data"])
        self.assertEqual(data["users"][0]["name"], "easysort")
        self.assertEqual(data["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 AAAA me"])
        network = yaml.safe_load(self.files()["network-config"])
        self.assertEqual(network["network"]["wifis"]["wlan0"]["access-points"], {'Site "Wi-Fi"': {"password": "hunter22"}})

    def test_dry_run_writes_the_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(flash.main(["--dry-run", str(out), "--organization", "acme", "--device", "x-01"]), 0)
            for name in ("user-data", "network-config", "meta-data", "easysort/device.env",
                         "easysort/tailscale-authkey", "easysort/settings.json"):
                self.assertTrue((out / name).is_file(), name)
            for name in flash.APP_FILES:
                self.assertTrue((out / "easysort" / "app" / name).is_file(), name)

    def test_names_are_checked(self) -> None:
        with self.assertRaises(FlashError):
            flash.check_name("Acme Yard", "device name")
        self.assertEqual(flash.check_name(" Acme ", "organization"), "acme")


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LoginTests(unittest.TestCase):
    def reply(self, organization: str, device: str):
        body = {"access_token": "t", "user": {"app_metadata": {"organization_name": organization, "device_name": device}}}
        seen = {}

        def urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data)
            seen["headers"] = dict(request.header_items())
            return Reply(json.dumps(body).encode())

        return urlopen, seen

    def test_matching_login(self) -> None:
        urlopen, seen = self.reply("acme", "yard-09")
        flash.verify_login(LOGIN, urlopen)
        self.assertTrue(seen["url"].endswith("/auth/v1/token?grant_type=password"))
        self.assertEqual(seen["body"]["email"], LOGIN.email)
        self.assertEqual(seen["headers"]["User-agent"], "easysort-edge/1")

    def test_login_for_another_device(self) -> None:
        urlopen, _ = self.reply("acme", "other-01")
        with self.assertRaisesRegex(FlashError, "acme/other-01"):
            flash.verify_login(LOGIN, urlopen)

    def test_wrong_password(self) -> None:
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))

        with self.assertRaisesRegex(FlashError, "refused the login"):
            flash.verify_login(LOGIN, urlopen)

    def test_ask_login_derives_the_email(self) -> None:
        answers = iter(["Acme", "yard-09"])
        with mock.patch("sys.stdout", io.StringIO()):
            login = flash.ask_login(None, None, ask=lambda _: next(answers), ask_secret=lambda _: "pw")
        self.assertEqual(login, DeviceLogin("acme", "yard-09", "recycling-acme-yard-09@devices.invalid", "pw"))

    def test_auth_key_format(self) -> None:
        self.assertEqual(flash.ask_auth_key(lambda _: " tskey-auth-abc123 "), "tskey-auth-abc123")
        with self.assertRaises(FlashError):
            flash.ask_auth_key(lambda _: "tskey-api-abc")


def runner(outputs: dict[str, tuple[int, str]]):
    def run(cmd, **kwargs):
        for prefix, (code, out) in outputs.items():
            if " ".join(cmd).startswith(prefix):
                return subprocess.CompletedProcess(cmd, code, out, "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    return run


class WifiTests(unittest.TestCase):
    def choose(self, ssids, passwords, answers, secret="typed-password"):
        replies = iter(answers)
        with mock.patch("sys.stdout", io.StringIO()):
            return flash.choose_wifi(ssids, passwords.get, ask=lambda _: next(replies), ask_secret=lambda _: secret)

    def test_enter_takes_the_first_with_its_saved_password(self) -> None:
        self.assertEqual(self.choose(["Site", "Home"], {"Site": "sitepass1"}, ["", "y"]), ("Site", "sitepass1"))

    def test_pick_by_number_and_type_the_password(self) -> None:
        self.assertEqual(self.choose(["Site", "Home"], {}, ["2"]), ("Home", "typed-password"))

    def test_decline_the_saved_password(self) -> None:
        self.assertEqual(self.choose(["Site"], {"Site": "sitepass1"}, ["1", "n"]), ("Site", "typed-password"))

    def test_type_a_network_not_in_the_list(self) -> None:
        self.assertEqual(self.choose(["Home"], {}, ["Other net"]), ("Other net", "typed-password"))

    def test_short_password_is_refused(self) -> None:
        with self.assertRaises(FlashError):
            self.choose([], {}, ["Site"], secret="short")

    def test_mac_reads_saved_networks_when_the_current_is_hidden(self) -> None:
        run = runner({
            "networksetup -listallhardwareports": (0, "Hardware Port: Ethernet\nDevice: en0\n\nHardware Port: Wi-Fi\nDevice: en1\n"),
            "ipconfig getsummary en1": (0, "<dictionary> {\n  SSID : <redacted>\n}\n"),
            "networksetup -listpreferredwirelessnetworks en1": (0, "Preferred networks on en1:\n\tSite\n\tHome\n"),
            "security find-generic-password -D AirPort network password -a Site -w": (0, "sitepass1\n"),
        })
        ssids, lookup = flash.mac_wifi(run)
        self.assertEqual(ssids, ["Site", "Home"])
        self.assertEqual(lookup("Site"), "sitepass1")
        self.assertIsNone(lookup("Home"))

    def test_mac_current_network_goes_first(self) -> None:
        run = runner({
            "networksetup -listallhardwareports": (0, "Hardware Port: Wi-Fi\nDevice: en0\n"),
            "ipconfig getsummary en0": (0, "  SSID : Home\n"),
            "networksetup -listpreferredwirelessnetworks en0": (0, "Preferred networks on en0:\n\tSite\n\tHome\n"),
        })
        self.assertEqual(flash.mac_wifi(run)[0], ["Home", "Site"])

    def test_linux_reads_network_manager(self) -> None:
        run = runner({
            "nmcli -t -f TYPE,NAME connection show --active": (0, "802-3-ethernet:Wired\n802-11-wireless:Site conn\n"),
            "nmcli --escape no -g 802-11-wireless.ssid connection show Site conn": (0, "Site\n"),
            "nmcli --escape no -s -g": (0, "Site\nsitepass1\n"),
        })
        ssids, lookup = flash.linux_wifi(run)
        self.assertEqual(ssids, ["Site"])
        self.assertEqual(lookup("Site"), "sitepass1")


class CardCheckTests(unittest.TestCase):
    def test_refuses_internal_disk(self) -> None:
        info = {"DeviceIdentifier": "disk0", "WholeDisk": True, "Internal": True, "TotalSize": 32 * 1000**3}
        with self.assertRaisesRegex(FlashError, "internal"):
            flash.check_card("disk0", info)

    def test_accepts_sd_card(self) -> None:
        info = {"DeviceIdentifier": "disk4", "WholeDisk": True, "Internal": False, "Removable": True,
                "VirtualOrPhysical": "Physical", "TotalSize": 32 * 1000**3, "MediaName": "SD Card"}
        self.assertIn("SD Card", flash.check_card("disk4", info))

    def test_linux_refuses_system_disk(self) -> None:
        info = {"name": "sda", "type": "disk", "rm": True, "size": 32 * 1000**3,
                "children": [{"path": "/dev/sda1", "mountpoints": ["/"]}]}
        with self.assertRaisesRegex(FlashError, "holds this computer"):
            flash.check_linux_card("sda", info)


class FilesTests(unittest.TestCase):
    def test_firstboot_is_valid_sh(self) -> None:
        subprocess.run(["sh", "-n", str(HERE / "firstboot.sh")], check=True)

    def test_units_point_at_the_installed_paths(self) -> None:
        unit = (HERE / "easysort-motion.service").read_text()
        self.assertIn(f"{flash.APP_DIR}/motion_capture.py", unit)
        self.assertIn("/etc/easysort/device.env", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("/usr/local/sbin/easysort-firstboot", (HERE / "easysort-firstboot.service").read_text())


if __name__ == "__main__":
    unittest.main()
