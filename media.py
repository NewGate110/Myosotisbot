"""Bounded media pipeline. Executed in a disposable, contained worker process."""

import contextlib
import json
import math
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yt_dlp

from process_control import contain_windows_worker
from links import InvalidLink, validate_web_url
from settings import Limits

OUTPUT_FILES = {"mp4": "output.mp4", "gif": "output.gif", "audio": "output.mp3"}

ERRORS = {
    "failed": "Couldn't download or process this media. Please try another link.",
    "unsupported": "Couldn't find a supported video at this link. Please send a direct video or post link.",
    "download_failed": "Couldn't download this video. It may require login, be restricted, or be temporarily unavailable.",
    "invalid_link": "Please send a valid HTTP or HTTPS link to a public video website.",
    "download_limit": "This media exceeds the download size limit. Please use a smaller clip.",
    "temp_limit": "Processing exceeded the temporary storage limit. Please use a smaller clip.",
    "duration": "This video exceeds the configured duration limit. Please use a shorter clip.",
    "playlist": "Please send a link to a single video. Playlists and posts with multiple videos aren't supported.",
    "live": "Live streams aren't supported. Please send a recorded video.",
    "timeout": "Processing took too long and was stopped. Please try a smaller clip.",
    "invalid_media": "This link didn't produce a valid video with a known duration.",
    "no_audio": "This video has no audio track to download. Please try a different video.",
    "upload_limit": "The result is still too large to upload after compression. Please use a shorter clip.",
}


class MediaError(Exception):
    def __init__(self, code):
        self.code = code if code in ERRORS else "failed"
        self.user_message = ERRORS[self.code]
        super().__init__(self.user_message)


def directory_size(directory):
    total = 0
    for path in Path(directory).rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass  # Downloaders rename/remove temporary fragments.
    return total


class Budget:
    def __init__(self, root, limits):
        self.root = Path(root)
        self.limits = limits
        self.deadline = time.monotonic() + limits.job_seconds
        self.downloaded = 0
        self.per_file = {}
        self.violation = None
        self.progress = {"phase": "Starting"}
        self._last_progress = 0
        self._ffmpeg_index = 0

    def fail(self, code):
        self.violation = code
        raise MediaError(code)

    def check(self):
        if self.violation:
            raise MediaError(self.violation)
        if time.monotonic() > self.deadline:
            self.fail("timeout")
        if directory_size(self.root) > self.limits.temp_bytes:
            self.fail("temp_limit")

    def add_download(self, count):
        self.downloaded += count
        if self.downloaded > self.limits.download_bytes:
            self.fail("download_limit")

    def hook(self, data):
        self.check()
        key = data.get("filename", "unknown")
        count = data.get("downloaded_bytes") or 0
        previous = self.per_file.get(key, 0)
        # Account for retries resetting byte counters, as well as separate streams.
        self.add_download(count - previous if count >= previous else count)
        self.per_file[key] = count
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if total and self.downloaded + max(0, total - count) > self.limits.download_bytes:
            self.fail("download_limit")
        self.publish(phase="Downloading", done=count, total=total, unit="bytes",
                     speed=data.get("speed"), eta=data.get("eta"), stream=True)

    def publish(self, force=False, **values):
        self.progress.update(values)
        now = time.monotonic()
        if not force and now - self._last_progress < 0.5:
            return
        try:
            pending = self.root / "progress.tmp"
            pending.write_text(json.dumps(self.progress), encoding="utf-8")
            pending.replace(self.root / "progress.json")
            self._last_progress = now
        except OSError:
            pass  # Status display failure must not interrupt media processing.

    def phase(self, text, duration=None):
        self.progress = {"phase": text, "duration": duration}
        self.publish(force=True)

    def command(self, args, timeout=None):
        self.check()
        if args[0] == "ffmpeg":
            self._ffmpeg_index += 1
            name = f"ffmpeg-{self._ffmpeg_index}.progress"
            args = [args[0], "-progress", str(self.root / name), "-nostats", *args[1:]]
            self.publish(force=True, ffmpeg=name, done=None, total=None)
        remaining = self.deadline - time.monotonic()
        try:
            result = subprocess.run(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, check=True,
                timeout=max(0.01, min(remaining, timeout or self.limits.process_seconds)),
            )
        except subprocess.TimeoutExpired:
            self.fail("timeout")
        self.check()
        return result.stdout


def validate_metadata(info, budget):
    if not info:
        raise MediaError("invalid_media")
    if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
        raise MediaError("playlist")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise MediaError("live")
    if (info.get("duration") or 0) > budget.limits.source_seconds:
        raise MediaError("duration")
    selected = info.get("requested_formats") or [info]
    size = sum(item.get("filesize") or item.get("filesize_approx") or 0 for item in selected)
    if size + budget.downloaded > budget.limits.download_bytes:
        raise MediaError("download_limit")


class QuietLogger:
    # yt-dlp output must never contaminate the worker's JSON protocol.
    def debug(self, message):
        pass

    info = warning = error = debug


def download_ytdlp(url, budget):
    source_dir = budget.root / "source"
    source_dir.mkdir(exist_ok=True)
    paths = []

    def postprocess(data):
        budget.check()
        if "Merger" in data.get("postprocessor", ""):
            budget.phase("Merging streams")
        path = data.get("info_dict", {}).get("filepath")
        if path:
            paths.append(Path(path))

    options = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(source_dir / "source.%(ext)s"),
        # This is an intermediate container; the final MP4 is explicitly prepared.
        "merge_output_format": "mkv",
        # Inspect at most one entry before rejecting the top-level playlist.
        "noplaylist": True, "extract_flat": "in_playlist", "playlistend": 1,
        "quiet": True, "no_warnings": True, "logger": QuietLogger(),
        "socket_timeout": 20, "retries": 2, "fragment_retries": 2,
        "concurrent_fragment_downloads": 1,
        "max_filesize": budget.limits.download_bytes,
        "progress_hooks": [budget.hook], "postprocessor_hooks": [postprocess],
        # Keep generic extraction enabled: yt-dlp can find embedded and direct
        # media even on sites without a named extractor.
        "default_search": "error", "enable_file_urls": False,
        "js_runtimes": {name: {} for name in ("deno", "node") if shutil.which(name)},
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
        validate_metadata(info, budget)
        budget.check()
        result = downloader.process_ie_result(info, download=True)
        validate_metadata({k: v for k, v in result.items() if k not in ("filesize", "filesize_approx", "requested_formats")}, budget)
        paths += [Path(item["filepath"]) for item in result.get("requested_downloads", []) if item.get("filepath")]
        if result.get("filepath"):
            paths.append(Path(result["filepath"]))
        paths.append(Path(downloader.prepare_filename(result)))
    budget.check()
    # Merged files take precedence over pre-merge download records.
    merged = source_dir / "source.mkv"
    if merged.is_file():
        return merged
    for path in reversed(paths):
        if path.is_file() and path.stat().st_size:
            return path
    raise MediaError("invalid_media")


def gallery_video_url(url, budget):
    output = budget.command([sys.executable, "-m", "gallery_dl", "--ignore-config", "--get-urls", url], timeout=30)
    candidates = []
    for line in output.decode("utf-8", errors="replace").splitlines():
        parsed = urlsplit(line.strip())
        if parsed.scheme == "https" and parsed.path.lower().endswith(".mp4"):
            candidates.append(line.strip())
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        raise MediaError("invalid_media")
    if len(candidates) > 1:
        raise MediaError("playlist")
    return candidates[0]


def download_direct(url, budget):
    path = budget.root / "source" / "fallback.mp4"
    path.parent.mkdir(exist_ok=True)
    with requests.get(url, stream=True, timeout=(15, 20)) as response:
        response.raise_for_status()
        size = int(response.headers.get("content-length") or 0)
        if size + budget.downloaded > budget.limits.download_bytes:
            budget.fail("download_limit")
        content_type = response.headers.get("content-type", "").lower()
        if content_type.startswith(("image/", "text/")):
            raise MediaError("invalid_media")
        with path.open("wb") as stream:
            received = 0
            started = time.monotonic()
            for chunk in response.iter_content(chunk_size=65536):
                budget.check()
                budget.add_download(len(chunk))  # Check BEFORE writing the next chunk.
                stream.write(chunk)
                received += len(chunk)
                speed = received / max(0.01, time.monotonic() - started)
                budget.publish(phase="Downloading", done=received, total=size or None,
                               unit="bytes", stream=False, speed=speed,
                               eta=max(0, size - received) / speed if size and speed else None)
    budget.check()
    return path


def download_error(exc):
    original = exc.exc_info[1] if getattr(exc, "exc_info", None) else None
    return MediaError("unsupported" if isinstance(original, yt_dlp.utils.UnsupportedError) else "download_failed")


def download(url, budget):
    budget.phase("Downloading")
    try:
        return download_ytdlp(url, budget)
    except yt_dlp.utils.DownloadError as exc:
        # yt-dlp can wrap a hook's exception. Limits must never trigger fallback.
        budget.check()
        if urlsplit(url).hostname not in ("x.com", "twitter.com"):
            raise download_error(exc) from None
        try:
            return download_direct(gallery_video_url(url, budget), budget)
        except MediaError:
            raise
        except Exception:
            raise download_error(exc) from None


def probe(path, budget, max_duration=None, stream_type="video"):
    data = json.loads(budget.command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ], timeout=30))
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == stream_type and not s.get("disposition", {}).get("attached_pic")]
    if not streams:
        raise MediaError("invalid_media")
    try:
        duration = float(data.get("format", {}).get("duration") or streams[0].get("duration") or 0)
    except (ValueError, TypeError):
        raise MediaError("invalid_media") from None
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("invalid_media")
    if duration > (max_duration or budget.limits.source_seconds) + 0.1:
        raise MediaError("duration")
    return data, streams[0], duration


def fast_start(path):
    """Check MP4 top-level boxes without loading the media into memory."""
    with path.open("rb") as stream:
        total = path.stat().st_size
        while stream.tell() + 8 <= total:
            start = stream.tell()
            size, kind = struct.unpack(">I4s", stream.read(8))
            header = 8
            if size == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    return False
                size = struct.unpack(">Q", extended)[0]
                header = 16
            if size < header or start + size > total:
                return False
            if kind == b"moov":
                return True
            if kind == b"mdat":
                return False
            stream.seek(start + size)
    return False


def prepare_mp4(source, budget, source_info):
    data, video, duration = source_info
    output = budget.root / "output.mp4"
    budget.phase("Preparing MP4", duration=duration)
    audios = [s for s in data["streams"] if s.get("codec_type") == "audio"]
    copy_video = video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
    copy_audio = not audios or audios[0].get("codec_name") == "aac"
    base = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2", "-i", str(source),
            "-map", f"0:{video['index']}", "-map", "0:a:0?", "-sn", "-dn", "-map_metadata", "-1"]
    encode = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-threads", "2",
              "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-crf", "23"]
    audio = ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    budget.command(base + (["-c:v", "copy"] if copy_video else encode) + audio + ["-movflags", "+faststart", str(output)])
    if output.stat().st_size > budget.limits.upload_bytes:
        # One bounded retry. Reserve 10% for mux overhead/rate-control variation.
        audio_rate = 96_000 if audios else 0
        video_rate = int(budget.limits.upload_bytes * 8 * 0.90 / duration) - audio_rate
        if video_rate < 80_000:
            raise MediaError("upload_limit")
        compressed = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-threads", "2",
                      "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                      "-b:v", str(video_rate), "-maxrate", str(video_rate), "-bufsize", str(video_rate * 2),
                      "-c:a", "aac", "-b:a", "96k", "-ac", "2"]
        budget.command(base + compressed + ["-movflags", "+faststart", str(output)])
    if not 0 < output.stat().st_size <= budget.limits.upload_bytes:
        raise MediaError("upload_limit")
    final_data, final_video, _ = probe(output, budget)
    if ("mp4" not in final_data.get("format", {}).get("format_name", "").split(",")
            or final_video.get("codec_name") != "h264" or final_video.get("pix_fmt") != "yuv420p"
            or any(s.get("codec_name") != "aac" for s in final_data["streams"] if s.get("codec_type") == "audio")
            or not fast_start(output)):
        raise MediaError("invalid_media")
    return output


def prepare_gif(source, budget, duration=None):
    budget.phase("Converting GIF", duration=min(duration, budget.limits.gif_seconds) if duration else None)
    output = budget.root / "output.gif"
    # Palette generated and used within one bounded FFmpeg invocation.
    for width, fps in ((480, 15), (320, 10)):
        graph = (f"[0:v]fps={fps},scale=w='min({width},iw)':h=-1:flags=lanczos,split[a][b];"
                 "[a]palettegen[p];[b][p]paletteuse[out]")
        budget.command([
            "ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2", "-t", str(budget.limits.gif_seconds),
            "-i", str(source), "-filter_complex_threads", "1", "-filter_complex", graph,
            "-map", "[out]", "-an", "-loop", "0", str(output),
        ])
        if 0 < output.stat().st_size <= budget.limits.upload_bytes:
            _, video, _ = probe(output, budget, max_duration=budget.limits.gif_seconds + 0.2)
            if video.get("codec_name") != "gif":
                raise MediaError("invalid_media")
            return output
    raise MediaError("upload_limit")


def prepare_audio(source, budget, source_info):
    """Extract one full audio track from a validated video as a portable MP3."""
    data, _, duration = source_info
    audios = [stream for stream in data["streams"] if stream.get("codec_type") == "audio"]
    if not audios:
        raise MediaError("no_audio")
    budget.phase("Extracting audio", duration=duration)
    output = budget.root / OUTPUT_FILES["audio"]
    base = [
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2", "-i", str(source),
        "-map", f"0:{audios[0]['index']}", "-vn", "-sn", "-dn", "-map_metadata", "-1",
        "-c:a", "libmp3lame", "-ar", "44100", "-ac", "2",
    ]
    budget.command(base + ["-b:a", "192k", str(output)])
    if output.stat().st_size > budget.limits.upload_bytes:
        # One lower-bitrate retry, leaving room for headers and encoder padding.
        target_kbps = budget.limits.upload_bytes * 8 * 0.90 / duration / 1000
        bitrate = next((rate for rate in (128, 96, 64, 48, 32) if rate <= target_kbps), None)
        if bitrate is None:
            raise MediaError("upload_limit")
        budget.command(base + ["-b:a", f"{bitrate}k", str(output)])
    if not 0 < output.stat().st_size <= budget.limits.upload_bytes:
        raise MediaError("upload_limit")
    final_data, audio, _ = probe(output, budget, stream_type="audio")
    if (audio.get("codec_name") != "mp3"
            or final_data.get("format", {}).get("format_name") != "mp3"
            or any(stream.get("codec_type") == "video" for stream in final_data["streams"])):
        raise MediaError("invalid_media")
    return output


def process_media(url, mode, root, limits):
    if mode not in OUTPUT_FILES:
        raise MediaError("failed")
    try:
        validate_web_url(url)
    except InvalidLink:
        raise MediaError("invalid_link") from None
    budget = Budget(root, limits)
    source = download(url, budget)
    budget.phase("Checking media")
    info = probe(source, budget)
    if mode == "gif":
        result = prepare_gif(source, budget, duration=info[2])
    elif mode == "audio":
        result = prepare_audio(source, budget, info)
    else:
        result = prepare_mp4(source, budget, info)
    budget.check()
    return result


def worker_main():
    # This runs before any external tool is started. Failure is fail-closed.
    job_handle = contain_windows_worker()
    try:
        limits = Limits(**json.loads(sys.stdin.buffer.read()))
        with contextlib.redirect_stdout(sys.stderr):
            process_media(sys.argv[1], sys.argv[2], Path(sys.argv[3]), limits)
        result = {"ok": True}
    except MediaError as exc:
        result = {"error": exc.code}
    except Exception:
        result = {"error": "failed"}
    print(json.dumps(result), flush=True)
    # Do not close job_handle early: process exit closes it and kills descendants.
    return job_handle


if __name__ == "__main__":
    worker_main()
