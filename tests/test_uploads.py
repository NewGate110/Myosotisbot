import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, ChatMigrated, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut

import bot
import uploads
from test_bot import context, update


class UploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "video.mp4"
        self.path.write_bytes(b"complete media")
        self.confirmation = NS(message_id=10, chat_id=-10042, video=NS(file_id="video"))
        self.client = NS(send_video=AsyncMock(return_value=self.confirmation),
                         send_animation=AsyncMock(return_value=NS(message_id=11, chat_id=-10042, animation=NS(file_id="gif"))))

    async def send(self, mode="mp4", **kwargs):
        return await uploads.upload_media(self.client, self.path, mode, -10042, 1000, **kwargs)

    async def test_success_requires_confirmation_and_closes_stream(self):
        self.assertIs(await self.send(thread_id=123), self.confirmation)
        arguments = self.client.send_video.call_args.kwargs
        self.assertTrue(arguments["video"].closed)
        self.assertEqual(arguments["message_thread_id"], 123)
        self.assertTrue(arguments["supports_streaming"])

    async def test_animation_success(self):
        result = await self.send("gif")
        self.assertEqual(result.animation.file_id, "gif")
        self.client.send_video.assert_not_awaited()
        self.assertTrue(self.client.send_animation.call_args.kwargs["animation"].closed)

    async def test_rate_limit_retries_with_fresh_stream(self):
        streams = []
        async def upload(**kwargs):
            stream = kwargs["video"]
            streams.append(stream)
            self.assertEqual(stream.read(), b"complete media")
            if len(streams) == 1:
                raise RetryAfter(0)
            return self.confirmation
        self.client.send_video.side_effect = upload
        await self.send()
        self.assertEqual(len(streams), 2)
        self.assertIsNot(streams[0], streams[1])
        self.assertTrue(all(stream.closed for stream in streams))

    async def test_timedelta_rate_limit_delay_is_supported(self):
        with patch.dict(os.environ, {"PTB_TIMEDELTA": "1"}):
            self.client.send_video.side_effect = [RetryAfter(timedelta(seconds=0)), self.confirmation]
            await self.send()
        self.assertEqual(self.client.send_video.await_count, 2)

    async def test_rate_limit_retry_count_is_bounded(self):
        self.client.send_video.side_effect = RetryAfter(0)
        with self.assertLogs("media_bot.uploads", level="WARNING"), self.assertRaises(uploads.UploadError) as caught:
            await self.send()
        self.assertEqual(caught.exception.code, "rate_limit")
        self.assertEqual(self.client.send_video.await_count, 2)

    async def test_long_rate_limit_is_not_waited_or_retried(self):
        self.client.send_video.side_effect = RetryAfter(10000)
        with self.assertLogs("media_bot.uploads", level="WARNING"), self.assertRaises(uploads.UploadError) as caught:
            await self.send()
        self.assertEqual(caught.exception.code, "rate_limit")
        self.client.send_video.assert_awaited_once()

    async def test_api_failures_are_classified_without_unsafe_retries(self):
        cases = ((TimedOut(), "timeout"), (NetworkError("secret URL"), "network"),
                 (Forbidden("secret URL"), "forbidden"), (InvalidToken(), "token"),
                 (ChatMigrated(-10099), "migrated"), (BadRequest("FILE_IS_TOO_BIG"), "size"),
                 (BadRequest("file is too big"), "size"), (BadRequest("request entity too large"), "size"),
                 (NetworkError("Request Entity Too Large (413)"), "size"),
                 (BadRequest("can't parse video"), "rejected"))
        for exc, expected in cases:
            with self.subTest(error=type(exc).__name__, expected=expected):
                self.client.send_video.reset_mock()
                self.client.send_video.side_effect = exc
                with self.assertLogs("media_bot.uploads", level="WARNING") as logs, self.assertRaises(uploads.UploadError) as caught:
                    await self.send()
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("secret", str(logs.output))
                self.client.send_video.assert_awaited_once()
                self.assertTrue(self.client.send_video.call_args.kwargs["video"].closed)

    async def test_overall_timeout_cancels_send_and_closes_stream(self):
        async def stall(**kwargs):
            await asyncio.Event().wait()
        self.client.send_video.side_effect = stall
        with patch.object(uploads, "UPLOAD_TIMEOUT", 0.02), self.assertLogs("media_bot.uploads", level="WARNING"), \
                self.assertRaises(uploads.UploadError) as caught:
            await self.send()
        self.assertEqual(caught.exception.code, "timeout")
        self.client.send_video.assert_awaited_once()
        self.assertTrue(self.client.send_video.call_args.kwargs["video"].closed)

    async def test_missing_empty_and_oversize_files_never_reach_telegram(self):
        for content, code in ((None, "file"), (b"", "file"), (b"x" * 1001, "size")):
            with self.subTest(code=code, content_size=len(content) if content is not None else None):
                if content is None:
                    self.path.unlink()
                else:
                    self.path.write_bytes(content)
                with self.assertLogs("media_bot.uploads", level="WARNING"), self.assertRaises(uploads.UploadError) as caught:
                    await self.send()
                self.assertEqual(caught.exception.code, code)
        self.client.send_video.assert_not_awaited()

    async def test_unconfirmed_responses_are_not_retried(self):
        for response in (None, NS(message_id=1, chat_id=-10042),
                         NS(message_id=1, chat_id=-10099, video=NS(file_id="video"))):
            self.client.send_video.reset_mock()
            self.client.send_video.return_value = response
            with self.assertLogs("media_bot.uploads", level="WARNING"), self.assertRaises(uploads.UploadError) as caught:
                await self.send()
            self.assertEqual(caught.exception.code, "unconfirmed")
            self.client.send_video.assert_awaited_once()

    async def test_cancellation_propagates_and_closes_stream(self):
        self.client.send_video.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.send()
        self.assertTrue(self.client.send_video.call_args.kwargs["video"].closed)


class UploadHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_keeps_status_cleans_files_and_releases_slot(self):
        ctx = context()
        await bot.handle_link(update(), ctx)
        state = ctx.application.bot_data["state"]
        request = update(key=next(iter(state.pending)))
        paths = []
        async def prepare(url, mode, directory, limits, query, status=None):
            path = Path(directory) / "output.mp4"
            path.write_bytes(b"test")
            paths.append(path)
            return path
        ctx.bot.send_video.side_effect = Forbidden("not enough rights")
        with patch.object(bot, "media_with_status", side_effect=prepare), self.assertLogs("media_bot.uploads", level="WARNING"):
            await bot.handle_button(request, ctx)
        self.assertEqual(state.active, 0)
        self.assertFalse(paths[0].exists())
        request.callback_query.delete_message.assert_not_awaited()
        self.assertIn("Telegram denied", request.callback_query.edit_message_text.call_args.args[0])

    async def test_deleted_status_uses_same_chat_and_topic(self):
        ctx = context()
        query = update().callback_query
        query.message.message_thread_id = 123
        query.edit_message_text.side_effect = BadRequest("message to edit not found")
        with self.assertLogs("media_bot", level="WARNING"):
            await bot.report_upload_error(query, ctx, -10042, uploads.UploadError("rejected"))
        ctx.bot.send_message.assert_awaited_once_with(
            chat_id=-10042, message_thread_id=123, text=uploads.MESSAGES["rejected"])

    async def test_notification_failure_is_logged_without_throwing(self):
        ctx = context()
        query = update().callback_query
        query.edit_message_text.side_effect = Forbidden("blocked")
        ctx.bot.send_message.side_effect = Forbidden("blocked")
        with self.assertLogs("media_bot", level="WARNING") as logs:
            await bot.report_upload_error(query, ctx, -10042, uploads.UploadError("forbidden"))
        self.assertTrue(any("Could not deliver the upload error" in line for line in logs.output))

    async def test_failed_status_deletion_does_not_report_upload_failure(self):
        ctx = context()
        await bot.handle_link(update(), ctx)
        state = ctx.application.bot_data["state"]
        request = update(key=next(iter(state.pending)))
        request.callback_query.delete_message.side_effect = BadRequest("already deleted")
        with patch.object(bot, "media_with_status", new_callable=AsyncMock), \
                patch.object(bot, "upload_media", new_callable=AsyncMock), \
                patch.object(bot, "report_upload_error", new_callable=AsyncMock) as report:
            await bot.handle_button(request, ctx)
        report.assert_not_awaited()
        self.assertEqual(state.active, 0)
