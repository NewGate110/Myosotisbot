"""Restricted Telegram media bot. Run with: python bot.py."""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from media import MediaError, OUTPUT_FILES, directory_size
from links import InvalidLink, message_url, normalize_url
from process_control import stop_worker
from settings import ConfigurationError, Settings
from uploads import UploadError, upload_media
from progress import TelegramProgress, read_worker_progress

log = logging.getLogger("media_bot")
CALLBACK_RE = re.compile(r"^(gif|mp4|audio):([a-f0-9]{32})$")


class RedactingFormatter(logging.Formatter):
    def __init__(self, token):
        super().__init__("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        self.secrets = {token, quote(token, safe="")} - {""}

    def format(self, record):
        # Redact after formatting so args and exception traces are covered too.
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", text)


def configure_logging(token):
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(token))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass
class PendingLink:
    url: str
    chat_id: int
    user_id: int
    created: float
    message_id: int | None = None


class BotState:
    def __init__(self, settings):
        self.settings = settings
        self.pending = {}
        self.active = 0

    def expire(self):
        cutoff = time.monotonic() - 900
        self.pending = {key: value for key, value in self.pending.items() if value.created > cutoff}


def authorized(update, settings):
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or user.is_bot:
        return False
    if chat.type == "private":
        return user.id == settings.owner_id and chat.id == settings.owner_id
    return chat.type in ("group", "supergroup") and chat.id == settings.group_id


async def handle_link(update, context):
    state = context.application.bot_data["state"]
    if not authorized(update, state.settings) or update.message is None:
        return
    try:
        url = message_url(update.message)
    except InvalidLink as exc:
        await update.message.reply_text(str(exc))
        return
    if url is None:
        return
    state.expire()
    if len(state.pending) >= 1000:
        await update.message.reply_text("Too many pending links. Please try again later.")
        return
    key = uuid.uuid4().hex
    pending = PendingLink(url, update.effective_chat.id, update.effective_user.id, time.monotonic())
    state.pending[key] = pending
    try:
        await update.message.reply_text("processing..", do_quote=True)
        await asyncio.sleep(1.5)
        message = await update.message.reply_text("Got it. Send as:", do_quote=True, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎞 GIF", callback_data=f"gif:{key}"),
            InlineKeyboardButton("📹 MP4", callback_data=f"mp4:{key}"),
            InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"audio:{key}"),
        ]]))
        pending.message_id = message.message_id
    except Exception:
        state.pending.pop(key, None)
        raise


async def safe_edit(query, text):
    try:
        await asyncio.wait_for(query.edit_message_text(text), timeout=15)
        return True
    except Exception:
        log.warning("Could not update the status message.")
        return False


async def report_upload_error(query, context, chat_id, error, text=None):
    text = text or error.user_message
    if await safe_edit(query, text):
        return
    # The original status can have been deleted. Try a new message in the same
    # authorized chat/topic; never reroute an error or an upload to another chat.
    kwargs = {"chat_id": chat_id, "text": text}
    thread_id = getattr(query.message, "message_thread_id", None)
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    try:
        await asyncio.wait_for(context.bot.send_message(**kwargs), timeout=15)
    except Exception:
        log.warning("Could not deliver the upload error notification: %s", error.code)


async def run_media_worker(url, mode, directory, limits, status):
    # No bot credentials are needed by extractors or their subprocesses.
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name("media.py")), url, mode, str(directory),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=os.name != "nt",
    )
    communication = asyncio.create_task(process.communicate(json.dumps(asdict(limits)).encode()))
    start = time.monotonic()
    try:
        while not communication.done():
            await asyncio.wait({communication}, timeout=0.25)
            elapsed = time.monotonic() - start
            if elapsed > limits.job_seconds:
                raise MediaError("timeout")
            if directory_size(directory) > limits.temp_bytes:
                raise MediaError("temp_limit")
            if directory_size(Path(directory) / "source") > limits.download_bytes:
                raise MediaError("download_limit")
            snapshot = read_worker_progress(directory)
            if snapshot:
                status.clear()
                status.update(snapshot)
        stdout, _ = await communication
        if process.returncode != 0:
            raise MediaError("failed")
        result = json.loads(stdout)
        if "error" in result:
            raise MediaError(result["error"])
        output = Path(directory) / OUTPUT_FILES[mode]
        if not output.is_file() or not 0 < output.stat().st_size <= limits.upload_bytes:
            raise MediaError("upload_limit")
        return output
    finally:
        await stop_worker(process)
        if not communication.done():
            communication.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await communication


async def media_with_status(url, mode, directory, limits, query, status=None):
    if status is not None:
        return await run_media_worker(url, mode, directory, limits, status)
    async with TelegramProgress(query, mode) as progress:
        return await run_media_worker(url, mode, directory, limits, progress.data)


async def handle_button(update, context):
    state = context.application.bot_data["state"]
    if not authorized(update, state.settings):
        return
    query = update.callback_query
    match = CALLBACK_RE.fullmatch(query.data or "")
    if not match:
        await query.answer("Invalid button.")
        return
    mode, key = match.groups()
    state.expire()
    pending = state.pending.get(key)
    if pending is None:
        await query.answer("That link expired. Send it again.")
        return
    if (pending.chat_id, pending.user_id, pending.message_id) != (update.effective_chat.id, update.effective_user.id, query.message.message_id):
        await query.answer("This button belongs to the person who sent the link.")
        return
    if state.active >= state.settings.limits.active_jobs:
        await query.answer("Busy processing another request. Please try this button again shortly.")
        return
    # Reserve before awaiting: concurrent callbacks cannot consume the same job.
    state.pending.pop(key)
    state.active += 1
    progress = TelegramProgress(query, mode)
    try:
        await query.answer()
        async with progress:
            with tempfile.TemporaryDirectory(prefix="telegram-media-") as directory:
                output = await media_with_status(pending.url, mode, directory, state.settings.limits, query,
                                                 status=progress.data)
                progress.data.clear()
                progress.data["phase"] = "Uploading"
                await upload_media(
                    context.bot, output, mode, pending.chat_id, state.settings.limits.upload_bytes,
                    thread_id=getattr(query.message, "message_thread_id", None), progress=progress.data,
                )
        if not await progress.complete():
            log.warning("Upload succeeded, but the progress message could not be deleted.")
    except UploadError as exc:
        await report_upload_error(query, context, pending.chat_id, exc, text=progress.text(error=exc.user_message))
    except MediaError as exc:
        await safe_edit(query, progress.text(error=exc.user_message))
    except Exception:
        log.exception("Media request failed")
        await safe_edit(query, progress.text(error="Couldn't finish this request. Please try again later."))
    finally:
        state.active -= 1


async def handle_error(update, context):
    log.error("Telegram update failed", exc_info=context.error)


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    configure_logging(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    try:
        settings = Settings.from_env()
        missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
        if missing:
            raise ConfigurationError("Install FFmpeg and ffprobe and add them to PATH. Missing: " + ", ".join(missing))
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    app = (Application.builder().token(settings.token).concurrent_updates(16)
           .connect_timeout(30).read_timeout(180).write_timeout(180).pool_timeout(30).build())
    app.bot_data["state"] = BotState(settings)
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_error_handler(handle_error)
    log.info("Bot starting with restricted access")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
