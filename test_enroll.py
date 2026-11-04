"""First-boot enrolment against a fake Worker. No network is used."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from enroll import enroll
from upload import Response

TOKEN = "esd1.acme.yard-09." + "x" * 43


class FakeWorker:
    def __init__(self, *replies: Response) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, body=None, timeout=300):
        self.calls.append((method, url, json.loads(body)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class EnrollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "enroll.json"
        self.config.write_text(json.dumps({"enroll_code": "the-shared-code", "org": "acme", "device": "yard-09"}))
        self.token = self.dir / "device-token"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_enroll(self, worker: FakeWorker) -> bool:
        return enroll(self.config, self.token, http=worker, worker_base="https://worker.test/v1/", owner=None)

    def test_enrols_and_saves_the_token_privately(self) -> None:
        worker = FakeWorker(Response(201, {"token": TOKEN, "org": "acme", "device": "yard-09"}))
        with self.assertLogs("pi5-motion", "INFO"):
            self.assertTrue(self.run_enroll(worker))
        self.assertEqual(worker.calls, [
            ("POST", "https://worker.test/v1/enroll", {"enroll_code": "the-shared-code", "org": "acme", "device": "yard-09"}),
        ])
        self.assertEqual(self.token.read_text(), TOKEN + "\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.token).st_mode), 0o600)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".device-token")], [])

    def test_existing_token_skips_the_worker(self) -> None:
        self.token.write_text(TOKEN + "\n")
        worker = FakeWorker()
        with self.assertLogs("pi5-motion", "INFO"):
            self.assertTrue(self.run_enroll(worker))
        self.assertEqual(worker.calls, [])

    def test_already_enrolled_says_what_to_do_and_retries(self) -> None:
        with self.assertLogs("pi5-motion", "ERROR") as logs:
            self.assertFalse(self.run_enroll(FakeWorker(Response(409, {"error": "device already enrolled"}))))
        self.assertIn("_devices/acme/yard-09.json", logs.output[0])
        self.assertFalse(self.token.exists())

    def test_wrong_code(self) -> None:
        with self.assertLogs("pi5-motion", "ERROR") as logs:
            self.assertFalse(self.run_enroll(FakeWorker(Response(403, {"error": "wrong enrolment code"}))))
        self.assertIn("enrolment code", logs.output[0])
        self.assertNotIn("the-shared-code", "\n".join(logs.output))

    def test_offline_and_server_errors_retry(self) -> None:
        with self.assertLogs("pi5-motion", "WARNING"):
            self.assertFalse(self.run_enroll(FakeWorker(OSError("network is unreachable"))))
            self.assertFalse(self.run_enroll(FakeWorker(Response(503, {"error": "enrolment not configured"}))))
        self.assertFalse(self.token.exists())

    def test_refuses_a_token_for_another_device(self) -> None:
        other = "esd1.acme.other-01." + "x" * 43
        with self.assertLogs("pi5-motion", "ERROR"):
            self.assertFalse(self.run_enroll(FakeWorker(Response(201, {"token": other}))))
        self.assertFalse(self.token.exists())

    def test_without_config_or_token(self) -> None:
        self.config.unlink()
        with self.assertLogs("pi5-motion", "ERROR"):
            self.assertFalse(self.run_enroll(FakeWorker()))


if __name__ == "__main__":
    unittest.main()
