"""Fuse the kept frames of each 15-minute window into one mp4 and a JSON sidecar.

The motion filter hands over only the frames around motion. They are piped
straight into ffmpeg, so a 15-minute window becomes one short mp4 that plays
the kept frames back to back at 4 fps. The sidecar lists the real UTC time of
every frame in the mp4, whether it had motion, and the clips they came from.

A window without kept frames writes nothing. While a window is open its
frames go to ``work/<window start>/part-NNN.mp4`` plus ``part-NNN.jsonl``. A
restart inside the window starts a new part, and a power cut leaves a
fragmented mp4 that is still readable up to its last fragment. When the
window ends (or on the next start, if it ended while the service was down)
the parts are joined into ``queue/<YYYY-MM-DD>/<HHMM>.mp4`` and the sidecar is
written next to it last, so a ``.json`` in the queue means the window is
complete and ready to upload.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("pi5-motion")

WINDOW_S = 15 * 60
PLAYBACK_FPS = 4
# The upload Worker refuses bodies over 95 MiB; stop adding frames before that.
MAX_WINDOW_BYTES = 90 * 1024 * 1024
SIDECAR_VERSION = 1


def window_start(ts: float) -> int:
    return int(ts // WINDOW_S * WINDOW_S)


def window_key(start: int) -> tuple[str, str]:
    """(YYYY-MM-DD, HHMM) of the window's UTC start."""
    when = datetime.fromtimestamp(start, timezone.utc)
    return when.strftime("%Y-%m-%d"), when.strftime("%H%M")


def iso(ts: float) -> str:
    when = datetime.fromtimestamp(ts, timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def part_command(path: Path) -> list[str]:
    """JPEGs on stdin to an mp4 that stays readable if the Pi loses power."""
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(PLAYBACK_FPS), "-i", "-",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
        # A keyframe every 5 s, each starting a fragment.
        "-g", str(PLAYBACK_FPS * 5),
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        str(path),
    ]


def join_command(list_file: Path, out: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart", str(out),
    ]


def count_frames(path: Path, run: Callable = subprocess.run) -> int:
    """Frames ffmpeg can read back from a part; 0 if it is unreadable."""
    result = run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except ValueError:
        return 0


@dataclass
class _Part:
    index: int
    process: subprocess.Popen
    frames_file: object
    frames: int = 0


@dataclass
class _Window:
    start: int
    folder: Path
    part: _Part | None = None
    clip_ids: dict[int, int] = field(default_factory=dict)
    truncated: bool = False


class WindowSink:
    """ClipSink that fuses each window's kept frames into one mp4."""

    def __init__(
        self,
        data_dir: Path,
        source: dict | None = None,
        popen: Callable = subprocess.Popen,
        run: Callable = subprocess.run,
    ) -> None:
        self.work = data_dir / "work"
        self.queue = data_dir / "queue"
        self.work.mkdir(parents=True, exist_ok=True)
        self.queue.mkdir(parents=True, exist_ok=True)
        self.source = source or {}
        self.popen = popen
        self.run = run
        self._window: _Window | None = None
        self._next_scan = 0.0

    # ClipSink

    def open(self, clip) -> None:
        pass

    def frame(self, clip, ts: float, jpeg: bytes, motion: bool = False) -> None:
        start = window_start(ts)
        if self._window is not None and self._window.start != start:
            self._finish_window()
        if self._window is None:
            self._window = self._begin(start)
        window = self._window
        if window.truncated:
            return
        if self._bytes(window) + len(jpeg) > MAX_WINDOW_BYTES:
            log.warning("Window %s is full; dropping its remaining frames", window.folder.name)
            window.truncated = True
            (window.folder / "truncated").touch()
            return
        if window.part is None:
            window.part = self._start_part(window)
        part = window.part
        clip_id = window.clip_ids.setdefault(clip.number, len(window.clip_ids))
        part.process.stdin.write(jpeg)
        part.frames_file.write(
            json.dumps({"t": round(ts, 3), "motion": motion, "clip": f"{part.index}-{clip_id}"}) + "\n"
        )
        part.frames_file.flush()
        part.frames += 1

    def close(self, clip) -> None:
        pass

    # Lifecycle

    def tick(self, now: float) -> None:
        """Finish every window whose 15 minutes are over.

        That includes a window an earlier run started: after a restart inside
        a window, its parts wait in work/ even if no new frame is kept.
        """
        if self._window is not None and now >= self._window.start + WINDOW_S:
            self._finish_window()
        if now >= self._next_scan:
            self._next_scan = now + 10
            self.recover(now)

    def recover(self, now: float) -> None:
        """Finish ended windows that are not open in this run."""
        current = self._window.start if self._window is not None else None
        for folder in sorted(self.work.iterdir()):
            if not folder.is_dir() or not folder.name.isdigit():
                continue
            start = int(folder.name)
            if start != current and now >= start + WINDOW_S:
                self.finalize(folder)

    def stop(self) -> None:
        """Close the current part without ending the window (service stop)."""
        if self._window is not None:
            self._close_part(self._window)
            self._window = None

    # Internals

    def _begin(self, start: int) -> _Window:
        folder = self.work / str(start)
        folder.mkdir(exist_ok=True)
        return _Window(start=start, folder=folder, truncated=(folder / "truncated").exists())

    def _bytes(self, window: _Window) -> int:
        return sum(path.stat().st_size for path in window.folder.glob("part-*.mp4"))

    def _start_part(self, window: _Window) -> _Part:
        index = len(list(window.folder.glob("part-*.jsonl")))
        path = window.folder / f"part-{index:03d}.mp4"
        # ffmpeg errors go to the service journal. Its own session keeps a
        # SIGTERM or Ctrl-C for the service away from it: on stop, the part
        # is ended by closing stdin, so ffmpeg finishes the file cleanly.
        process = self.popen(part_command(path), stdin=subprocess.PIPE, start_new_session=True)
        frames_file = (window.folder / f"part-{index:03d}.jsonl").open("a", encoding="utf-8")
        return _Part(index=index, process=process, frames_file=frames_file)

    def _close_part(self, window: _Window) -> None:
        part, window.part = window.part, None
        if part is None:
            return
        part.frames_file.close()
        part.process.stdin.close()
        try:
            part.process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            part.process.kill()
            part.process.wait()
        if part.process.returncode != 0:
            log.error("ffmpeg exited %s for %s part %d", part.process.returncode, window.folder.name, part.index)

    def _finish_window(self) -> None:
        window, self._window = self._window, None
        self._close_part(window)
        self.finalize(window.folder)

    def finalize(self, folder: Path) -> Path | None:
        """Join a window's parts into the queue. Returns the queued mp4."""
        start = int(folder.name)
        frames: list[dict] = []
        parts: list[Path] = []
        incomplete = False
        for jsonl in sorted(folder.glob("part-*.jsonl")):
            mp4 = jsonl.with_suffix(".mp4")
            lines = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
            readable = count_frames(mp4, self.run) if mp4.exists() else 0
            if readable < len(lines):
                # Power was cut while this part was written.
                incomplete = True
                lines = lines[:readable]
            if lines:
                parts.append(mp4)
                frames.extend(lines)
        if not frames:
            shutil.rmtree(folder, ignore_errors=True)
            return None

        date, hhmm = window_key(start)
        target_dir = self.queue / date
        target_dir.mkdir(parents=True, exist_ok=True)
        mp4_out, json_out = target_dir / f"{hhmm}.mp4", target_dir / f"{hhmm}.json"
        if json_out.exists():
            log.error("%s is already queued; keeping %s for inspection", json_out, folder)
            return None
        with tempfile.NamedTemporaryFile("w", suffix=".txt", dir=folder, delete=False) as handle:
            for part in parts:
                handle.write(f"file '{part}'\n")
            list_file = Path(handle.name)
        partial = target_dir / f".{hhmm}.partial.mp4"
        result = self.run(join_command(list_file, partial), capture_output=True, text=True)
        if result.returncode != 0:
            log.error("Could not join %s: %s", folder, (result.stderr or "")[-1000:])
            partial.unlink(missing_ok=True)
            return None
        os.replace(partial, mp4_out)
        sidecar = self.sidecar(start, frames, incomplete, (folder / "truncated").exists())
        tmp_json = target_dir / f".{hhmm}.json.partial"
        tmp_json.write_text(json.dumps(sidecar, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp_json, json_out)
        shutil.rmtree(folder, ignore_errors=True)
        log.info("Queued %s/%s: %d frames in %d clips", date, hhmm, len(frames), len(sidecar["clips"]))
        return mp4_out

    def sidecar(self, start: int, frames: list[dict], incomplete: bool, truncated: bool) -> dict:
        clip_index: dict[str, int] = {}
        clips: list[dict] = []
        out_frames = []
        for line in frames:
            index = clip_index.get(line["clip"])
            if index is None:
                index = clip_index[line["clip"]] = len(clips)
                clips.append({"start": iso(line["t"]), "end": iso(line["t"]), "frames": 0, "motion_frames": 0})
            clip = clips[index]
            clip["end"] = iso(line["t"])
            clip["frames"] += 1
            clip["motion_frames"] += int(line["motion"])
            out_frames.append({"t": iso(line["t"]), "motion": line["motion"], "clip": index})
        return {
            "version": SIDECAR_VERSION,
            "window_start": iso(start),
            "window_end": iso(start + WINDOW_S),
            "fps": PLAYBACK_FPS,
            "frame_count": len(out_frames),
            "motion_frames": sum(int(f["motion"]) for f in out_frames),
            "incomplete": incomplete,
            "truncated": truncated,
            "source": self.source,
            "clips": clips,
            "frames": out_frames,
        }
