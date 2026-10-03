import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import media
from settings import Limits


def fake_probe(codec="h264", pixel="yuv420p", audio="aac", duration="2"):
    streams = [{"index": 0, "codec_type": "video", "codec_name": codec, "pix_fmt": pixel}]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": audio})
    return {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": duration}, "streams": streams}


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.budget = media.Budget(self.root, Limits())

    def test_metadata_rejects_live_playlist_duration_and_size(self):
        cases = [({"entries": []}, "playlist"), ({"is_live": True}, "live"),
                 ({"duration": 601}, "duration"),
                 ({"requested_formats": [{"filesize": 120_000_000}, {"filesize": 100_000_000}]}, "download_limit")]
        for info, code in cases:
            with self.subTest(code=code), self.assertRaises(media.MediaError) as caught:
                media.validate_metadata(info, self.budget)
            self.assertEqual(caught.exception.code, code)

    def test_download_budget_combines_streams_and_retries(self):
        budget = media.Budget(self.root, replace(Limits(), download_bytes=100))
        budget.hook({"filename": "video", "downloaded_bytes": 60})
        budget.hook({"filename": "video", "downloaded_bytes": 60})
        budget.hook({"filename": "audio", "downloaded_bytes": 30})
        with self.assertRaises(media.MediaError):
            budget.hook({"filename": "audio", "downloaded_bytes": 20})
        self.assertEqual(budget.violation, "download_limit")

    def test_limit_wrapped_by_ytdlp_does_not_fall_back(self):
        def failed(*args):
            self.budget.violation = "download_limit"
            raise media.yt_dlp.utils.DownloadError("wrapped")
        with patch.object(media, "download_ytdlp", side_effect=failed), patch.object(media, "gallery_video_url") as fallback:
            with self.assertRaises(media.MediaError):
                media.download("https://x.com/i/status/123", self.budget)
            fallback.assert_not_called()

    def test_metadata_rejection_does_not_fall_back(self):
        with patch.object(media, "download_ytdlp", side_effect=media.MediaError("playlist")), patch.object(media, "gallery_video_url") as fallback:
            with self.assertRaises(media.MediaError):
                media.download("https://x.com/i/status/123", self.budget)
            fallback.assert_not_called()

    def test_ytdlp_checks_metadata_before_downloading(self):
        with patch.object(media.yt_dlp, "YoutubeDL") as factory:
            downloader = factory.return_value.__enter__.return_value
            downloader.extract_info.return_value = {"duration": 601}
            with self.assertRaises(media.MediaError):
                media.download_ytdlp("url", self.budget)
            downloader.process_ie_result.assert_not_called()
            downloader.extract_info.assert_called_once_with("url", download=False)

    def test_ytdlp_uses_actual_merged_file(self):
        with patch.object(media.yt_dlp, "YoutubeDL") as factory:
            downloader = factory.return_value.__enter__.return_value
            downloader.extract_info.return_value = {"duration": 2}
            merged = self.root / "source/source.mkv"
            def download(*args, **kwargs):
                merged.write_bytes(b"merged media")
                return {"duration": 2, "requested_downloads": [{"filepath": str(self.root / "source/source.webm")}]}
            downloader.process_ie_result.side_effect = download
            downloader.prepare_filename.return_value = str(self.root / "source/source.webm")
            self.assertEqual(media.download_ytdlp("url", self.budget), merged)

    def test_non_x_failure_never_uses_gallery_fallback(self):
        with patch.object(media, "download_ytdlp", side_effect=media.yt_dlp.utils.DownloadError("failed")), \
                patch.object(media, "gallery_video_url") as fallback:
            with self.assertRaises(media.MediaError) as caught:
                media.download("https://youtube.com/watch?v=abc", self.budget)
            self.assertEqual(caught.exception.code, "download_failed")
            fallback.assert_not_called()

    def test_unsupported_url_gets_safe_specific_error(self):
        original = media.yt_dlp.utils.UnsupportedError("https://example.com/no-video")
        error = media.yt_dlp.utils.DownloadError("raw sensitive error", exc_info=(type(original), original, None))
        with patch.object(media, "download_ytdlp", side_effect=error):
            with self.assertRaises(media.MediaError) as caught:
                media.download("https://example.com/no-video", self.budget)
        self.assertEqual(caught.exception.code, "unsupported")
        self.assertNotIn("raw sensitive", caught.exception.user_message)

    def test_worker_rejects_invalid_url_before_download(self):
        with patch.object(media, "download") as download:
            with self.assertRaises(media.MediaError) as caught:
                media.process_media("file:///etc/passwd", "mp4", self.root, Limits())
            self.assertEqual(caught.exception.code, "invalid_link")
            download.assert_not_called()

    def test_gallery_rejects_photos_and_multiple_videos(self):
        for output in (b"https://pbs.twimg.com/a.jpg\n", b"https://video.twimg.com/a.mp4\nhttps://video.twimg.com/b.mp4\n"):
            with patch.object(self.budget, "command", return_value=output), self.assertRaises(media.MediaError):
                media.gallery_video_url("url", self.budget)

    def test_fallback_counts_bytes_without_content_length(self):
        budget = media.Budget(self.root, replace(Limits(), download_bytes=10))
        response = Mock(headers={})
        response.iter_content.return_value = [b"12345678", b"abcdef"]
        with patch.object(media.requests, "get") as get:
            get.return_value.__enter__.return_value = response
            with self.assertRaises(media.MediaError) as caught:
                media.download_direct("https://video.example/test.mp4", budget)
        self.assertEqual(caught.exception.code, "download_limit")
        self.assertEqual((self.root / "source/fallback.mp4").stat().st_size, 8)

    def test_fallback_respects_bytes_already_downloaded(self):
        budget = media.Budget(self.root, replace(Limits(), download_bytes=10))
        budget.add_download(8)
        response = Mock(headers={"content-length": "3"})
        with patch.object(media.requests, "get") as get:
            get.return_value.__enter__.return_value = response
            with self.assertRaises(media.MediaError):
                media.download_direct("https://video.example/test.mp4", budget)
        response.iter_content.assert_not_called()

    def test_fallback_rejects_html_masquerading_as_video(self):
        response = Mock(headers={"content-type": "text/html"})
        with patch.object(media.requests, "get") as get:
            get.return_value.__enter__.return_value = response
            with self.assertRaises(media.MediaError):
                media.download_direct("https://video.example/test.mp4", self.budget)
        response.iter_content.assert_not_called()

    def test_probe_rejects_images_audio_and_invalid_duration(self):
        cases = [{"streams": [{"codec_type": "audio"}]}, fake_probe(codec="mjpeg", duration="0"),
                 fake_probe(duration="nan"), fake_probe(duration="601")]
        for info in cases:
            with patch.object(self.budget, "command", return_value=json.dumps(info).encode()), self.assertRaises(media.MediaError):
                media.probe(self.root / "test.mp4", self.budget)

    def test_temp_and_subprocess_deadlines(self):
        (self.root / "large").write_bytes(b"12345")
        budget = media.Budget(self.root, replace(Limits(), temp_bytes=4))
        with self.assertRaises(media.MediaError) as caught:
            budget.check()
        self.assertEqual(caught.exception.code, "temp_limit")
        with patch.object(media.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            with self.assertRaises(media.MediaError) as caught:
                self.budget.command(["ffmpeg"])
        self.assertEqual(caught.exception.code, "timeout")

    def test_mp4_copies_compatible_streams_and_validates_faststart(self):
        info = fake_probe()
        output = self.root / "output.mp4"
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[0] == "ffprobe":
                return json.dumps(info).encode()
            output.write_bytes(struct.pack(">I4s", 8, b"moov") + struct.pack(">I4s", 8, b"mdat"))
            return b""
        with patch.object(self.budget, "command", side_effect=command):
            self.assertEqual(media.prepare_mp4(self.root / "source.mp4", self.budget, (info, info["streams"][0], 2)), output)
        self.assertIn("copy", calls[0])
        self.assertIn("+faststart", calls[0])

    def test_incompatible_mp4_is_transcoded_and_checked(self):
        source_info = fake_probe(codec="vp9", pixel="yuv444p", audio="opus")
        output = self.root / "output.mp4"
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[0] == "ffprobe":
                return json.dumps(fake_probe()).encode()
            output.write_bytes(struct.pack(">I4s", 8, b"moov"))
            return b""
        with patch.object(self.budget, "command", side_effect=command):
            media.prepare_mp4(self.root / "source.webm", self.budget, (source_info, source_info["streams"][0], 2))
        self.assertIn("libx264", calls[0])
        self.assertIn("aac", calls[0])
        self.assertIn("yuv420p", calls[0])

    def test_mp4_compression_attempt_is_bounded_and_size_checked(self):
        info = fake_probe()
        budget = media.Budget(self.root, replace(Limits(), upload_bytes=100_000))
        output = self.root / "output.mp4"
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            output.write_bytes(b"x" * 100_001)
            return b""
        with patch.object(budget, "command", side_effect=command):
            with self.assertRaises(media.MediaError) as caught:
                media.prepare_mp4(self.root / "source.mp4", budget, (info, info["streams"][0], 2))
        self.assertEqual(caught.exception.code, "upload_limit")
        self.assertEqual(len(calls), 2)
        self.assertIn("-maxrate", calls[1])

    def test_gif_retries_once_then_rejects(self):
        budget = media.Budget(self.root, replace(Limits(), upload_bytes=5))
        def command(args, **kwargs):
            (self.root / "output.gif").write_bytes(b"123456")
        with patch.object(budget, "command", side_effect=command) as run:
            with self.assertRaises(media.MediaError) as caught:
                media.prepare_gif(self.root / "source.mp4", budget)
        self.assertEqual(caught.exception.code, "upload_limit")
        self.assertEqual(run.call_count, 2)

    def test_bad_final_mp4_is_never_accepted(self):
        info = fake_probe()
        def command(args, **kwargs):
            if args[0] == "ffprobe":
                return json.dumps(fake_probe(codec="vp9")).encode()
            (self.root / "output.mp4").write_bytes(struct.pack(">I4s", 8, b"moov"))
            return b""
        with patch.object(self.budget, "command", side_effect=command):
            with self.assertRaises(media.MediaError) as caught:
                media.prepare_mp4(self.root / "source.mp4", self.budget, (info, info["streams"][0], 2))
        self.assertEqual(caught.exception.code, "invalid_media")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe are not on PATH")
class ConversionIntegrationTests(unittest.TestCase):
    def test_real_transcode_and_gif(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.avi"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
                            "-t", "1", "-c:v", "mpeg4", str(source)], check=True, timeout=30)
            budget = media.Budget(root, Limits())
            info = media.probe(source, budget)
            mp4 = media.prepare_mp4(source, budget, info)
            self.assertTrue(media.fast_start(mp4))
            self.assertEqual(media.probe(mp4, budget)[1]["codec_name"], "h264")
            gif = media.prepare_gif(source, budget)
            self.assertEqual(media.probe(gif, budget)[1]["codec_name"], "gif")
