"""15-minute windows and the R2 upload queue, with a fake Worker.

The window tests run the real ffmpeg when it is installed (skipped otherwise).
No network, camera or device login is used.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

import upload
import windows
from motion_capture import Clip, MotionFilter, MotionSettings
from upload import DeviceToken, LoginError, Response, Uploader
from windows import WINDOW_S, WindowSink, iso, window_key, window_start

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
T0 = 1_793_361_600  # 2026-10-30T12:00:00Z, a window start


def jpeg(value: int) -> bytes:
    frame = np.full((48, 64, 3), value % 256, dtype=np.uint8)
    return cv2.imencode(".jpg", frame)[1].tobytes()


class WindowNameTests(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(window_start(T0 + 899.9), T0)
        self.assertEqual(window_start(T0 + 900), T0 + 900)
        self.assertEqual(window_key(T0), ("2026-10-30", "1200"))
        self.assertEqual(window_key(T0 + 2700), ("2026-10-30", "1245"))
        self.assertEqual(iso(T0 + 1.25), "2026-10-30T12:00:01.250Z")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class WindowSinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def frames_in(self, mp4: Path) -> int:
        return windows.count_frames(mp4)

    def test_one_mp4_and_sidecar_per_window(self) -> None:
        sink = WindowSink(self.data, source={"model": "m"})
        first, second = Clip(T0 + 10, T0 + 10), Clip(T0 + 300, T0 + 300)
        for i in range(8):
            sink.frame(first, T0 + 10 + i * 0.25, jpeg(i), motion=i == 4)
        for i in range(4):
            sink.frame(second, T0 + 300 + i * 0.25, jpeg(100 + i))
        sink.tick(T0 + 899)
        self.assertEqual(list((self.data / "queue").rglob("*.json")), [])
        sink.tick(T0 + 900)

        mp4 = self.data / "queue" / "2026-10-30" / "1200.mp4"
        sidecar = json.loads(mp4.with_suffix(".json").read_text())
        self.assertEqual(self.frames_in(mp4), 12)
        self.assertEqual(sidecar["frame_count"], 12)
        self.assertEqual(sidecar["motion_frames"], 1)
        self.assertEqual(sidecar["window_start"], "2026-10-30T12:00:00.000Z")
        self.assertEqual(sidecar["frames"][0], {"t": "2026-10-30T12:00:10.000Z", "motion": False, "clip": 0})
        self.assertEqual(sidecar["frames"][-1]["t"], "2026-10-30T12:05:00.750Z")
        self.assertEqual([c["frames"] for c in sidecar["clips"]], [8, 4])
        self.assertEqual(sidecar["source"], {"model": "m"})
        self.assertFalse(sidecar["incomplete"])
        self.assertEqual(list((self.data / "work").iterdir()), [])

    def test_stop_ends_ffmpeg_cleanly_in_its_own_session(self) -> None:
        sink = WindowSink(self.data)
        clip = Clip(T0, T0)
        sink.frame(clip, T0, jpeg(1))
        process = sink._window.part.process
        # A signal to the service's process group does not reach ffmpeg.
        self.assertNotEqual(os.getpgid(process.pid), os.getpgid(0))
        with self.assertNoLogs("pi5-motion", "ERROR"):
            sink.stop()
        self.assertEqual(process.returncode, 0)

    def test_empty_window_writes_nothing(self) -> None:
        sink = WindowSink(self.data)
        sink.tick(T0 + 900)
        sink.recover(T0 + 5000)
        self.assertEqual(list((self.data / "queue").iterdir()), [])

    def test_frame_in_next_window_finishes_the_previous(self) -> None:
        sink = WindowSink(self.data)
        clip = Clip(T0 + 899, T0 + 899)
        sink.frame(clip, T0 + 899.5, jpeg(1))
        sink.frame(clip, T0 + 900.25, jpeg(2))
        self.assertTrue((self.data / "queue" / "2026-10-30" / "1200.json").exists())
        sink.tick(T0 + 1800)
        later = json.loads((self.data / "queue" / "2026-10-30" / "1215.json").read_text())
        self.assertEqual(later["frame_count"], 1)

    def test_restart_inside_a_window_joins_the_parts(self) -> None:
        sink, first = WindowSink(self.data), Clip(T0 + 60, T0 + 60)
        for i in range(3):
            sink.frame(first, T0 + 60 + i, jpeg(i))
        sink.stop()  # systemctl restart
        again, second = WindowSink(self.data), Clip(T0 + 200, T0 + 200)
        again.recover(T0 + 120)  # the window is still open: left alone
        for i in range(2):
            again.frame(second, T0 + 200 + i, jpeg(50 + i))
        again.tick(T0 + 900)
        mp4 = self.data / "queue" / "2026-10-30" / "1200.mp4"
        sidecar = json.loads(mp4.with_suffix(".json").read_text())
        self.assertEqual(self.frames_in(mp4), 5)
        self.assertEqual(len(sidecar["clips"]), 2)

    def test_window_left_by_a_restart_is_finished_without_new_frames(self) -> None:
        sink, clip = WindowSink(self.data), Clip(T0 + 60, T0 + 60)
        for i in range(3):
            sink.frame(clip, T0 + 60 + i, jpeg(i))
        sink.stop()  # restarted at 12:11, nobody seen since
        again = WindowSink(self.data)
        again.recover(T0 + 660)
        again.tick(T0 + 899)
        self.assertEqual(list((self.data / "queue").rglob("*.json")), [])
        again.tick(T0 + 910)  # the work/ scan runs every 10 s
        sidecar = json.loads((self.data / "queue" / "2026-10-30" / "1200.json").read_text())
        self.assertEqual(sidecar["frame_count"], 3)

    def test_recover_after_power_cut(self) -> None:
        sink = WindowSink(self.data)
        for i in range(4):
            sink.frame(Clip(0, 0), T0 + i, jpeg(i))
        sink.stop()
        folder = self.data / "work" / str(T0)
        # The sidecar lines got further than the mp4 before power went.
        with (folder / "part-000.jsonl").open("a") as handle:
            handle.write(json.dumps({"t": T0 + 9, "motion": True, "clip": "0-0"}) + "\n")
        WindowSink(self.data).recover(T0 + 901)
        sidecar = json.loads((self.data / "queue" / "2026-10-30" / "1200.json").read_text())
        self.assertEqual(sidecar["frame_count"], 4)
        self.assertTrue(sidecar["incomplete"])

    def test_full_window_drops_the_rest(self) -> None:
        sink = WindowSink(self.data)
        limit = windows.MAX_WINDOW_BYTES
        windows.MAX_WINDOW_BYTES = 1
        try:
            sink.frame(Clip(0, 0), T0, jpeg(1))
        finally:
            windows.MAX_WINDOW_BYTES = limit
        sink.tick(T0 + 900)
        self.assertEqual(list((self.data / "queue").rglob("*.json")), [])

    def test_motion_filter_feeds_the_window(self) -> None:
        sink = WindowSink(self.data)
        motion_filter = MotionFilter(sink, MotionSettings())
        for i in range(40):
            motion_filter.process(T0 + i * 0.25, jpeg(i), 20 <= i < 24)
        sink.tick(T0 + 900)
        sidecar = json.loads((self.data / "queue" / "2026-10-30" / "1200.json").read_text())
        # 1 s of motion, 2 s either side at 4 fps.
        self.assertEqual(sidecar["frame_count"], 4 + 8 + 8)
        self.assertEqual(sidecar["motion_frames"], 4)
        self.assertEqual(sidecar["frames"][0]["t"], iso(T0 + 3.0))


TOKEN = "esd1.example.site-01." + "A" * 43


class FakeCloud:
    """The device-storage Worker."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.auth: list[str] = []
        self.script: list[Response] = []

    def __call__(self, method, url, headers, body=None, timeout=300):
        self.calls.append((method, url))
        self.auth.append(headers["Authorization"])
        if self.script:
            return self.script.pop(0)
        data = body.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert headers["X-Content-SHA256"] == digest
        assert headers["Content-Length"] == str(len(data))
        key = url.split("/reuse-detection/", 1)[1]
        if key in self.objects:
            return Response(409, {"error": "exists", "sha256": self.objects[key]})
        self.objects[key] = digest
        return Response(201, {"key": key, "size": len(data), "sha256": digest})


class UploaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.token_file = self.data / "device-token"
        self.token_file.write_text(TOKEN + "\n")
        self.cloud = FakeCloud()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def queue(self, date: str, hhmm: str, size: int = 10) -> Path:
        folder = self.data / "queue" / date
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{hhmm}.mp4").write_bytes(b"m" * size)
        (folder / f"{hhmm}.json").write_text("{}")
        return folder / f"{hhmm}.json"

    def uploader(self, **kwargs) -> Uploader:
        login = DeviceToken(self.token_file)
        return Uploader(self.data, login, "https://worker.test/device-storage/v1/reuse-detection",
                        http=self.cloud, free_bytes=lambda _: 10**12, **kwargs)

    def test_token_gives_the_device(self) -> None:
        login = DeviceToken(self.token_file)
        self.assertEqual(login.token(), TOKEN)
        self.assertEqual((login.organization, login.device), ("example", "site-01"))

    def test_malformed_token_file_waits(self) -> None:
        self.token_file.write_text("not a token\n")
        with self.assertRaises(LoginError):
            DeviceToken(self.token_file).token()
        sidecar = self.queue("2026-10-30", "1200")
        with self.assertLogs("pi5-motion", "WARNING"):
            self.assertGreater(self.uploader().run_once(), 0)
        self.assertTrue(sidecar.exists())

    def test_uploads_mp4_then_json_and_empties_queue(self) -> None:
        self.queue("2026-10-30", "1200")
        self.queue("2026-10-30", "1215")
        self.assertEqual(self.uploader().run_once(), 0)
        puts = [url.split("reuse-detection/")[1] for method, url in self.cloud.calls if method == "PUT"]
        self.assertEqual(puts, [
            "example/site-01/2026-10-30/1200.mp4",
            "example/site-01/2026-10-30/1200.json",
            "example/site-01/2026-10-30/1215.mp4",
            "example/site-01/2026-10-30/1215.json",
        ])
        self.assertEqual(set(self.cloud.auth), {f"Bearer {TOKEN}"})
        self.assertEqual(list((self.data / "queue").iterdir()), [])

    def test_matching_409_counts_as_uploaded(self) -> None:
        sidecar = self.queue("2026-10-30", "1200")
        self.cloud.objects["example/site-01/2026-10-30/1200.mp4"] = hashlib.sha256(b"m" * 10).hexdigest()
        self.uploader().run_once()
        self.assertFalse(sidecar.exists())
        self.assertIn("example/site-01/2026-10-30/1200.json", self.cloud.objects)

    def test_different_409_is_parked(self) -> None:
        self.queue("2026-10-30", "1200")
        self.cloud.objects["example/site-01/2026-10-30/1200.mp4"] = "0" * 64
        self.uploader().run_once()
        self.assertTrue((self.data / "queue" / "parked" / "2026-10-30" / "1200.mp4").exists())
        self.assertNotIn("example/site-01/2026-10-30/1200.json", self.cloud.objects)

    def test_401_reads_the_token_file_again_once(self) -> None:
        self.queue("2026-10-30", "1200")
        self.cloud.script = [Response(401, {"error": "invalid token"})]
        new = "esd1.example.site-01." + "B" * 43
        self.token_file.write_text(new)  # e.g. the Pi enrolled again
        uploader = self.uploader()
        uploader.login._token = TOKEN
        uploader.login.organization, uploader.login.device = "example", "site-01"
        uploader.run_once()
        self.assertEqual(self.cloud.auth[:2], [f"Bearer {TOKEN}", f"Bearer {new}"])
        self.assertEqual(len(self.cloud.objects), 2)

    def test_revoked_device_keeps_its_windows(self) -> None:
        sidecar = self.queue("2026-10-30", "1200")
        self.cloud.script = [Response(401, {"error": "revoked"}), Response(401, {"error": "revoked"})]
        with self.assertLogs("pi5-motion", "WARNING"):
            self.assertGreater(self.uploader().run_once(), 0)
        self.assertTrue(sidecar.exists() and sidecar.with_suffix(".mp4").exists())

    def test_server_error_keeps_the_window_for_later(self) -> None:
        sidecar = self.queue("2026-10-30", "1200")
        self.cloud.script = [Response(503, {"error": "auth unavailable"}, retry_after=120)]
        self.assertEqual(self.uploader().run_once(), 120)
        self.assertTrue(sidecar.exists() and sidecar.with_suffix(".mp4").exists())
        self.assertEqual(self.uploader().run_once(), 0)
        self.assertFalse(sidecar.exists())

    def test_mp4_already_sent_resumes_with_json(self) -> None:
        sidecar = self.queue("2026-10-30", "1200")
        self.cloud.script = [Response(201, {}), Response(500, {})]
        self.assertEqual(self.uploader().run_once(), upload.RETRY_MIN_S)
        self.assertFalse(sidecar.with_suffix(".mp4").exists())
        self.uploader().run_once()
        self.assertFalse(sidecar.exists())

    def test_before_enrolment_windows_stay_queued(self) -> None:
        self.token_file.unlink()
        sidecar = self.queue("2026-10-30", "1200")
        self.assertEqual(self.uploader().run_once(), 60)
        self.assertTrue(sidecar.exists())
        self.assertEqual(self.cloud.calls, [])

    def test_disk_cap_deletes_oldest_windows(self) -> None:
        self.token_file.unlink()
        old = self.queue("2026-10-29", "2345", size=600)
        new = self.queue("2026-10-30", "0000", size=600)
        with redirect_stderr(io.StringIO()):
            self.uploader(cap_bytes=1000).run_once()
        self.assertFalse(old.exists() or old.with_suffix(".mp4").exists())
        self.assertTrue(new.exists())

    def test_requests_name_their_agent(self) -> None:
        seen = {}

        class Reply(io.BytesIO):
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout):
            seen.update(request.header_items())
            return Reply(b'{"key": "k"}')

        with mock.patch.object(upload.urllib.request, "urlopen", urlopen):
            reply = upload.http_request("PUT", "https://worker.test/x", {"Content-Length": "0"}, b"")
        self.assertEqual(reply.body, {"key": "k"})
        self.assertEqual(seen["User-agent"], "easysort-edge/1")

    def test_run_forever_stops(self) -> None:
        stop = threading.Event()
        stop.set()
        self.uploader().run_forever(stop)


if __name__ == "__main__":
    unittest.main()
