import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter, TimedOut

import bot
import media
import uploads
from settings import Limits
from test_bot import context, update
from test_media import fake_probe


def audio_probe():
    return {"format": {"format_name": "mp3", "duration": "2"},
            "streams": [{"index": 0, "codec_type": "audio", "codec_name": "mp3"}]}


class AudioMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_is_in_existing_link_menu(self):
        ctx = context()
        request = update()
        await bot.handle_link(request, ctx)
        buttons = request.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual([button.callback_data.split(":")[0] for button in buttons], ["gif", "mp4", "audio"])
        self.assertEqual(len({button.callback_data.split(":")[1] for button in buttons}), 1)

    async def test_normal_messages_and_unlinked_attachments_have_no_menu(self):
        for text in ("hello", None):
            ctx = context()
            request = update()
            request.message.text = text
            await bot.handle_link(request, ctx)
            request.message.reply_text.assert_not_awaited()

    async def test_audio_button_uses_worker_and_audio_upload(self):
        ctx = context()
        await bot.handle_link(update(), ctx)
        state = ctx.application.bot_data["state"]
        request = update(key=next(iter(state.pending)))
        request.callback_query.data = request.callback_query.data.replace("mp4:", "audio:")
        request.callback_query.message.message_thread_id = 123
        async def prepare(url, mode, directory, limits, query, status=None):
            self.assertEqual(mode, "audio")
            output = Path(directory) / "output.mp3"
            output.write_bytes(b"audio")
            return output
        with patch.object(bot, "media_with_status", side_effect=prepare):
            await bot.handle_button(request, ctx)
        ctx.bot.send_audio.assert_awaited_once()
        ctx.bot.send_video.assert_not_awaited()
        ctx.bot.send_animation.assert_not_awaited()
        self.assertEqual(ctx.bot.send_audio.call_args.kwargs["message_thread_id"], 123)
        self.assertTrue(ctx.bot.send_audio.call_args.kwargs["audio"].input_file_content.closed)
        self.assertEqual(state.active, 0)

    async def test_audio_callback_retains_requester_restriction(self):
        ctx = context()
        await bot.handle_link(update(), ctx)
        state = ctx.application.bot_data["state"]
        key = next(iter(state.pending))
        request = update(user_id=99, key=key)
        request.callback_query.data = f"audio:{key}"
        with patch.object(bot, "media_with_status", new_callable=AsyncMock) as worker:
            await bot.handle_button(request, ctx)
        worker.assert_not_awaited()
        self.assertIn(key, state.pending)


class AudioProcessingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.budget = media.Budget(self.root, Limits())
        data = fake_probe()
        self.info = (data, data["streams"][0], 2)

    def test_mp3_conversion_maps_only_audio_and_validates_output(self):
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[0] == "ffprobe":
                return json.dumps(audio_probe()).encode()
            (self.root / "output.mp3").write_bytes(b"mp3")
            return b""
        with patch.object(self.budget, "command", side_effect=command):
            output = media.prepare_audio(self.root / "source.mp4", self.budget, self.info)
        self.assertEqual(output.suffix, ".mp3")
        self.assertIn("-vn", calls[0])
        self.assertEqual(calls[0][calls[0].index("-map") + 1], "0:1")
        self.assertIn("libmp3lame", calls[0])
        self.assertNotIn("-t", calls[0])  # Full audio, not the GIF's 30s trim.

    def test_silent_video_has_clear_error_before_conversion(self):
        data = fake_probe(audio=None)
        with patch.object(self.budget, "command") as command, self.assertRaises(media.MediaError) as caught:
            media.prepare_audio(self.root / "source.mp4", self.budget, (data, data["streams"][0], 2))
        self.assertEqual(caught.exception.code, "no_audio")
        command.assert_not_called()

    def test_audio_only_source_is_rejected(self):
        with patch.object(media, "download", return_value=self.root / "source.mp3"), \
                patch.object(media.Budget, "command", return_value=json.dumps(audio_probe()).encode()), \
                patch.object(media, "prepare_audio") as convert, self.assertRaises(media.MediaError) as caught:
            media.process_media("https://example.com/track.mp3", "audio", self.root, Limits())
        self.assertEqual(caught.exception.code, "invalid_media")
        convert.assert_not_called()

    def test_oversize_audio_gets_one_lower_bitrate_retry(self):
        budget = media.Budget(self.root, replace(Limits(), upload_bytes=20_000))
        encodes = []
        def command(args, **kwargs):
            if args[0] == "ffprobe":
                return json.dumps(audio_probe()).encode()
            encodes.append(args)
            (self.root / "output.mp3").write_bytes(b"x" * (20_001 if len(encodes) == 1 else 10_000))
            return b""
        with patch.object(budget, "command", side_effect=command):
            media.prepare_audio(self.root / "source.mp4", budget, self.info)
        self.assertEqual(len(encodes), 2)
        self.assertIn("192k", encodes[0])
        self.assertIn("64k", encodes[1])

    def test_still_oversize_audio_is_rejected(self):
        budget = media.Budget(self.root, replace(Limits(), upload_bytes=20_000))
        def command(args, **kwargs):
            (self.root / "output.mp3").write_bytes(b"x" * 20_001)
        with patch.object(budget, "command", side_effect=command) as command, self.assertRaises(media.MediaError) as caught:
            media.prepare_audio(self.root / "source.mp4", budget, self.info)
        self.assertEqual(caught.exception.code, "upload_limit")
        self.assertEqual(command.call_count, 2)

    def test_invalid_audio_output_is_rejected(self):
        for invalid in (fake_probe(), {**audio_probe(), "streams": [{"codec_type": "audio", "codec_name": "aac"}]}):
            def command(args, **kwargs):
                if args[0] == "ffprobe":
                    return json.dumps(invalid).encode()
                (self.root / "output.mp3").write_bytes(b"test")
            with patch.object(self.budget, "command", side_effect=command), self.assertRaises(media.MediaError):
                media.prepare_audio(self.root / "source.mp4", self.budget, self.info)


class AudioUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_retry_confirmation_and_stream_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.mp3"
            path.write_bytes(b"audio")
            streams = []
            async def send(**kwargs):
                streams.append(kwargs["audio"])
                self.assertEqual(kwargs["audio"].read(), b"audio")
                if len(streams) == 1:
                    raise RetryAfter(0)
                return NS(chat_id=42, message_id=1, audio=NS(file_id="audio"))
            client = NS(send_audio=AsyncMock(side_effect=send))
            await uploads.upload_media(client, path, "audio", 42, 1000)
            self.assertEqual(len(streams), 2)
            self.assertTrue(all(stream.closed for stream in streams))

    async def test_audio_timeout_is_reported_without_duplicate_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.mp3"
            path.write_bytes(b"audio")
            client = NS(send_audio=AsyncMock(side_effect=TimedOut()))
            with self.assertLogs("media_bot.uploads", level="WARNING"), self.assertRaises(uploads.UploadError) as caught:
                await uploads.upload_media(client, path, "audio", 42, 1000)
            self.assertEqual(caught.exception.code, "timeout")
            client.send_audio.assert_awaited_once()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe are not on PATH")
class AudioConversionIntegrationTests(unittest.TestCase):
    def test_real_video_to_mp3(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=size=160x120:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2",
                "-c:v", "mpeg4", "-c:a", "aac", str(source),
            ], check=True, timeout=30)
            budget = media.Budget(root, Limits())
            output = media.prepare_audio(source, budget, media.probe(source, budget))
            data, audio, duration = media.probe(output, budget, stream_type="audio")
            self.assertEqual(audio["codec_name"], "mp3")
            self.assertFalse(any(stream["codec_type"] == "video" for stream in data["streams"]))
            self.assertAlmostEqual(duration, 2, delta=0.1)
