"""
Telegram bot: paste an X (Twitter) link, get back the media as GIF or MP4,
always at the highest quality yt-dlp can find.

Flow:
  1. User sends a message containing an x.com / twitter.com link.
  2. Bot replies with two inline buttons: GIF / MP4.
  3. User taps one.
  4. Bot downloads the media with yt-dlp (bestvideo) and, if GIF was chosen,
     converts it with ffmpeg using a two-pass palette for good quality.
  5. Bot sends the file back and deletes the temp files.

Setup:
  pip install -r requirements.txt
  export TELEGRAM_BOT_TOKEN="123456:ABC-your-token-from-BotFather"
  python bot.py
"""

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import requests
import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("gif_bot")

load_dotenv()  # reads a .env file in the current directory, if present

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit(
        "Set TELEGRAM_BOT_TOKEN, either in a .env file or as an environment "
        'variable, e.g. export TELEGRAM_BOT_TOKEN="123456:ABC..."'
    )

# -----------------------------------------------------------------------
# Which sites the bot will accept links from. Edit this list to add/remove
# sites — everything else (matching, the button flow, download, convert)
# works the same for all of them.
# -----------------------------------------------------------------------
ALLOWED_DOMAINS = [
    "x.com",
    "twitter.com",
    "youtube.com",
    "youtu.be",
    "reddit.com",
    "redd.it",
]

_domain_pattern = "|".join(re.escape(d) for d in ALLOWED_DOMAINS)
LINK_RE = re.compile(rf"https?://(?:www\.|m\.)?(?:{_domain_pattern})/\S+")

# Pulls just the numeric tweet ID out of any x.com/twitter.com URL shape,
# including ones with a /photo/N or /video/N suffix (e.g. .../status/123/photo/1).
TWEET_ID_RE = re.compile(r"/status/(\d+)")

# GIFs longer than this get trimmed to the first N seconds. Mainly guards
# against someone tapping GIF on a long YouTube video by mistake — X's own
# gifs are always short so this never affects them.
MAX_GIF_SECONDS = 30

# In-memory store: callback_id -> tweet/video URL.
# Fine for a personal single-user bot; if this ever runs for many people
# at once for a long time, swap for something with expiry (e.g. TTL cache).
PENDING_LINKS: dict[str, str] = {}


def normalize_url(url: str) -> str:
    """
    Site-specific URL cleanup before handing off to the downloader.

    For x.com/twitter.com: strip any /photo/N or /video/N suffix down to
    the bare tweet URL. Those suffixes point yt-dlp at one specific media
    slot on the tweet, and it sometimes misreads that slot's type (e.g. a
    gif reported as "not a video") when there are multiple attachments.
    The plain /status/<id> URL makes yt-dlp inspect every attached media
    item and correctly pick the video/gif regardless of its slot.

    Other sites pass through unchanged.
    """
    if "x.com" in url or "twitter.com" in url:
        match = TWEET_ID_RE.search(url)
        if match:
            return f"https://x.com/i/status/{match.group(1)}"
    return url


# ---------------------------------------------------------------------------
# Step 1: user sends a link -> show GIF / MP4 buttons
# ---------------------------------------------------------------------------
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    match = LINK_RE.search(update.message.text or "")
    if not match:
        return

    url = normalize_url(match.group(0))
    callback_id = uuid.uuid4().hex[:12]
    PENDING_LINKS[callback_id] = url

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎞 GIF", callback_data=f"gif:{callback_id}"),
                InlineKeyboardButton("📹 MP4", callback_data=f"mp4:{callback_id}"),
            ]
        ]
    )
    await update.message.reply_text("Got it. Send as:", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Status window: a single helper used for every long-running phase
# (download, convert, upload). `status["text"]` is whatever the phase wants
# displayed right now (e.g. "Downloading… 42%, 1.3 MB/s") — this just edits
# the Telegram message to match it every couple seconds, with elapsed time
# appended, and only sends an edit when the text actually changed (avoids
# hitting Telegram's rate limit on identical edits).
# ---------------------------------------------------------------------------
async def run_with_status(query, status: dict, awaitable):
    start = time.monotonic()
    last_shown = None

    async def ticker():
        nonlocal last_shown
        while True:
            elapsed = int(time.monotonic() - start)
            text = f"{status.get('text', 'Working…')} ({elapsed}s)"
            if text != last_shown:
                try:
                    await query.edit_message_text(text)
                    last_shown = text
                except Exception:
                    pass  # message may be gone/already identical, safe to ignore
            await asyncio.sleep(2)

    ticker_task = asyncio.create_task(ticker())
    try:
        return await awaitable
    finally:
        ticker_task.cancel()


# ---------------------------------------------------------------------------
# Step 2: user taps a button -> download (+ convert) + send
# ---------------------------------------------------------------------------
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # stop the button's loading spinner

    mode, callback_id = query.data.split(":", 1)
    url = PENDING_LINKS.pop(callback_id, None)
    if not url:
        await query.edit_message_text("That link expired, send it again.")
        return

    status = {"text": f"Downloading ({mode})…"}
    await query.edit_message_text(status["text"])

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            mp4_path = await run_with_status(
                query, status,
                asyncio.to_thread(download_best_video, url, tmpdir, status),
            )
        except Exception as exc:  # yt-dlp failures, no video found, etc.
            log.exception("Download failed")
            await query.edit_message_text(f"Couldn't download that: {exc}")
            return

        gif_path = None
        if mode == "gif":
            status["text"] = "Converting to GIF…"
            try:
                gif_path = await run_with_status(
                    query, status,
                    asyncio.to_thread(convert_to_gif, mp4_path, tmpdir),
                )
            except Exception as exc:
                log.exception("GIF conversion failed")
                await query.edit_message_text(f"Downloaded but couldn't convert to GIF: {exc}")
                return

        try:
            if mode == "gif":
                status["text"] = "Uploading GIF…"
                await run_with_status(
                    query, status,
                    context.bot.send_animation(
                        chat_id=query.message.chat_id,
                        animation=gif_path.open("rb"),
                        write_timeout=180,
                        read_timeout=180,
                    ),
                )
            else:
                status["text"] = "Uploading video…"
                await run_with_status(
                    query, status,
                    context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=mp4_path.open("rb"),
                        supports_streaming=True,
                        write_timeout=180,
                        read_timeout=180,
                    ),
                )
            await query.delete_message()
        except Exception as exc:
            log.exception("Send failed")
            await query.edit_message_text(f"Converted but couldn't send it: {exc}")


# ---------------------------------------------------------------------------
# Media handling
# ---------------------------------------------------------------------------
def download_best_video(url: str, out_dir: str, status: dict | None = None) -> Path:
    """
    Download the highest quality video/gif for this tweet.

    Tries yt-dlp first (handles the general case well). Some tweets trip up
    yt-dlp's media-type detection — most commonly gifs that X serves through
    a "photo" slot — where it wrongly reports "Media #N is not a video" even
    though the content plays fine on x.com. When that happens, fall back to
    gallery-dl, which correctly distinguishes X's photo/video/animated_gif
    media types and won't hit this misclassification.

    If given, `status["text"]` is kept updated with progress (percent/speed
    while yt-dlp downloads, percent while the fallback path downloads) for
    run_with_status() to display.
    """
    try:
        return _download_via_ytdlp(url, out_dir, status)
    except Exception as first_exc:
        log.warning("yt-dlp failed (%s); trying gallery-dl fallback", first_exc)
        if status is not None:
            status["text"] = "yt-dlp couldn't read this one, trying fallback…"
        try:
            media_url = _fetch_media_url_via_gallery_dl(url)
            return _download_direct(media_url, out_dir, status)
        except Exception:
            # Fallback didn't pan out either — surface the original,
            # more informative yt-dlp error rather than the fallback's.
            raise first_exc


def _format_progress(downloaded: int, total: int | None, speed: float | None) -> str:
    if total:
        text = f"Downloading… {downloaded / total * 100:.0f}%"
    else:
        text = f"Downloading… {downloaded / (1024 * 1024):.1f} MB"
    if speed:
        text += f" ({speed / (1024 * 1024):.1f} MB/s)"
    return text


def _download_via_ytdlp(url: str, out_dir: str, status: dict | None = None) -> Path:
    def progress_hook(d: dict) -> None:
        if status is None:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            status["text"] = _format_progress(
                d.get("downloaded_bytes", 0), total, d.get("speed")
            )
        elif d.get("status") == "finished":
            status["text"] = "Download complete, processing…"

    out_template = str(Path(out_dir) / "%(id)s.%(ext)s")
    ydl_opts = {
        # bestvideo+bestaudio merged, falling back to best single file.
        # X gifs/videos usually have no separate audio track, but this
        # covers regular videos posted as links too.
        "format": "bestvideo+bestaudio/best",
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        # Prefer yt-dlp's own record of what it actually wrote to disk
        # (requested_downloads[].filepath) over reconstructing the name
        # from the outtmpl — the latter can guess wrong (e.g. an "NA"
        # extension) when yt-dlp couldn't determine the format ahead of
        # time, which happens on some of X's gif/photo-slot tweets.
        downloads = info.get("requested_downloads") or [info]
        filepath = downloads[0].get("filepath") or ydl.prepare_filename(info)
        path = Path(filepath)

        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(
                f"yt-dlp reported success but produced no usable file ({path.name})"
            )
        return path


def _fetch_media_url_via_gallery_dl(url: str) -> str:
    """
    Ask gallery-dl for the direct media URL(s) on this tweet, without
    downloading anything itself. gallery-dl reads X's real media type
    (photo / video / animated_gif) rather than guessing from the URL slot,
    so it doesn't hit the same misclassification yt-dlp can.
    """
    result = subprocess.run(
        ["gallery-dl", "--get-urls", url],
        capture_output=True,
        text=True,
        timeout=30,
    )
    urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not urls:
        raise RuntimeError(f"gallery-dl found no media: {result.stderr.strip()}")

    # If gallery-dl lists more than one URL (rare for a single video/gif
    # tweet), prefer whichever actually looks like a video file.
    video_urls = [u for u in urls if u.split("?")[0].lower().endswith(".mp4")]
    return video_urls[0] if video_urls else urls[0]


def _download_direct(media_url: str, out_dir: str, status: dict | None = None) -> Path:
    path = Path(out_dir) / "media.mp4"
    with requests.get(media_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0) or None
        downloaded = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                downloaded += len(chunk)
                if status is not None:
                    status["text"] = _format_progress(downloaded, total, None)
    return path


def convert_to_gif(mp4_path: Path, out_dir: str) -> Path:
    """
    Two-pass ffmpeg conversion: generate an optimal color palette from the
    video, then encode the gif against that palette. Much better quality
    than a naive single-pass conversion, at basically the same effort.

    Trims to MAX_GIF_SECONDS if the source is longer, so tapping GIF on a
    long video (e.g. a full YouTube video) doesn't produce a huge/slow file.
    """
    import subprocess

    gif_path = Path(out_dir) / f"{mp4_path.stem}.gif"
    palette_path = Path(out_dir) / "palette.png"

    fps = "15"  # good enough smoothness while keeping file size sane
    scale = "480:-1"  # cap width at 480px; -1 keeps aspect ratio
    trim = ["-t", str(MAX_GIF_SECONDS)]  # only affects sources longer than this

    # Pass 1: build palette
    subprocess.run(
        [
            "ffmpeg", "-y", *trim, "-i", str(mp4_path),
            "-vf", f"fps={fps},scale={scale}:flags=lanczos,palettegen",
            str(palette_path),
        ],
        check=True,
        capture_output=True,
    )

    # Pass 2: encode using that palette
    subprocess.run(
        [
            "ffmpeg", "-y", *trim, "-i", str(mp4_path), "-i", str(palette_path),
            "-lavfi", f"fps={fps},scale={scale}:flags=lanczos[x];[x][1:v]paletteuse",
            str(gif_path),
        ],
        check=True,
        capture_output=True,
    )
    return gif_path


# ---------------------------------------------------------------------------
def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60)
        .read_timeout(180)
        .write_timeout(180)
        .pool_timeout(60)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_button))
    log.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()