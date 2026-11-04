#!/usr/bin/env python3
"""Enrol this Pi with the Easysort Worker and save its device token.

Run as root by firstboot.sh at every boot until it succeeds:

    python3 enroll.py --config /etc/easysort/enroll.json --token /etc/easysort/device-token

enroll.json is written by flash.py: {"enroll_code", "org", "device"}. The
Worker answers 201 with a token for this device, once. It is written to the
token file (mode 0600, owned by the user the motion service runs as) before
anything else happens, so a crash cannot lose it. firstboot.sh then deletes
enroll.json, because the enrolment code is shared by every device.

Exit 0 when the token file exists (now or already), 1 otherwise, so systemd
retries. A 409 (the device name is already enrolled) and a 403 (wrong code)
are logged clearly: an admin has to act, and the retry picks it up after.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pwd
import sys
import tempfile
from pathlib import Path
from typing import Callable

from upload import TOKEN_RE, WORKER_BASE, http_request

log = logging.getLogger("pi5-motion")

CONFIG = Path("/etc/easysort/enroll.json")
TOKEN = Path("/etc/easysort/device-token")
OWNER = "easysort"


def save_token(path: Path, token: str, owner: str | None = OWNER) -> None:
    """Write the token atomically, readable only by the motion service's user."""
    fd, tmp = tempfile.mkstemp(prefix=".device-token.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if owner:
            user = pwd.getpwnam(owner)
            os.fchown(fd, user.pw_uid, user.pw_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def enroll(
    config_path: Path,
    token_path: Path,
    http: Callable = http_request,
    worker_base: str = WORKER_BASE,
    owner: str | None = OWNER,
) -> bool:
    """True when the device has a token."""
    if token_path.exists() and token_path.read_text(encoding="utf-8").strip():
        log.info("Already enrolled (%s exists)", token_path)
        return True
    if not config_path.exists():
        log.error("No %s and no device token: write a new card with flash.py", config_path)
        return False
    config = json.loads(config_path.read_text(encoding="utf-8"))
    org, device = config.get("org", ""), config.get("device", "")
    body = json.dumps({"enroll_code": config.get("enroll_code", ""), "org": org, "device": device}).encode()
    try:
        reply = http(
            "POST", f"{worker_base.rstrip('/')}/enroll",
            {"Content-Type": "application/json", "Content-Length": str(len(body))}, body, 60,
        )
    except OSError as error:
        log.warning("Could not reach the Worker to enrol %s/%s: %s", org, device, error)
        return False
    if reply.status == 201:
        token = str(reply.body.get("token", ""))
        match = TOKEN_RE.fullmatch(token)
        if not match or match.groups() != (org, device):
            log.error("The Worker returned a token that is not for %s/%s", org, device)
            return False
        save_token(token_path, token, owner)
        log.info("Enrolled as %s/%s", org, device)
        return True
    if reply.status == 409:
        log.error(
            "%s/%s is already enrolled. Choose another device name and write a new card, or ask an admin to "
            "delete _devices/%s/%s.json if this Pi replaces it (retrying)", org, device, org, device,
        )
    elif reply.status == 403:
        log.error("The Worker refused the enrolment code (retrying; write a new card with the right code)")
    else:
        log.warning("Enrolment answered %s %s; retrying", reply.status, reply.body)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--token", type=Path, default=TOKEN)
    parser.add_argument("--worker-base", default=WORKER_BASE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    return 0 if enroll(args.config, args.token, worker_base=args.worker_base) else 1


if __name__ == "__main__":
    raise SystemExit(main())
