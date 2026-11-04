"""The camera check page and the snapshots motion_capture.py writes for it.

No camera, network or Pi is needed: system readers get fake files and commands.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import numpy as np

import preview
from motion_capture import Snapshots, camera_name, parse_args
from upload import DeviceToken, Response, Uploader

HERE = Path(__file__).resolve().parent
NOW = 1_793_361_600.0


def frame(value: int, width: int = 1920, height: int = 1080) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


def fake_run(throttled: str = "throttled=0x50005\n"):
    def run(command, **_):
        if command[0] == "vcgencmd":
            return subprocess.CompletedProcess(command, 0, throttled, "")
        if command[0] == "systemctl":
            out = "ActiveState=active\nSubState=running\nActiveEnterTimestampMonotonic=1\nNRestarts=2\n"
            return subprocess.CompletedProcess(command, 0, out, "")
        raise OSError("unexpected command")
    return run


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def index(self) -> dict:
        return json.loads((self.dir / "snapshots.json").read_text())

    def test_live_every_5_s_motion_every_10_s_newest_5(self) -> None:
        snaps = Snapshots(self.dir, ["cam-a", "cam-b"])
        for step in range(240):  # 60 s at 4 fps, motion on camera b all the time
            snaps.update(NOW + step * 0.25, [frame(10), frame(200)], [False, True])
        index = self.index()
        self.assertEqual([e["camera"] for e in index["live"]], ["cam-a", "cam-b"])
        self.assertEqual(index["live"][0]["ts"], NOW + 55)
        self.assertEqual([e["ts"] for e in index["motion"]], [NOW + 10 * i for i in range(1, 6)])
        self.assertTrue(all(e["camera"] == "cam-b" for e in index["motion"]))
        files = sorted(p.name for p in self.dir.glob("*.jpg"))
        self.assertEqual(len(files), 2 * 2 + 5)  # newest and previous live per camera, 5 motion
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith(".")), [])

    def test_images_are_small(self) -> None:
        snaps = Snapshots(self.dir, ["cam"])
        rng = np.random.default_rng(1)
        noisy = rng.integers(0, 255, (1080, 1920, 3), dtype=np.uint8)
        snaps.update(NOW, [noisy], [True])
        import cv2
        path = self.dir / self.index()["live"][0]["file"]
        self.assertEqual(cv2.imread(str(path)).shape[:2], (540, 960))

    def test_motion_frames_survive_a_restart_live_ones_do_not(self) -> None:
        Snapshots(self.dir, ["cam"]).update(NOW, [frame(1)], [True])
        (self.dir / "live-0-1.jpg").write_bytes(b"old")
        snaps = Snapshots(self.dir, ["cam"])
        self.assertEqual(len(snaps.motion), 1)
        self.assertEqual(sorted(p.name.split("-")[0] for p in self.dir.glob("*.jpg")), ["motion"])

    def test_errors_never_reach_recording(self) -> None:
        snaps = Snapshots(self.dir / "missing" / "x", ["cam"])
        with mock.patch("upload.write_atomic", side_effect=OSError("no space")):
            with self.assertLogs("pi5-motion", "WARNING") as logs:
                snaps.update(NOW, [frame(1)], [True])
                snaps.update(NOW + 20, [frame(1)], [True])
        self.assertEqual(len(logs.output), 1)

    def test_camera_name_and_flag(self) -> None:
        self.assertEqual(camera_name("/dev/v4l/by-id/usb-046d_C920_AB12-video-index0"), "046d_C920_AB12")
        self.assertIsNone(parse_args([]).snapshots)


class UploadStatusTests(unittest.TestCase):
    def test_last_result_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            token = data / "token"
            token.write_text("esd1.acme.yard-09." + "x" * 43)
            window = data / "queue" / "2026-10-30"
            window.mkdir(parents=True)
            (window / "1200.mp4").write_bytes(b"mp4")
            (window / "1200.json").write_text("{}")
            status = data / "upload.json"
            replies = [Response(201, {}), Response(503, {})]
            uploader = Uploader(data, DeviceToken(token), "https://w.test/r", http=lambda *a, **k: replies.pop(0),
                                free_bytes=lambda _: 10**12, status=status)
            with self.assertLogs("pi5-motion"):
                uploader.run_once()
            last = json.loads(status.read_text())
            self.assertEqual((last["file"], last["result"]), ("2026-10-30/1200.json", "503, retrying"))


class PageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.run_dir, self.data = root / "run", root / "data"
        self.run_dir.mkdir()
        (self.data / "queue" / "2026-10-30").mkdir(parents=True)
        (self.data / "queue" / "2026-10-30" / "1200.json").write_text("{}")
        (self.data / "queue" / "2026-10-30" / "1200.mp4").write_bytes(b"x" * 2_000_000)
        (self.data / "queue" / "parked" / "2026-10-29").mkdir(parents=True)
        (self.data / "queue" / "parked" / "2026-10-29" / "0900.json").write_text("{}")
        self.route = root / "route"
        self.route.write_text(
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n"
            "eth1\t00000000\t0108A8C0\t0003\t0\t0\t100\t00000000\n"
            "eth1\t0008A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\n"
        )
        self.temp = root / "temp"
        self.temp.write_text("51234\n")
        snaps = Snapshots(self.run_dir, ["046d_C920<x>"])
        snaps.update(NOW - 3, [frame(50)], [True])
        (self.run_dir / "upload.json").write_text(json.dumps({"at": NOW - 60, "file": "2026-10-30/1145.json",
                                                              "result": "stored (201)"}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def page(self, now: float = NOW) -> str:
        return preview.render_page(self.run_dir, self.data, now, fake_run(), self.route, self.temp)

    def test_page_shows_status_and_images(self) -> None:
        page = self.page()
        for text in ("active (running)", "restarted 2 times", "stored (201) for 2026-10-30/1145.json",
                     "1 window(s) waiting, 2.0 MB", "1 parked", "51.2 °C",
                     "under-voltage now, throttled now, under-voltage since boot, throttled since boot (0x50005)",
                     "eth1 (4G dongle); standby: wlan0 (Wi-Fi)", "046d_C920&lt;x&gt;", "11:59:57 UTC"):
            self.assertIn(text, page)
        self.assertEqual(page.count("<img "), 2)  # live and one motion
        self.assertNotIn("&amp;", page)
        self.assertNotIn("<script", page)
        self.assertNotIn("refresh", page.lower().replace("no auto refresh", ""))
        self.assertLess(len(page), 8000)
        self.assertNotIn(str(self.run_dir), page)

    def test_old_live_frame_is_flagged(self) -> None:
        self.assertIn("is the recording service running", self.page(NOW + 120))

    def test_decode(self) -> None:
        self.assertEqual(preview.decode_throttled(0), "OK")
        self.assertEqual(preview.throttled(fake_run("throttled=0x0\n")), "OK (0x0)")
        self.assertIn("unknown", preview.throttled(fake_run("garbage")))
        self.assertEqual(preview.network(Path("/nonexistent")), "no default route (offline?)")

    def test_server_headers_and_only_snapshot_files(self) -> None:
        (self.run_dir.parent / "secret.jpg").write_text("no")
        server = preview.make_server("127.0.0.1", 0, self.run_dir, self.data)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with mock.patch.object(preview, "service_state", return_value="active"), \
                urllib.request.urlopen(base + "/") as reply:
            self.assertEqual(reply.headers["Cache-Control"], "no-store")
            page = reply.read().decode()
        image = page.split('src="', 1)[1].split('"', 1)[0]
        with urllib.request.urlopen(base + image) as reply:
            self.assertEqual(reply.headers["Content-Type"], "image/jpeg")
            self.assertIn("immutable", reply.headers["Cache-Control"])
        for bad in ("/img/../secret.jpg", "/img/%2e%2e/secret.jpg", "/img/snapshots.json", "/upload.json",
                    "/img/live-0-1.jpg"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + bad)
            self.assertEqual(caught.exception.code, 404, bad)

    def test_listens_on_loopback_by_default(self) -> None:
        with mock.patch.object(preview, "make_server") as make:
            make.return_value.serve_forever.return_value = None
            preview.main([])
        self.assertEqual(make.call_args.args[:2], ("127.0.0.1", 8080))


class UnitTests(unittest.TestCase):
    def test_preview_is_loopback_only_and_independent(self) -> None:
        unit = (HERE / "easysort-preview.service").read_text()
        self.assertIn("--bind 127.0.0.1", unit)
        self.assertIn("User=easysort", unit)
        motion = (HERE / "easysort-motion.service").read_text()
        self.assertNotIn("preview", motion)
        self.assertIn("RuntimeDirectory=easysort-motion", motion)
        self.assertIn("--snapshots /run/easysort-motion", motion)
        for line in unit.splitlines():
            self.assertFalse(line.startswith(("Requires=", "BindsTo=", "PartOf=")), line)

    def test_new_cards_install_and_enable_it(self) -> None:
        import flash
        self.assertIn("preview.py", flash.APP_FILES)
        self.assertIn("easysort-preview.service", flash.APP_FILES)
        user_data = flash.render_user_data("acme-yard-09", [])
        self.assertIn("{}/easysort-preview.service, /etc/systemd/system/".format(flash.APP_DIR), user_data)
        enable = [line for line in user_data.splitlines() if "[systemctl, enable," in line][0]
        self.assertIn("easysort-preview.service", enable)


if __name__ == "__main__":
    unittest.main()
