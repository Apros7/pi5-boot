#!/usr/bin/env python3
"""Record motion from the Pi's cameras and upload it every 15 minutes.

Runs on the Raspberry Pi 5 as easysort-motion.service. Every 0.25 s (4 fps)
it takes the newest frame from each USB camera. With more than one camera the
frames are put side by side into one picture, so a device still uploads one
mp4 per window. A cheap motion check (grey-level difference on a 64 px wide
copy of each camera's frame) decides what to keep: the frames from 2 s before
motion to 2 s after it. Frames without motion are dropped.

The kept frames of each UTC quarter hour become one mp4 plus a JSON sidecar
(windows.py), and upload.py sends both to R2 through the device-storage
Worker with the token the Pi got when it enrolled.

For the camera check page (preview.py) it also keeps a few small JPEGs in
/run/easysort-motion (RAM): each camera's newest frame every 5 s and the
last 5 motion frames.
"""

from __future__ import annotations

import argparse
import glob
import itertools
import json
import logging
import os
import signal
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import cv2
import numpy as np

log = logging.getLogger("pi5-motion")

CAPTURE_INTERVAL_S = 0.25
# Each camera is scaled down to at most this height before the frames are
# put side by side, so the window mp4 stays a sane size.
MAX_TILE_HEIGHT = 1080
DEFAULT_DATA = "/var/lib/easysort-motion"
SNAPSHOT_WIDTH = 960
SNAPSHOT_QUALITY = 70


@dataclass(frozen=True)
class MotionSettings:
    buffer_s: float = 10.0
    pre_s: float = 2.0
    post_s: float = 2.0
    max_clip_s: float = 120.0
    # Motion: fraction of downscaled pixels whose grey level changed by more
    # than pixel_delta since the previous frame.
    width: int = 64
    pixel_delta: int = 25
    min_fraction: float = 0.01


class MotionDetector:
    """Frame differencing on a small blurred grey copy."""

    def __init__(self, settings: MotionSettings) -> None:
        self.settings = settings
        self._previous: np.ndarray | None = None

    def update(self, image: np.ndarray) -> bool:
        """True when this frame differs enough from the previous one."""
        height, width = image.shape[:2]
        target_w = min(self.settings.width, width)
        target_h = max(1, round(height * target_w / width))
        grey = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(grey, (target_w, target_h), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (3, 3), 0)
        previous, self._previous = self._previous, small
        if previous is None or previous.shape != small.shape:
            return False
        changed = cv2.absdiff(small, previous) > self.settings.pixel_delta
        return float(np.count_nonzero(changed)) / changed.size >= self.settings.min_fraction


_clip_numbers = itertools.count()


@dataclass
class Clip:
    start_ts: float
    end_ts: float
    frames: int = 0
    motion_frames: int = 0
    # Unique in this run. windows.py groups frames by it; id() could be
    # reused once a finished clip is freed.
    number: int = field(default_factory=lambda: next(_clip_numbers))


class Sink(Protocol):
    def frame(self, clip: Clip, ts: float, jpeg: bytes, motion: bool = False) -> None: ...


@dataclass
class Stats:
    frames: int = 0
    motion_frames: int = 0
    kept_frames: int = 0
    clips: int = 0


@dataclass
class MotionFilter:
    """Keeps frames from pre_s before motion to post_s after it.

    Feed one composite frame at a time, in timestamp order, with a motion
    flag per camera. Motion in any camera keeps the whole composite.
    """

    sink: Sink
    settings: MotionSettings = MotionSettings()
    stats: Stats = field(default_factory=Stats)

    def __post_init__(self) -> None:
        self._buffer: deque[tuple[float, bytes]] = deque()
        self._clip: Clip | None = None
        self._keep_until = float("-inf")

    def process(self, ts: float, jpeg: bytes, motion: bool) -> bool:
        """Handle one frame. Returns True when it was kept."""
        settings = self.settings
        self.stats.frames += 1
        if motion:
            self.stats.motion_frames += 1
            self._keep_until = ts + settings.post_s
            if self._clip is None:
                self._open(ts)

        kept = False
        if self._clip is not None:
            if ts > self._keep_until:
                self._clip = None
            else:
                self._add(ts, jpeg, motion)
                kept = True
                if ts - self._clip.start_ts >= settings.max_clip_s:
                    # Long motion: carry on in a new clip so the sidecar
                    # shows separate events.
                    self._clip = Clip(start_ts=ts, end_ts=ts)
                    self.stats.clips += 1

        self._buffer.append((ts, jpeg))
        while self._buffer and ts - self._buffer[0][0] > settings.buffer_s:
            self._buffer.popleft()
        return kept

    def _open(self, ts: float) -> None:
        pre = [item for item in self._buffer if item[0] >= ts - self.settings.pre_s]
        start = pre[0][0] if pre else ts
        self._clip = Clip(start_ts=start, end_ts=start)
        self.stats.clips += 1
        for frame_ts, jpeg in pre:
            self._add(frame_ts, jpeg, False)

    def _add(self, ts: float, jpeg: bytes, motion: bool) -> None:
        assert self._clip is not None
        self._clip.end_ts = ts
        self._clip.frames += 1
        self._clip.motion_frames += int(motion)
        self.stats.kept_frames += 1
        self.sink.frame(self._clip, ts, jpeg, motion)


# --- Cameras -----------------------------------------------------------------


def find_cameras(pattern: str = "/dev/v4l/by-id/*-video-index0") -> list[str]:
    """USB cameras by stable id. The Pi 5's own ISP and codec nodes are not listed here."""
    found = sorted(glob.glob(pattern))
    if found:
        return found
    # Some cameras have no serial, so no by-id link; fall back to the USB port.
    return sorted(glob.glob("/dev/v4l/by-path/*usb*-video-index0"))


def open_camera(device: str) -> cv2.VideoCapture:
    """MJPG at the camera's largest size, as the fleet's capture.py does."""
    cap = cv2.VideoCapture(os.path.realpath(device), cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 10000)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 10000)
    # 4 fps is used; ask for few frames so decoding the rest costs little.
    cap.set(cv2.CAP_PROP_FPS, 5)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        raise OSError(f"could not open camera {device}")
    return cap


class CameraReader:
    """Reads one camera in its own thread and keeps only the newest frame."""

    def __init__(self, device: str, opener: Callable[[str], cv2.VideoCapture] = open_camera) -> None:
        self.device = device
        self.opener = opener
        self.size: tuple[int, int] | None = None  # width, height
        self._frame: np.ndarray | None = None
        self._at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"camera {device}", daemon=True)

    def start(self, timeout_s: float = 30) -> None:
        """Start reading and wait for the first frame, which fixes the size."""
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while self.size is None and time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.1)
        if self.size is None:
            raise OSError(f"no frame from camera {self.device} within {timeout_s:.0f} s")

    def latest(self, max_age_s: float = 2.0) -> np.ndarray | None:
        with self._lock:
            if self._frame is None or time.monotonic() - self._at > max_age_s:
                return None
            return self._frame

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        cap = None
        while not self._stop.is_set():
            if cap is None:
                try:
                    cap = self.opener(self.device)
                except OSError as error:
                    log.warning("%s; trying again in 5 s", error)
                    self._stop.wait(5)
                    continue
            ok, frame = cap.read()
            if not ok or frame is None:
                log.warning("Frame grab failed on %s, reopening", self.device)
                cap.release()
                cap = None
                self._stop.wait(1)
                continue
            with self._lock:
                self._frame, self._at = frame, time.monotonic()
                if self.size is None:
                    self.size = (frame.shape[1], frame.shape[0])
                    log.info("Camera %s at %dx%d", self.device, *self.size)
        if cap is not None:
            cap.release()


def tile_sizes(sizes: list[tuple[int, int]], max_height: int = MAX_TILE_HEIGHT) -> list[tuple[int, int]]:
    """Every camera at one common height (the smallest, at most max_height), widths even."""
    height = min([max_height, *(h for _, h in sizes)])
    height -= height % 2
    return [(max(2, round(w * height / h) // 2 * 2), height) for w, h in sizes]


def compose(frames: list[np.ndarray | None], tiles: list[tuple[int, int]]) -> np.ndarray:
    """Cameras side by side in a fixed layout; a camera without a frame is black."""
    parts = []
    for frame, (width, height) in zip(frames, tiles):
        if frame is None:
            parts.append(np.zeros((height, width, 3), dtype=np.uint8))
        elif (frame.shape[1], frame.shape[0]) != (width, height):
            parts.append(cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA))
        else:
            parts.append(frame)
    return parts[0] if len(parts) == 1 else np.hstack(parts)


def encode_jpeg(frame: np.ndarray, quality: int = 90) -> bytes:
    ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return data.tobytes()


# --- Snapshots for the camera check page ------------------------------------


def camera_name(device: str) -> str:
    """usb-046d_HD_Pro_Webcam_C920_AB12-video-index0 -> 046d_HD_Pro_Webcam_C920_AB12"""
    name = Path(device).name
    name = name.removeprefix("usb-").removesuffix("-video-index0")
    return name or device


class Snapshots:
    """Small JPEGs of what the cameras see, for preview.py.

    Each camera's newest frame every live_every_s (with whether that camera
    saw motion since the one before), and the frames of the
    cameras that saw motion, at most once every motion_every_s, keeping the
    newest `keep`. Images are at most 960 px wide at JPEG quality 70 (about
    100 KB). The folder is /run (RAM), so this costs no SD-card writes. Any
    error is logged once and never stops recording.
    """

    INDEX = "snapshots.json"

    def __init__(
        self,
        folder: Path,
        cameras: list[str],
        live_every_s: float = 1.0,
        motion_every_s: float = 10.0,
        keep: int = 5,
    ) -> None:
        self.folder = folder
        self.cameras = cameras
        self.live_every_s = live_every_s
        self.motion_every_s = motion_every_s
        self.keep = keep
        self.live: dict[int, list[dict]] = {}
        self.motion: list[dict] = []
        self.last_motion: float | None = None
        self._moved_since_live: dict[int, bool] = {}
        self._next_live = float("-inf")
        self._next_motion = float("-inf")
        self._warned = False
        try:
            self._load()
        except (OSError, ValueError) as error:
            self._warn(error)

    def update(self, ts: float, frames: list[np.ndarray | None], motion: list[bool]) -> None:
        try:
            self._update(ts, frames, motion)
        except (OSError, cv2.error, RuntimeError) as error:
            self._warn(error)

    def _warn(self, error: Exception) -> None:
        if not self._warned:
            self._warned = True
            log.warning("Camera check snapshots failed (recording is not affected): %s", error)

    def _load(self) -> None:
        """Keep the motion frames of an earlier run (the folder survives a restart); drop the rest."""
        self.folder.mkdir(parents=True, exist_ok=True)
        index = self.folder / self.INDEX
        if index.exists():
            self.motion = [
                entry for entry in json.loads(index.read_text()).get("motion", [])[-self.keep:]
                if (self.folder / entry["file"]).exists()
            ]
        kept = {entry["file"] for entry in self.motion}
        for path in self.folder.glob("*.jpg"):
            if path.name not in kept:
                path.unlink()

    def _update(self, ts: float, frames: list[np.ndarray | None], motion: list[bool]) -> None:
        changed = False
        for number, moved in enumerate(motion):
            if moved:
                self._moved_since_live[number] = True
                self.last_motion = ts
        if ts >= self._next_live:
            self._next_live = ts + self.live_every_s
            for number, frame in enumerate(frames):
                if frame is None:
                    continue
                entries = self.live.setdefault(number, [])
                entry = self._write("live", number, ts, frame)
                entry["motion"] = self._moved_since_live.pop(number, False)
                entries.append(entry)
                # A few older ones stay a little longer, for a page on a
                # slow 4G link that read the index just before this write.
                while len(entries) > 5:
                    self._remove(entries.pop(0))
                changed = True
        if any(motion) and ts >= self._next_motion:
            self._next_motion = ts + self.motion_every_s
            for number, (frame, moved) in enumerate(zip(frames, motion)):
                if moved and frame is not None:
                    self.motion.append(self._write("motion", number, ts, frame))
                    changed = True
            while len(self.motion) > self.keep:
                self._remove(self.motion.pop(0))
        if changed:
            from upload import write_atomic

            index = {
                "live": [entries[-1] for _, entries in sorted(self.live.items())],
                "motion": self.motion,
                "last_motion": self.last_motion,
            }
            write_atomic(self.folder / self.INDEX, json.dumps(index).encode())

    def _write(self, kind: str, number: int, ts: float, frame: np.ndarray) -> dict:
        from upload import write_atomic

        height, width = frame.shape[:2]
        if width > SNAPSHOT_WIDTH:
            size = (SNAPSHOT_WIDTH, max(1, round(height * SNAPSHOT_WIDTH / width)))
            frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        name = f"{kind}-{number}-{int(ts * 1000)}.jpg"
        write_atomic(self.folder / name, encode_jpeg(frame, SNAPSHOT_QUALITY))
        camera = self.cameras[number] if number < len(self.cameras) else f"camera {number}"
        return {"file": name, "camera": camera, "ts": ts}

    def _remove(self, entry: dict) -> None:
        (self.folder / entry["file"]).unlink(missing_ok=True)


# --- Service -----------------------------------------------------------------


def wait_for_clock(timeout_s: float = 300) -> None:
    """The Pi has no clock battery; window names need the NTP time."""
    import subprocess

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True, text=True
        )
        if result.stdout.strip() == "yes":
            return
        time.sleep(5)
    log.warning("Clock not synchronised after %ds; window names may be wrong", timeout_s)


def _raise_interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def start_cameras(devices: list[str]) -> list[CameraReader]:
    readers = []
    for device in devices:
        reader = CameraReader(device)
        try:
            reader.start()
        except OSError as error:
            log.error("%s; leaving it out", error)
            reader.stop()
            continue
        readers.append(reader)
    return readers


def run(args: argparse.Namespace) -> int:
    from upload import DeviceToken, Uploader, WORKER_URL
    from windows import WindowSink

    data = Path(args.data)
    settings = MotionSettings(
        pre_s=args.pre_s,
        post_s=args.post_s,
        pixel_delta=args.pixel_delta,
        min_fraction=args.min_fraction,
    )

    devices = args.camera or find_cameras()
    while not devices:
        log.warning("No USB camera found; looking again in 30 s")
        time.sleep(30)
        devices = find_cameras()
    readers = start_cameras(devices)
    if not readers:
        log.error("No camera gave a frame; systemd restarts the service")
        return 1
    tiles = tile_sizes([reader.size for reader in readers])  # type: ignore[misc]

    sink = WindowSink(
        data,
        source={
            "host": socket.gethostname(),
            "trigger": "motion",
            "cameras": [
                {"device": r.device, "size": list(r.size or ()), "tile": list(t)} for r, t in zip(readers, tiles)
            ],
            "pre_s": settings.pre_s,
            "post_s": settings.post_s,
            "pixel_delta": settings.pixel_delta,
            "min_fraction": settings.min_fraction,
        },
    )
    snapshots = None
    if args.snapshots:
        snapshots = Snapshots(Path(args.snapshots), [camera_name(reader.device) for reader in readers])
    stop_upload = threading.Event()
    if not args.no_upload:
        uploader = Uploader(
            data, DeviceToken(Path(args.device_token)), args.worker_url or WORKER_URL,
            status=Path(args.snapshots) / "upload.json" if args.snapshots else None,
        )
        threading.Thread(target=uploader.run_forever, args=(stop_upload,), daemon=True).start()

    wait_for_clock()
    sink.recover(time.time())
    detectors = [MotionDetector(settings) for _ in readers]
    motion_filter = MotionFilter(sink, settings)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    log.info("Recording motion from %d camera(s); windows queue in %s", len(readers), data / "queue")
    next_shot = time.monotonic()
    last_report = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now < next_shot:
                time.sleep(next_shot - now)
            next_shot = max(next_shot + CAPTURE_INTERVAL_S, time.monotonic())
            frames = [reader.latest() for reader in readers]
            if all(frame is None for frame in frames):
                continue
            moved = [frame is not None and detector.update(frame) for detector, frame in zip(detectors, frames)]
            motion = any(moved)
            # Every frame is encoded: the pre-roll buffer needs the frames
            # from before motion started.
            ts = time.time()
            motion_filter.process(ts, encode_jpeg(compose(frames, tiles)), motion)
            if snapshots is not None:
                snapshots.update(ts, frames, moved)
            sink.tick(ts)
            if time.monotonic() - last_report >= 300:
                last_report = time.monotonic()
                stats = motion_filter.stats
                log.info(
                    "frames=%d motion=%d kept=%d clips=%d",
                    stats.frames, stats.motion_frames, stats.kept_frames, stats.clips,
                )
    except KeyboardInterrupt:
        log.info("Stopping")
    finally:
        sink.stop()
        for reader in readers:
            reader.stop()
        stop_upload.set()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    defaults = MotionSettings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--camera", action="append", help="camera device (repeatable; default: every USB camera)")
    parser.add_argument("--data", default=DEFAULT_DATA, help="work and upload queue folder")
    parser.add_argument("--device-token", default="/etc/easysort/device-token", help="the token from enrolment")
    parser.add_argument("--worker-url", help="device-storage base URL (default: production Worker)")
    parser.add_argument("--snapshots", help="folder for the camera check page's images (default: none)")
    parser.add_argument("--no-upload", action="store_true", help="queue windows locally only")
    parser.add_argument("--pre-s", type=float, default=defaults.pre_s)
    parser.add_argument("--post-s", type=float, default=defaults.post_s)
    parser.add_argument("--pixel-delta", type=int, default=defaults.pixel_delta)
    parser.add_argument("--min-fraction", type=float, default=defaults.min_fraction)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
