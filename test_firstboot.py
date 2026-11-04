"""Runs the real firstboot.sh in a sandbox with stubbed system commands.

Checks the ordering that keeps recording independent of the network
services: the packages-ready marker that easysort-motion waits for is
written as soon as the packages are there, even when Tailscale and
enrolment fail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

STUBS = {
    # install [-d] [-m mode] [-o user] [-g group] src... dst, without changing owners.
    "install": """#!/bin/sh
dir=0
while [ $# -gt 0 ]; do
  case "$1" in
    -d) dir=1; shift ;;
    -m|-o|-g) shift 2 ;;
    *) break ;;
  esac
done
if [ $dir = 1 ]; then mkdir -p "$@"; else cp "$1" "$2"; fi
""",
    "dpkg-query": '#!/bin/sh\n[ "$PACKAGES_INSTALLED" = 1 ] && printf "install ok installed"\n',
    "apt-get": '#!/bin/sh\necho "apt-get $*" >> "$LOG"\nexit "$APT_RC"\n',
    "shred": '#!/bin/sh\nfor last; do :; done\nrm -f "$last"\n',
    "systemctl": "#!/bin/sh\nexit 0\n",
    "tailscale": '#!/bin/sh\necho "tailscale $*" >> "$LOG"\nexit "$TAILSCALE_RC"\n',
}

FAILING_ENROLL = "import sys\nsys.exit(1)\n"


class FirstbootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.boot, self.etc, self.app, self.bin = (root / name for name in ("boot", "etc", "app", "bin"))
        for folder in (self.boot, self.app, self.bin):
            folder.mkdir()
        for name, text in STUBS.items():
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        (self.boot / "settings.json").write_text(json.dumps({"hostname": "acme-yard-09"}))
        (self.boot / "tailscale-authkey").write_text("tskey-auth-TEST\n")
        (self.boot / "enroll.json").write_text("{}")
        (self.app / "enroll.py").write_text(FAILING_ENROLL)
        self.log = root / "log"
        self.log.touch()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_firstboot(self, packages_installed: bool, apt_rc: int = 0, tailscale_rc: int = 1) -> int:
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "EASYSORT_BOOT": str(self.boot),
            "EASYSORT_ETC": str(self.etc),
            "EASYSORT_APP": str(self.app),
            "PACKAGES_INSTALLED": "1" if packages_installed else "0",
            "APT_RC": str(apt_rc),
            "TAILSCALE_RC": str(tailscale_rc),
            "LOG": str(self.log),
        }
        result = subprocess.run(["sh", str(HERE / "firstboot.sh")], env=env, capture_output=True, text=True)
        return result.returncode

    def test_marker_written_even_when_tailscale_and_enrolment_fail(self) -> None:
        self.assertEqual(self.run_firstboot(packages_installed=False, apt_rc=0, tailscale_rc=1), 1)
        self.assertTrue((self.etc / "packages-ready").exists())
        log = self.log.read_text()
        self.assertIn("apt-get install", log)
        self.assertIn("tailscale up", log)
        # Not joined and not enrolled: both are kept for the retry.
        self.assertTrue((self.etc / "tailscale-authkey").exists())
        self.assertTrue((self.etc / "enroll.json").exists())
        self.assertFalse((self.boot / "enroll.json").exists())

    def test_no_marker_while_apt_fails(self) -> None:
        self.assertEqual(self.run_firstboot(packages_installed=False, apt_rc=100), 1)
        self.assertFalse((self.etc / "packages-ready").exists())

    def test_packages_already_there(self) -> None:
        self.run_firstboot(packages_installed=True)
        self.assertTrue((self.etc / "packages-ready").exists())
        self.assertNotIn("apt-get", self.log.read_text())

    def test_everything_done_exits_0(self) -> None:
        (self.etc).mkdir()
        (self.etc / "device-token").write_text("esd1.acme.yard-09." + "x" * 43 + "\n")
        self.assertEqual(self.run_firstboot(packages_installed=True, tailscale_rc=0), 0)
        self.assertFalse((self.etc / "tailscale-authkey").exists())
        self.assertFalse((self.etc / "enroll.json").exists())


class MotionUnitTests(unittest.TestCase):
    def test_motion_waits_only_for_packages(self) -> None:
        unit = (HERE / "easysort-motion.service").read_text()
        self.assertIn(
            "ExecStartPre=/bin/sh -c 'until [ -f /etc/easysort/packages-ready ]; do sleep 5; done'", unit
        )
        self.assertIn("TimeoutStartSec=infinity", unit)
        # Nothing may make recording depend on Tailscale, enrolment or firstboot succeeding.
        for line in unit.splitlines():
            if line.startswith(("Requires=", "BindsTo=", "Requisite=", "ExecStartPre=", "ConditionPathExists=")):
                for word in ("device-token", "tailscale", "firstboot"):
                    self.assertNotIn(word, line)


@unittest.skipUnless(shutil.which("sh"), "no sh")
class SyntaxTests(unittest.TestCase):
    def test_firstboot_is_valid_sh(self) -> None:
        subprocess.run(["sh", "-n", str(HERE / "firstboot.sh")], check=True)


if __name__ == "__main__":
    unittest.main()
