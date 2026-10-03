"""Check Telegram upload results and handle failures without duplicate sends."""

import asyncio
import logging
import os
import time
from datetime import timedelta
from telegram import InputFile

from telegram.error import (
    BadRequest, ChatMigrated, Forbidden, InvalidToken, NetworkError,
    RetryAfter, TimedOut,
)

log = logging.getLogger("media_bot.uploads")
UPLOAD_TIMEOUT = 240
MAX_ATTEMPTS = 2
MAX_RETRY_WAIT = 30

MESSAGES = {
    "file": "The prepared file is missing, empty, or unreadable. Please send the link again.",
    "size": "The prepared file is too large to upload. Please use a shorter clip.",
    "forbidden": "Telegram denied the upload. Check that the bot is allowed to send videos, animations, and audio in this chat and hasn't been blocked.",
    "token": "Telegram rejected the bot credentials. The owner needs to check the bot token.",
    "migrated": "This group has changed its ID. The owner needs to update TELEGRAM_ALLOWED_GROUP_ID and restart the bot.",
    "rate_limit": "Telegram is limiting uploads right now. Please wait a little, then send the link again.",
    "rejected": "Telegram rejected the upload. Try a different clip, and check the bot's media permissions in this chat.",
    "timeout": "Telegram didn't confirm the upload before it timed out. Check whether the media arrived before sending the link again.",
    "network": "The connection to Telegram failed during upload. Check whether the media arrived before sending the link again.",
    "unconfirmed": "Telegram didn't return a valid media confirmation. Check whether the media arrived before sending the link again.",
    "failed": "Couldn't confirm the upload. Check whether the media arrived before sending the link again.",
}


class UploadError(Exception):
    def __init__(self, code):
        self.code = code
        self.user_message = MESSAGES[code]
        super().__init__(self.user_message)


def classify_error(exc):
    if isinstance(exc, (TimedOut, asyncio.TimeoutError)):
        return UploadError("timeout")
    if isinstance(exc, Forbidden):
        return UploadError("forbidden")
    if isinstance(exc, InvalidToken):
        return UploadError("token")
    if isinstance(exc, ChatMigrated):
        return UploadError("migrated")
    if isinstance(exc, NetworkError):
        # Some gateways report HTTP 413 as NetworkError rather than BadRequest.
        reason = str(exc).lower().replace("_", " ")
        if "file is too big" in reason or "request entity too large" in reason:
            return UploadError("size")
    if isinstance(exc, BadRequest):  # BadRequest is also a NetworkError.
        return UploadError("rejected")
    if isinstance(exc, NetworkError):
        return UploadError("network")
    if isinstance(exc, OSError):
        return UploadError("file")
    return UploadError("failed")


class ProgressFile:
    """Count bytes lazily consumed by HTTPX, not eager InputFile buffering."""
    def __init__(self, stream, total, progress):
        self.stream, self.total, self.progress = stream, total, progress
        self.started = time.monotonic()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, size=-1):
        chunk = self.stream.read(size)
        done = min(self.stream.tell(), self.total)
        speed = done / max(time.monotonic() - self.started, 0.01)
        self.progress.update(phase="Confirming" if done == self.total else "Uploading",
                             done=done, total=self.total, unit="bytes", speed=speed,
                             eta=(self.total - done) / speed if speed else None)
        return chunk


async def upload_media(bot, path, mode, chat_id, upload_limit, thread_id=None, progress=None):
    """Retry only an explicit rate-limit rejection; network failures are ambiguous.

    Telegram may accept a file before the response is lost. Retrying timeouts or
    connection errors can post duplicates, so report their uncertain outcome.
    """
    deadline = time.monotonic() + UPLOAD_TIMEOUT

    async def send():
        for attempt in range(MAX_ATTEMPTS):
            try:
                # Reopen on each attempt: a rejected request may consume the stream.
                with path.open("rb") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    if size == 0:
                        raise UploadError("file")
                    if size > upload_limit:
                        raise UploadError("size")
                    attachment = stream
                    if progress is not None:
                        progress.clear()
                        progress.update(phase="Uploading", done=0, total=size, unit="bytes")
                        attachment = InputFile(ProgressFile(stream, size, progress), filename=path.name,
                                               read_file_handle=False)
                    kwargs = dict(chat_id=chat_id, write_timeout=180, read_timeout=180)
                    if thread_id is not None:
                        kwargs["message_thread_id"] = thread_id
                    if mode == "gif":
                        result = await bot.send_animation(animation=attachment, **kwargs)
                    elif mode == "mp4":
                        result = await bot.send_video(video=attachment, supports_streaming=True, **kwargs)
                    elif mode == "audio":
                        result = await bot.send_audio(audio=attachment, **kwargs)
                    else:
                        raise UploadError("file")
            except RetryAfter as exc:
                delay = exc.retry_after
                if isinstance(delay, timedelta):
                    delay = delay.total_seconds()
                delay = max(0, float(delay))
                if progress is not None:
                    progress.clear()
                    progress.update(phase="Retrying", pause_until=time.monotonic() + delay)
                if (attempt + 1 >= MAX_ATTEMPTS or delay > MAX_RETRY_WAIT
                        or delay + 1 >= deadline - time.monotonic()):
                    raise UploadError("rate_limit") from None
                log.info("Telegram rate-limited the upload; retrying once after its requested delay")
                await asyncio.sleep(delay)
                continue
            attachment = getattr(result, {"gif": "animation", "mp4": "video", "audio": "audio"}[mode], None)
            file_id = getattr(attachment, "file_id", None)
            message_id = getattr(result, "message_id", None)
            if (not isinstance(file_id, str) or not file_id
                    or not isinstance(message_id, int) or message_id <= 0
                    or getattr(result, "chat_id", None) != chat_id):
                raise UploadError("unconfirmed")
            return result

    try:
        result = await asyncio.wait_for(send(), timeout=UPLOAD_TIMEOUT)
    except UploadError as exc:
        log.warning("Upload unsuccessful: %s", exc.code)
        raise
    except Exception as exc:
        error = classify_error(exc)
        # Fixed categories only: raw API errors can include URLs or credentials.
        log.warning("Upload unsuccessful: %s", error.code)
        raise error from None
    log.info("Telegram confirmed the media upload")
    return result
