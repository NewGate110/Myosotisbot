import io
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
from settings import ConfigurationError, Limits, Settings


def update(chat_id=-10042, user_id=42, kind="supergroup", key=None):
    message = NS(text="https://x.com/test/status/123", message_id=5,
                 message_thread_id=None, reply_text=AsyncMock(return_value=NS(message_id=5, edit_text=AsyncMock())))
    query = NS(data=f"mp4:{key}", message=message, answer=AsyncMock(),
               edit_message_text=AsyncMock(), delete_message=AsyncMock())
    return NS(effective_chat=NS(id=chat_id, type=kind), effective_user=NS(id=user_id, is_bot=False),
              message=message, callback_query=query)


def context():
    state = bot.BotState(Settings("000:FAKE_TOKEN", 42, -10042, Limits()))
    return NS(application=NS(bot_data={"state": state}),
              bot=NS(send_video=AsyncMock(return_value=NS(message_id=10, chat_id=-10042, video=NS(file_id="video"))),
                     send_animation=AsyncMock(return_value=NS(message_id=10, chat_id=-10042, animation=NS(file_id="gif"))),
                     send_audio=AsyncMock(return_value=NS(message_id=10, chat_id=-10042, audio=NS(file_id="audio"))),
                     send_message=AsyncMock()))


class ConfigTests(unittest.TestCase):
    def test_missing_or_invalid_identity_fails_closed(self):
        for values in ({}, {"TELEGRAM_OWNER_USER_ID": "42"},
                       {"TELEGRAM_OWNER_USER_ID": "42", "TELEGRAM_ALLOWED_GROUP_ID": "42"},
                       {"TELEGRAM_OWNER_USER_ID": "-42", "TELEGRAM_ALLOWED_GROUP_ID": "-10042"}):
            with self.subTest(values=values), patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake", **values}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env()

    def test_bad_resource_limits_rejected(self):
        for key, value in (("MAX_JOB_SECONDS", "0"), ("MAX_DOWNLOAD_BYTES", "oops"), ("MAX_UPLOAD_BYTES", "50000000")):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Limits.from_env()

    def test_config_and_import_need_no_real_token(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_OWNER_USER_ID": "42", "TELEGRAM_ALLOWED_GROUP_ID": "-10042"}, clear=True):
            self.assertEqual(Settings.from_env().group_id, -10042)

    def test_missing_ffmpeg_fails_before_application_is_created(self):
        with patch.object(bot, "load_dotenv"), patch.object(bot, "configure_logging"), \
                patch.object(bot.Settings, "from_env", return_value=Settings("fake", 42, -10042, Limits())), \
                patch.object(bot.shutil, "which", return_value=None), patch.object(bot, "Application") as app:
            with self.assertRaisesRegex(SystemExit, "ffprobe"):
                bot.main()
            app.builder.assert_not_called()


class LoggingTests(unittest.TestCase):
    def test_token_removed_from_args_urls_and_tracebacks(self):
        token = "000:FAKE_TOKEN"
        output = io.StringIO()
        logger = logging.Logger("test-redaction")
        handler = logging.StreamHandler(output)
        handler.setFormatter(bot.RedactingFormatter(token))
        logger.addHandler(handler)
        logger.warning("Request %s", f"https://api.telegram.org/bot{token}/sendVideo")
        logger.warning("Encoded %s", "000%3AFAKE_TOKEN")
        try:
            raise ValueError(token)
        except ValueError:
            logger.exception("Failed %s", token)
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn("000%3AFAKE_TOKEN", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())

    def test_http_logging_is_suppressed(self):
        with patch.object(logging, "basicConfig"):
            bot.configure_logging("fake")
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_personal_upload_size_validation_reset_and_access(self):
        ctx = context()
        state = ctx.application.bot_data["state"]
        ctx.args = ["20"]
        await bot.handle_upload_size(update(user_id=43), ctx)
        self.assertEqual(state.upload_preferences, {43: 20_000_000})
        for args in (["0"], ["-1"], ["46"], ["20.5"], ["bad"], ["20", "30"]):
            ctx.args = args
            await bot.handle_upload_size(update(user_id=43), ctx)
            self.assertEqual(state.upload_preferences, {43: 20_000_000})
        ctx.args = []
        request = update(user_id=43)
        await bot.handle_upload_size(request, ctx)
        self.assertIn("20 MB", request.message.reply_text.call_args.args[0])
        ctx.args = ["10"]
        denied = update(99, 99, "private")
        await bot.handle_upload_size(denied, ctx)
        denied.message.reply_text.assert_not_awaited()
        self.assertNotIn(99, state.upload_preferences)
        ctx.args = ["reset"]
        await bot.handle_upload_size(update(user_id=43), ctx)
        self.assertEqual(state.upload_preferences, {})

    async def test_personal_limit_reaches_conversion_and_upload_for_all_formats(self):
        for mode in ("mp4", "gif", "audio"):
            ctx = context()
            state = ctx.application.bot_data["state"]
            state.upload_preferences[42] = 20_000_000
            key = "a" * 32
            state.pending[key] = bot.PendingLink("url", -10042, 42, time.monotonic(), 5)
            request = update(key=key)
            request.callback_query.data = f"{mode}:{key}"
            with patch.object(bot, "media_with_status", new_callable=AsyncMock) as worker, \
                    patch.object(bot, "upload_media", new_callable=AsyncMock) as upload:
                await bot.handle_button(request, ctx)
            self.assertEqual(worker.call_args.args[3].upload_bytes, 20_000_000)
            self.assertEqual(upload.call_args.args[4], 20_000_000)
            self.assertEqual(state.settings.limits.upload_bytes, 45_000_000)

    async def test_processing_reply_precedes_delayed_menu(self):
        ctx = context()
        request = update()
        reply = NS(message_id=4, edit_text=AsyncMock(), delete=AsyncMock())
        menu = NS(message_id=5)
        request.message.reply_text.side_effect = [reply, menu]

        async def check_delay(seconds):
            self.assertEqual(seconds, 1.5)
            request.message.reply_text.assert_awaited_once_with("processing..", do_quote=True)
            reply.edit_text.assert_not_awaited()

        with patch.object(bot.asyncio, "sleep", side_effect=check_delay):
            await bot.handle_link(request, ctx)

        reply.edit_text.assert_not_awaited()
        reply.delete.assert_not_awaited()
        self.assertEqual(request.message.reply_text.await_count, 2)
        self.assertEqual(request.message.reply_text.call_args.args[0], "Got it. Send as:")
        state = ctx.application.bot_data["state"]
        self.assertEqual(next(iter(state.pending.values())).message_id, menu.message_id)

    def test_access_matrix(self):
        settings = context().application.bot_data["state"].settings
        cases = [(42, 42, "private", True), (43, 43, "private", False),
                 (-10042, 43, "supergroup", True), (-10099, 42, "supergroup", False),
                 (-10042, 42, "channel", False), (-10042, 42, "group", True)]
        for chat, user, kind, expected in cases:
            with self.subTest(chat=chat, user=user, kind=kind):
                self.assertEqual(bot.authorized(update(chat, user, kind), settings), expected)

    async def test_unauthorized_messages_and_buttons_do_nothing(self):
        ctx = context()
        request = update(99, 99, "private")
        with patch.object(bot, "media_with_status", new_callable=AsyncMock) as worker:
            await bot.handle_link(request, ctx)
            await bot.handle_button(request, ctx)
            worker.assert_not_awaited()
        request.message.reply_text.assert_not_awaited()
        request.callback_query.answer.assert_not_awaited()
        self.assertFalse(ctx.application.bot_data["state"].pending)

    async def test_foreign_button_does_not_consume_pending_request(self):
        ctx = context()
        state = ctx.application.bot_data["state"]
        key = "a" * 32
        state.pending[key] = bot.PendingLink("url", -10042, 42, time.monotonic(), 5)
        request = update(user_id=99, key=key)
        with patch.object(bot, "media_with_status", new_callable=AsyncMock) as worker:
            await bot.handle_button(request, ctx)
            worker.assert_not_awaited()
        self.assertIn(key, state.pending)
        self.assertIn("belongs", request.callback_query.answer.call_args.args[0])

    async def test_busy_button_can_be_retried(self):
        ctx = context()
        state = ctx.application.bot_data["state"]
        key = "a" * 32
        state.pending[key] = bot.PendingLink("url", -10042, 42, time.monotonic(), 5)
        state.active = 1
        request = update(key=key)
        await bot.handle_button(request, ctx)
        self.assertIn(key, state.pending)
        self.assertIn("Busy", request.callback_query.answer.call_args.args[0])

    async def test_successful_upload_closes_file_and_releases_slot(self):
        ctx = context()
        await bot.handle_link(update(), ctx)
        state = ctx.application.bot_data["state"]
        key = next(iter(state.pending))
        request = update(key=key)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.mp4"
            output.write_bytes(b"test")
            with patch.object(bot, "media_with_status", new_callable=AsyncMock, return_value=output):
                await bot.handle_button(request, ctx)
        self.assertEqual(state.active, 0)
        self.assertFalse(state.pending)
        ctx.bot.send_video.assert_awaited_once()
        self.assertTrue(ctx.bot.send_video.call_args.kwargs["video"].input_file_content.closed)
        request.callback_query.delete_message.assert_awaited_once()

    async def test_errors_are_safe_and_slots_are_released(self):
        ctx = context()
        await bot.handle_link(update(), ctx)
        state = ctx.application.bot_data["state"]
        key = next(iter(state.pending))
        request = update(key=key)
        with patch.object(bot, "media_with_status", new_callable=AsyncMock, side_effect=ValueError("secret raw error")), \
                patch.object(bot.log, "exception"):
            await bot.handle_button(request, ctx)
        self.assertEqual(state.active, 0)
        self.assertNotIn("secret", str(request.callback_query.edit_message_text.call_args_list))

    def test_expiry_and_url_host_check(self):
        state = context().application.bot_data["state"]
        state.pending["old"] = bot.PendingLink("url", 1, 1, time.monotonic() - 1000)
        state.expire()
        self.assertFalse(state.pending)
        url = "https://youtube.com/watch?v=abc&ref=https://x.com/u/status/123"
        self.assertEqual(bot.normalize_url(url), url)
