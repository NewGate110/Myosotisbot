"""Plain-text Telegram status cards and a rate-limited message editor."""

import asyncio
import contextlib
import json
import math
import re
import time
from pathlib import Path

from telegram.error import BadRequest, RetryAfter

PHASES = {
    "Starting": (0, "Checking link"),
    "Downloading": (0, "Downloading"),
    "Checking media": (1, "Checking media"),
    "Merging streams": (1, "Merging video and audio"),
    "Preparing MP4": (1, "Preparing MP4"),
    "Converting GIF": (1, "Creating GIF"),
    "Extracting audio": (1, "Extracting audio"),
    "Uploading": (2, "Uploading"),
    "Confirming": (2, "Waiting for Telegram"),
    "Retrying": (2, "Waiting to retry upload"),
    "Complete": (3, "Delivered"),
    "Failed": (None, "Request stopped"),
}


def number(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


def read_worker_progress(directory):
    root = Path(directory)
    try:
        data = json.loads((root / "progress.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("phase") not in PHASES:
            return None
    except (OSError, ValueError):
        return None
    name, duration = data.get("ffmpeg"), number(data.get("duration"))
    if isinstance(name, str) and re.fullmatch(r"ffmpeg-\d+\.progress", name) and duration:
        try:
            with (root / name).open("rb") as stream:
                stream.seek(max(0, (root / name).stat().st_size - 4096))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            values = dict(line.split("=", 1) for line in lines if "=" in line)
            processed = max(0, float(values["out_time_us"]) / 1_000_000)
            if math.isfinite(processed):
                data.update(done=min(processed, duration), total=duration, unit="seconds")
        except (OSError, ValueError, KeyError):
            pass
    return data


def render_status(mode, data, elapsed, error=None):
    phase = "Failed" if error else data.get("phase", "Starting")
    _, icon, title = PHASES.get(phase, PHASES["Starting"])
    lines = [f"{icon} {title} · { {'mp4': 'MP4', 'gif': 'GIF', 'audio': 'MP3'}.get(mode, 'Media')}"]
    done, total = number(data.get("done")), number(data.get("total"))
    if phase == "Complete":
        lines.append("#" * 10 + " 100%")
    elif error:
        lines.append(error)
    elif done is not None and total:
        fraction = min(done / total, 1)
        blocks = min(10, int(fraction * 10))
        lines.append("#" * blocks + "-" * (10 - blocks) + f" {int(fraction * 100)}%")
    else:
        position = int(elapsed / 2) % 8
        lines.append("-" * position + "###" + "-" * (7 - position) + " Working…")
    return "\n".join(lines)


class TelegramProgress:
    def __init__(self, query, mode):
        self.query, self.mode = query, mode
        self.data = {"phase": "Starting"}
        self.started = time.monotonic()
        self.next_edit = 0
        self.last_text = None
        self.task = None

    def text(self, error=None):
        return render_status(self.mode, self.data, time.monotonic() - self.started, error)

    async def edit(self, text):
        if time.monotonic() < max(self.next_edit, self.data.get("pause_until", 0)):
            return False
        if text == self.last_text:
            return True
        try:
            await asyncio.wait_for(self.query.edit_message_text(text), timeout=10)
            self.last_text = text
            self.next_edit = time.monotonic() + 3
            return True
        except RetryAfter as exc:
            delay = exc.retry_after
            delay = delay.total_seconds() if hasattr(delay, "total_seconds") else delay
            self.next_edit = time.monotonic() + max(3, float(delay))
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                self.last_text = text
            self.next_edit = time.monotonic() + 6
        except Exception:
            self.next_edit = time.monotonic() + 6
        return False

    async def __aenter__(self):
        async def ticker():
            while True:
                await self.edit(self.text())
                await asyncio.sleep(1)
        self.task = asyncio.create_task(ticker())
        return self

    async def __aexit__(self, *args):
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task

    async def complete(self):
        # Called after verified delivery and after the ticker has stopped.
        try:
            await asyncio.wait_for(self.query.delete_message(), timeout=15)
            return True
        except Exception:
            # A cleanup failure must not turn a successful upload into an error.
            return False
