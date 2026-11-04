#!/usr/bin/env python3
"""Camera check page: the cameras' newest frames and the Pi's health.

Runs as easysort-preview.service and listens on 127.0.0.1:8080 only.
firstboot.sh shares it on the tailnet with `tailscale serve`, as
https://<hostname>.<tailnet>.ts.net/, so it is never on the LAN, Wi-Fi or 4G
side. Without that: `ssh -L 8080:127.0.0.1:8080 easysort@<hostname>` and open
http://127.0.0.1:8080/.

Built for a 4G link: no auto refresh, no scripts, no video. Reload the page
to look again. The page is a few KB and never cached; each image is about
100 KB and cached by the browser (its name changes when it does).

Read-only. It serves only the JPEGs motion_capture.py writes to
/run/easysort-motion, and reads the upload queue's size; nothing secret is
shown. Recording does not depend on this service.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

RUN_DIR = Path("/run/easysort-motion")
DATA_DIR = Path("/var/lib/easysort-motion")
MOTION_UNIT = "easysort-motion.service"
IMAGE_RE = re.compile(r"^/img/((?:live|motion)-[0-9]+-[0-9]+\.jpg)$")
LIVE_STALE_S = 30

THROTTLE_BITS = {
    0: "under-voltage now",
    1: "CPU speed capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage since boot",
    17: "CPU speed capped since boot",
    18: "throttled since boot",
    19: "soft temperature limit since boot",
}

Runner = Callable[..., subprocess.CompletedProcess]


def decode_throttled(value: int) -> str:
    words = [text for bit, text in THROTTLE_BITS.items() if value & (1 << bit)]
    return "OK" if not words else ", ".join(words)


def throttled(run: Runner = subprocess.run) -> str:
    try:
        out = run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
        value = int(out.strip().split("=", 1)[1], 16)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return "unknown (vcgencmd failed)"
    return f"{decode_throttled(value)} ({value:#x})"


def cpu_temperature(path: Path = Path("/sys/class/thermal/thermal_zone0/temp")) -> str:
    try:
        return f"{int(path.read_text()) / 1000:.1f} °C"
    except (OSError, ValueError):
        return "unknown"


def default_routes(path: Path = Path("/proc/net/route")) -> list[tuple[str, int]]:
    """(interface, metric) of every default route, preferred first."""
    routes = []
    try:
        lines = path.read_text().splitlines()[1:]
    except OSError:
        return []
    for line in lines:
        fields = line.split()
        if len(fields) >= 8 and fields[1] == "00000000" and fields[7] == "00000000":
            routes.append((fields[0], int(fields[6])))
    return sorted(routes, key=lambda route: route[1])


def interface_kind(name: str) -> str:
    if name.startswith("wl"):
        return "Wi-Fi"
    if name == "eth0":
        return "Ethernet"
    if name.startswith(("eth", "usb", "wwan", "enx")):
        return "4G dongle"
    return "other"


def network(path: Path = Path("/proc/net/route")) -> str:
    routes = default_routes(path)
    if not routes:
        return "no default route (offline?)"
    (name, _), rest = routes[0], routes[1:]
    text = f"{name} ({interface_kind(name)})"
    if rest:
        text += "; standby: " + ", ".join(f"{n} ({interface_kind(n)})" for n, _ in rest)
    return text


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} s"
    return f"{seconds} s"


def service_state(unit: str = MOTION_UNIT, run: Runner = subprocess.run) -> str:
    try:
        out = run(
            ["systemctl", "show", unit, "--property=ActiveState,SubState,ActiveEnterTimestampMonotonic,NRestarts"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown (systemctl failed)"
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    state = f"{props.get('ActiveState', '?')} ({props.get('SubState', '?')})"
    since = int(props.get("ActiveEnterTimestampMonotonic", "0") or 0)
    if props.get("ActiveState") == "active" and since:
        state += f" for {duration(time.monotonic() - since / 1e6)}"
    restarts = props.get("NRestarts", "")
    if restarts not in ("", "0"):
        state += f", restarted {restarts} times since boot"
    return state


def queue_size(queue: Path) -> str:
    windows = parked = 0
    total = 0
    try:
        for path in queue.rglob("*"):
            if not path.is_file():
                continue
            total += path.stat().st_size
            if path.suffix == ".json":
                if "parked" in path.relative_to(queue).parts:
                    parked += 1
                else:
                    windows += 1
    except OSError:
        return "unknown"
    text = f"{windows} window(s) waiting, {total / 1e6:.1f} MB"
    if parked:
        text += f"; {parked} parked (refused by the Worker, see the journal)"
    return text


def when(ts: float, now: float) -> str:
    local = datetime.fromtimestamp(ts).astimezone()
    utc = datetime.fromtimestamp(ts, timezone.utc)
    return (
        f"{local:%Y-%m-%d %H:%M:%S} {local.tzname()} / {utc:%H:%M:%S} UTC"
        f" ({duration(now - ts)} ago)"
    )


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def figure(entry: dict, now: float, note: str = "") -> str:
    name = str(entry.get("file", ""))
    if not IMAGE_RE.match(f"/img/{name}"):
        return ""
    camera = html.escape(str(entry.get("camera", "")))
    caption = f"{camera}: {when(float(entry.get('ts', 0)), now)}{note}"
    return f'<figure><img src="/img/{name}" alt="{camera}"><figcaption>{caption}</figcaption></figure>'


def render_page(
    run_dir: Path = RUN_DIR,
    data_dir: Path = DATA_DIR,
    now: float | None = None,
    run: Runner = subprocess.run,
    route_file: Path = Path("/proc/net/route"),
    temp_file: Path = Path("/sys/class/thermal/thermal_zone0/temp"),
) -> str:
    now = time.time() if now is None else now
    host = socket.gethostname()
    index = read_json(run_dir / "snapshots.json")
    upload = read_json(run_dir / "upload.json")

    if upload:
        last_upload = (
            f"{html.escape(str(upload.get('result', '')))} for {html.escape(str(upload.get('file') or '-'))}, "
            f"{when(float(upload.get('at', 0)), now)}"
        )
    else:
        last_upload = "no upload tried since the service started"
    try:
        uptime = duration(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        uptime = "unknown"
    rows = [
        ("Recording service", service_state(run=run)),
        ("Last upload", last_upload),
        ("Upload queue", queue_size(data_dir / "queue")),
        ("CPU temperature", cpu_temperature(temp_file)),
        ("Power / throttling", throttled(run)),
        ("Network", network(route_file)),
        ("Pi uptime", uptime),
    ]
    table = "".join(f"<tr><th>{name}</th><td>{value}</td></tr>" for name, value in rows)

    live = []
    for entry in index.get("live", []):
        stale = now - float(entry.get("ts", 0)) > LIVE_STALE_S
        live.append(figure(entry, now, " <b>(old: is the recording service running?)</b>" if stale else ""))
    motion = [figure(entry, now) for entry in reversed(index.get("motion", []))]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(host)} cameras</title>
<style>
body{{font:14px/1.4 system-ui,sans-serif;margin:16px;max-width:960px}}
img{{width:100%;height:auto;display:block;background:#ccc}}
figure{{margin:0 0 16px}} figcaption{{color:#555}}
th{{text-align:left;padding:2px 12px 2px 0;vertical-align:top;white-space:nowrap}}
</style></head><body>
<h1>{html.escape(host)}</h1>
<p>As of {when(now, now).rsplit(" (", 1)[0]}. No auto refresh: <a href="/">reload</a> to look again.</p>
<table>{table}</table>
<h2>Live</h2>
{"".join(live) or "<p>No frame yet (the recording service writes one every 5 s).</p>"}
<h2>Last motion</h2>
{"".join(motion) or "<p>No motion seen since the service started.</p>"}
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "easysort-preview"
    sys_version = ""
    run_dir = RUN_DIR
    data_dir = DATA_DIR

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            try:
                body = render_page(self.run_dir, self.data_dir).encode()
            except Exception as error:  # the page must not take the server down
                self.send_text(500, f"error: {error}")
                return
            self.send(200, "text/html; charset=utf-8", body, "no-store")
            return
        match = IMAGE_RE.match(path)
        if match:
            try:
                body = (self.run_dir / match.group(1)).read_bytes()
            except OSError:
                self.send_text(404, "image gone; reload the page")
                return
            # The name holds the time, so the content never changes.
            self.send(200, "image/jpeg", body, "private, max-age=86400, immutable")
            return
        self.send_text(404, "not found")

    def send_text(self, status: int, text: str) -> None:
        self.send(status, "text/plain; charset=utf-8", text.encode(), "no-store")

    def send(self, status: int, content_type: str, body: bytes, cache: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep the journal quiet


def make_server(bind: str, port: int, run_dir: Path, data_dir: Path) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"run_dir": run_dir, "data_dir": data_dir})
    return ThreadingHTTPServer((bind, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default="127.0.0.1", help="address to listen on (default: loopback only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--run-dir", type=Path, default=RUN_DIR)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)
    server = make_server(args.bind, args.port, args.run_dir, args.data)
    print(f"Camera check page on http://{args.bind}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
