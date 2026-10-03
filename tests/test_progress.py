import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx
from telegram import InputFile
from telegram.error import RetryAfter

from media import Budget
from progress import TelegramProgress, read_worker_progress, render_status
from settings import Limits
from uploads import upload_media


class RenderingTests(unittest.TestCase):
    def test_download_card_only_shows_title_and_bar(self):
        text = render_status("mp4", {"phase": "Downloading", "done": 12_400_000,
            "total": 20_000_000, "speed": 2_100_000, "eta": 4, "unit": "bytes", "stream": True}, 18)
        self.assertEqual(text, "📥 Downloading · MP4\n######---- 62%")

    def test_unknown_total_never_invents_a_percentage(self):
        text = render_status("gif", {"phase": "Converting GIF"}, 7)
        self.assertIn("Working…", text)
        self.assertNotIn("%", text)
        self.assertEqual(len(text.splitlines()), 2)

    def test_upload_finishing_is_not_delivery_confirmation(self):
        text = render_status("audio", {"phase": "Confirming", "done": 100, "total": 100, "unit": "bytes"}, 65)
        self.assertIn("Waiting for Telegram", text)
        self.assertNotIn("Delivered", text)
        self.assertEqual(len(text.splitlines()), 2)
        complete = render_status("audio", {"phase": "Complete"}, 66)
        self.assertIn("Delivered · MP3", complete)
        self.assertEqual(len(complete.splitlines()), 2)

    def test_error_replaces_success_and_progress(self):
        text = render_status("mp4", {"phase": "Uploading", "done": 10, "total": 10}, 8, error="Upload timed out")
        self.assertIn("Request stopped", text)
        self.assertIn("Upload timed out", text)
        self.assertNotIn("100%", text)

    def test_corrupt_numeric_values_do_not_break_rendering(self):
        text = render_status("gif", {"phase": "Downloading", "done": float("nan"), "total": "bad", "eta": float("inf")}, 2)
        self.assertIn("Working…", text)


class WorkerProgressTests(unittest.TestCase):
    def test_hook_writes_atomic_progress_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = Budget(tmp, Limits())
            budget.hook({"filename": "source.mp4", "downloaded_bytes": 100, "total_bytes": 200, "speed": 10, "eta": 10})
            snapshot = read_worker_progress(tmp)
            self.assertEqual(snapshot["phase"], "Downloading")
            self.assertEqual(snapshot["done"], 100)
            self.assertEqual(snapshot["total"], 200)
            self.assertFalse((Path(tmp) / "progress.tmp").exists())

    def test_ffmpeg_processed_duration_becomes_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "progress.json").write_text(json.dumps({"phase": "Preparing MP4", "duration": 10, "ffmpeg": "ffmpeg-1.progress"}))
            (root / "ffmpeg-1.progress").write_text("out_time_us=2000000\nprogress=continue\nout_time_us=5000000\nprogress=continue\n")
            snapshot = read_worker_progress(tmp)
            self.assertEqual(snapshot["done"], 5)
            self.assertIn("50%", render_status("mp4", snapshot, 7))

    def test_missing_or_partial_status_does_not_stop_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_worker_progress(tmp))
            (Path(tmp) / "progress.json").write_text("{")
            self.assertIsNone(read_worker_progress(tmp))

    def test_new_conversion_attempt_does_not_reuse_old_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = Budget(tmp, Limits())
            budget.phase("Extracting audio", duration=10)
            with patch("media.subprocess.run", return_value=NS(stdout=b"")):
                budget.command(["ffmpeg", "-i", "source", "output"])
                first = read_worker_progress(tmp)["ffmpeg"]
                budget.command(["ffmpeg", "-i", "source", "output"])
                second = read_worker_progress(tmp)["ffmpeg"]
            self.assertNotEqual(first, second)


class LiveProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_is_lazy_and_multipart_reads_update_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.mp4"
            content = b"x" * 200_000
            path.write_bytes(content)
            progress = {}
            observed = []
            async def send_video(**kwargs):
                upload = kwargs["video"]
                self.assertIsInstance(upload, InputFile)
                self.assertEqual(progress["done"], 0)  # No eager read disguised as progress.
                request = httpx.Request("POST", "https://example.com/upload", files={"video": upload.field_tuple})
                for _ in request.stream:
                    observed.append(progress["done"])
                    await asyncio.sleep(0)
                self.assertEqual(progress["phase"], "Confirming")
                self.assertEqual(progress["done"], len(content))
                return NS(message_id=1, chat_id=42, video=NS(file_id="confirmed"))
            client = NS(send_video=AsyncMock(side_effect=send_video))
            await upload_media(client, path, "mp4", 42, 1_000_000, progress=progress)
            self.assertTrue(any(0 < done < len(content) for done in observed))
            self.assertTrue(client.send_video.call_args.kwargs["video"].input_file_content.closed)

    async def test_message_updates_are_throttled_and_retry_after_is_respected(self):
        query = NS(edit_message_text=AsyncMock())
        reporter = TelegramProgress(query, "mp4")
        await reporter.edit("one")
        await reporter.edit("two")
        query.edit_message_text.assert_awaited_once()
        reporter.next_edit = 0
        query.edit_message_text.side_effect = RetryAfter(60)
        await reporter.edit("two")
        self.assertGreater(reporter.next_edit, time.monotonic() + 59)
        await reporter.edit("three")
        self.assertEqual(query.edit_message_text.await_count, 2)

    async def test_ticker_stops_before_final_message(self):
        query = NS(edit_message_text=AsyncMock(), delete_message=AsyncMock())
        reporter = TelegramProgress(query, "gif")
        async with reporter:
            await asyncio.sleep(0.01)
        self.assertTrue(reporter.task.done())
        reporter.next_edit = 0
        self.assertTrue(await reporter.complete())
        query.delete_message.assert_awaited_once()
