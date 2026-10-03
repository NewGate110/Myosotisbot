import asyncio
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import bot
from media import MediaError
from process_control import stop_worker
from settings import Limits

PROJECT = Path(bot.__file__).parent


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopping_worker_kills_descendants(self):
        # The grandchild inherits stdout. EOF proves it died as well as its parent.
        script = (
            "import subprocess, sys, time; "
            "from process_control import contain_windows_worker; "
            "job = contain_windows_worker(); "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print('ready', flush=True); time.sleep(60)"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, cwd=PROJECT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        try:
            ready = await asyncio.wait_for(process.stdout.readline(), 5)
            if ready != b"ready\r\n" and ready != b"ready\n":
                self.fail((await process.stderr.read()).decode())
            await asyncio.wait_for(stop_worker(process), 5)
            self.assertEqual(await asyncio.wait_for(process.stdout.read(), 5), b"")
        finally:
            if process.returncode is None:
                await stop_worker(process)

    async def run_fake_worker(self, body, limits, expected_code=None, mode="mp4"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "media.py"
            worker.write_text(
                "import json, os, sys, time\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(PROJECT)!r})\n"
                "from process_control import contain_windows_worker\n"
                "job = contain_windows_worker()\n"
                "limits = json.loads(sys.stdin.buffer.read())\n"
                "root = Path(sys.argv[3])\n" + body,
                encoding="utf-8",
            )
            directory = root / "job"
            directory.mkdir()
            with patch.object(bot, "__file__", str(root / "bot.py")):
                if expected_code:
                    with self.assertRaises(MediaError) as caught:
                        await asyncio.wait_for(bot.run_media_worker("url", mode, directory, limits, {}), 10)
                    self.assertEqual(caught.exception.code, expected_code)
                else:
                    result = await bot.run_media_worker("url", mode, directory, limits, {})
                    self.assertEqual(result.read_bytes(), b"test")

    async def test_audio_worker_returns_mp3_output(self):
        await self.run_fake_worker(
            "(root / 'output.mp3').write_bytes(b'test')\nprint(json.dumps({'ok': True}), flush=True)\n",
            Limits(), mode="audio",
        )

    async def test_worker_deadline_stops_process(self):
        await self.run_fake_worker("time.sleep(60)\n", replace(Limits(), job_seconds=1), "timeout")

    async def test_worker_temp_limit_stops_process(self):
        await self.run_fake_worker("(root / 'large').write_bytes(b'x' * 1000)\ntime.sleep(60)\n",
                                   replace(Limits(), temp_bytes=100), "temp_limit")

    async def test_worker_source_limit_stops_process(self):
        await self.run_fake_worker("(root / 'source').mkdir()\n(root / 'source/file').write_bytes(b'x' * 1000)\ntime.sleep(60)\n",
                                   replace(Limits(), download_bytes=100), "download_limit")

    async def test_worker_gets_no_telegram_credentials(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "FAKE_SECRET"}):
            await self.run_fake_worker(
                "assert not any(k.startswith('TELEGRAM_') for k in os.environ)\n"
                "(root / 'output.mp4').write_bytes(b'test')\nprint(json.dumps({'ok': True}), flush=True)\n",
                Limits(),
            )

    async def test_parent_checks_output_size(self):
        await self.run_fake_worker(
            "(root / 'output.mp4').write_bytes(b'0123456789')\nprint(json.dumps({'ok': True}), flush=True)\n",
            replace(Limits(), upload_bytes=5), "upload_limit",
        )

    async def test_cancellation_waits_for_worker_exit(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(bot.asyncio, "create_subprocess_exec", return_value=process):
            task = asyncio.create_task(bot.run_media_worker("url", "mp4", tmp, Limits(), {}))
            try:
                await asyncio.sleep(0.1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                self.assertIsNotNone(process.returncode)
            finally:
                if process.returncode is None:
                    await stop_worker(process)
