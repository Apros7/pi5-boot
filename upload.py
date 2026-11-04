"""Upload queued windows to R2 through the Easysort device-storage Worker.

Contract:

    PUT https://worker.easysort.org/device-storage/v1/reuse-detection/
        <org>/<device>/<YYYY-MM-DD>/<HHMM>.(mp4|json)
    Authorization: Bearer <the device token from enrolment>
    Content-Type: video/mp4 | application/json
    X-Content-SHA256: <64 lowercase hex>

201 stored. 409 already there, with the stored sha256: a match means an
earlier upload got through. 401 read the token file again and try once more
(401 "revoked" means an admin revoked the device). 429, 5xx and 503 retry
later. 400, 403, 411, 413 and 415 will not
change on retry, so the file is parked in ``queue/parked/``.

The Pi enrols itself on first boot (enroll.py) and keeps its token in
``/etc/easysort/device-token``. The token is ``esd1.<org>.<device>.<random>``,
so it also gives the upload path. Until the token exists, windows stay in
the local queue.

The mp4 goes up first and the sidecar last, so a ``.json`` in the bucket
means the window is complete. When the queue grows past the disk cap the
oldest windows are deleted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("pi5-motion")

WORKER_BASE = "https://worker.easysort.org/device-storage/v1"
WORKER_URL = f"{WORKER_BASE}/reuse-detection"
DEVICE_TOKEN = Path("/etc/easysort/device-token")
TOKEN_RE = re.compile(r"^esd1\.([a-z0-9][a-z0-9-]{0,62})\.([a-z0-9][a-z0-9-]{0,62})\.[A-Za-z0-9_-]{43}$")
MAX_UPLOAD_BYTES = 99_614_720
DEFAULT_CAP_BYTES = 8 * 1024**3
MIN_FREE_BYTES = 2 * 1024**3
RETRY_MIN_S = 30
RETRY_MAX_S = 15 * 60
CONTENT_TYPES = {".mp4": "video/mp4", ".json": "application/json"}
PARK = {400, 403, 411, 413, 415}
# Cloudflare's bot check answers the default Python-urllib agent with 403 1010.
USER_AGENT = "easysort-edge/1"


@dataclass
class Response:
    status: int
    body: dict
    retry_after: float | None = None


def http_request(
    method: str, url: str, headers: dict[str, str], body: bytes | Path | None = None, timeout: float = 300
) -> Response:
    """One HTTP call; a body Path is streamed from disk."""
    handle = body.open("rb") if isinstance(body, Path) else None
    try:
        headers = {"User-Agent": USER_AGENT, **headers}
        request = urllib.request.Request(url, data=handle or body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return Response(reply.status, _json(reply.read()))
        except urllib.error.HTTPError as error:
            retry_after = error.headers.get("Retry-After")
            return Response(
                error.code,
                _json(error.read()),
                float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
    finally:
        if handle is not None:
            handle.close()


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw[:200].decode("utf-8", "replace")}
    return value if isinstance(value, dict) else {"value": value}


class LoginError(RuntimeError):
    pass


class DeviceToken:
    """The token the Pi got when it enrolled. The organization and device are part of it."""

    def __init__(self, path: Path = DEVICE_TOKEN) -> None:
        self.path = path
        self._token = ""
        self.organization = ""
        self.device = ""

    def available(self) -> bool:
        return self.path.exists()

    def token(self) -> str:
        if not self._token:
            value = self.path.read_text(encoding="utf-8").strip()
            match = TOKEN_RE.fullmatch(value)
            if not match:
                raise LoginError(f"{self.path} does not hold a device token")
            self._token = value
            self.organization, self.device = match.group(1), match.group(2)
        return self._token

    def renew(self) -> None:
        """Read the file again on the next upload (a new enrolment may have replaced it)."""
        self._token = ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Uploader:
    def __init__(
        self,
        data_dir: Path,
        login: DeviceToken,
        worker_url: str = WORKER_URL,
        http: Callable = http_request,
        cap_bytes: int = DEFAULT_CAP_BYTES,
        free_bytes: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
    ) -> None:
        self.queue = data_dir / "queue"
        self.parked = self.queue / "parked"
        self.queue.mkdir(parents=True, exist_ok=True)
        self.login = login
        self.worker_url = worker_url.rstrip("/")
        self.http = http
        self.cap_bytes = cap_bytes
        self.free_bytes = free_bytes
        self.uploaded = 0

    def windows(self) -> list[Path]:
        """Complete windows (sidecar written), oldest first, as their .json path."""
        return sorted(
            path for path in self.queue.glob("*/*.json") if path.parent.name != "parked"
        )

    def run_once(self) -> float:
        """Upload what is queued. Returns seconds to wait before the next try (0: queue empty)."""
        self.enforce_cap()
        if not self.windows():
            return 0
        if not self.login.available():
            return 60.0
        for sidecar in self.windows():
            mp4 = sidecar.with_suffix(".mp4")
            for path in (mp4, sidecar):
                if not path.exists():
                    continue  # the mp4 already went up in an earlier run
                outcome = self.upload(path)
                if isinstance(outcome, float):
                    return outcome
                if outcome == "park":
                    self.park(sidecar)
                    break
                if path is mp4:
                    path.unlink()
            else:
                sidecar.unlink()
                self.uploaded += 1
            if sidecar.parent.exists() and not any(sidecar.parent.iterdir()):
                sidecar.parent.rmdir()
        return 0

    def upload(self, path: Path) -> str | float:
        """"done", "park", or seconds until a retry."""
        size = path.stat().st_size
        key = f"{path.parent.name}/{path.name}"
        if size > MAX_UPLOAD_BYTES:
            log.error("%s is %d bytes, over the upload limit", key, size)
            return "park"
        digest = sha256_file(path)
        for attempt in range(2):
            try:
                token = self.login.token()
            except (LoginError, OSError) as error:
                log.warning("No device login: %s", error)
                return float(RETRY_MIN_S * 4)
            url = f"{self.worker_url}/{self.login.organization}/{self.login.device}/{key}"
            try:
                reply = self.http(
                    "PUT",
                    url,
                    {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": CONTENT_TYPES[path.suffix],
                        "Content-Length": str(size),
                        "X-Content-SHA256": digest,
                    },
                    path,
                )
            except OSError as error:
                log.warning("Upload of %s failed: %s", key, error)
                return float(RETRY_MIN_S)
            if reply.status == 201:
                log.info("Uploaded %s: 201 %s", key, json.dumps(reply.body))
                return "done"
            if reply.status == 409:
                if reply.body.get("sha256") == digest:
                    log.info("%s was already uploaded", key)
                    return "done"
                log.error("%s exists in R2 with different content", key)
                return "park"
            if reply.status == 401 and attempt == 0:
                self.login.renew()
                continue
            if reply.status in PARK:
                log.error("Upload of %s refused: %s %s", key, reply.status, reply.body)
                return "park"
            log.warning("Upload of %s: %s %s; retrying later", key, reply.status, reply.body)
            return float(reply.retry_after or RETRY_MIN_S)
        return float(RETRY_MIN_S)

    def park(self, sidecar: Path) -> None:
        target = self.parked / sidecar.parent.name
        target.mkdir(parents=True, exist_ok=True)
        for path in (sidecar.with_suffix(".mp4"), sidecar):
            if path.exists():
                path.replace(target / path.name)

    def queued_files(self) -> list[Path]:
        # Parked or not, the folder is the window's date, so this is oldest first.
        return sorted(
            (path for path in self.queue.rglob("*") if path.is_file()),
            key=lambda path: (path.parent.name, path.name),
        )

    def enforce_cap(self) -> None:
        """Delete the oldest windows while the queue is over the cap or the disk is nearly full."""
        files = self.queued_files()
        total = sum(path.stat().st_size for path in files)
        while files and (total > self.cap_bytes or self.free_bytes(self.queue) < MIN_FREE_BYTES):
            oldest = files[0]
            stem = oldest.with_suffix("")
            for path in [p for p in files if p.with_suffix("") == stem]:
                total -= path.stat().st_size
                path.unlink()
                files.remove(path)
            log.warning("Queue over its cap; deleted %s", stem.relative_to(self.queue))

    def run_forever(self, stop: threading.Event) -> None:
        failures = 0
        while not stop.is_set():
            wait = self.run_once()
            if wait:
                failures += 1
                wait = min(RETRY_MAX_S, max(wait, RETRY_MIN_S * 2 ** (failures - 1)))
            else:
                failures, wait = 0, 10
            stop.wait(wait)
