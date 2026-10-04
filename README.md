# Telegram video downloader

Accepts video links through yt-dlp, offers GIF, MP4 or Audio (MP3), and uploads validated media. Access
is restricted to one group and the owner's private chat. MP4 output uses H.264
video, yuv420p pixels, AAC audio when present, and fast-start metadata.

## Setup

Requires Python 3.10+, FFmpeg and ffprobe on PATH:

```bash
sudo apt install ffmpeg
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

On Windows activate `.venv\Scripts\Activate.ps1` and install a build of FFmpeg
that includes ffprobe; put its `bin` directory on PATH.

For full YouTube extraction, also install a supported JavaScript runtime on
PATH: Deno (recommended by yt-dlp) or Node.js. The bot automatically enables
whichever of those runtimes are present. `requirements.txt` installs yt-dlp's
default dependencies, including its EJS challenge-solving package. Runtime
version requirements are maintained in the
[yt-dlp EJS guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

The requirements also include yt-dlp's `curl-cffi` extra for browser request
impersonation used by sites such as TikTok. After pulling dependency changes,
run `python -m pip install -U -r requirements.txt` in the bot's virtual environment
and restart the bot.

Fill in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=your_BotFather_token
TELEGRAM_OWNER_USER_ID=your_numeric_Telegram_user_id
TELEGRAM_ALLOWED_GROUP_ID=your_numeric_group_or_supergroup_id
```

The owner ID must be positive; the group ID must be negative (supergroup IDs
usually begin with `-100`). These are numeric IDs, not usernames, invite links,
or login sessions. Both IDs are required: missing or invalid settings stop
startup. The token does not identify the bot's creator automatically.

```bash
python bot.py
```

The `.env` next to `bot.py` is loaded; existing environment variables take
precedence. Never commit `.env`. If earlier bot logs were shared and contained
the token, replace it through BotFather and update `.env`.

## Access and usage

- Use `/uploadsize 20` to set your personal output size cap in decimal MB for
  MP4, GIF, and MP3. `/uploadsize` shows your setting; `/uploadsize reset`
  restores the configured default. Choose a positive whole number up to
  `MAX_UPLOAD_BYTES` (45 MB by default). The preference applies when a job
  starts, is shared across your authorized chats, and resets on bot restart.
  It never changes another user's limit or an already-running job. Smaller
  caps may reduce quality or cause a file to be rejected.
  Telegram's hosted Bot API documents a 50 MB upload limit; this bot retains
  a conservative 49 MB configuration ceiling.

- All human members may submit links in the configured group. Only the owner
  may use the bot in a private chat. Messages elsewhere are silently ignored,
  including the owner's messages in other groups. Channels are unsupported.
- Telegram still lets other accounts send private messages; the bot ignores
  them and performs no downloads for them.
- Set group privacy appropriately in BotFather so the bot receives ordinary
  pasted links. Telegram may require removing and re-adding the bot after a
  privacy change. See https://core.telegram.org/bots/features#privacy-mode.
- Only the original requester can use their GIF/MP4/Audio buttons in the original
  chat/message. Buttons expire after 15 minutes and after restart. A request is
  consumed once; a busy response leaves its buttons usable for another attempt.
- Group-to-supergroup migration changes the chat ID. Update `.env` and restart;
  access fails closed until the new ID is configured.
- There is no fixed domain list. HTTP/HTTPS video links are passed to yt-dlp,
  including YouTube/Shorts, Facebook videos/Reels, X/Twitter, Reddit,
  Instagram, TikTok, Vimeo, Dailymotion, and other supported services.
  yt-dlp also attempts generic pages with embedded videos and direct media URLs.
  See its [supported extractors](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).
- TikTok video links support the same MP4, GIF, and Audio (MP3) options.
  Paste a direct video link or a `vm.tiktok.com`, `vt.tiktok.com`, or
  `tiktok.com/t/` share link. Video and `/t/` links without `www` are normalized
  for the TikTok extractor; yt-dlp resolves short share links.
- Paste a link in message text, a media caption, or a hidden hyperlink. Telegram
  URL entities without a scheme are treated as HTTPS. Only the first web link
  in each message is processed. Signed URL queries are preserved.
- Send one recorded video. Playlists, live streams, audio-only links and posts
  containing multiple videos are rejected. Unsupported pages receive a clear
  error instead of being silently ignored. File/FTP URLs, embedded URL
  credentials, localhost and literal private IP addresses are not accepted.
- Availability depends on the installed yt-dlp version and the source site.
  This bot currently uses public, unauthenticated extraction: private videos,
  login-required posts, geo-restricted content, and DRM-protected streams may
  fail even when their website has an extractor. No browser cookies or account
  sessions are imported automatically.
- GIF output is at most 30 seconds by default, initially up to 480px wide at
  15fps. If oversized it gets one retry at up to 320px/10fps.
- MP4 preserves compatible video/audio streams when possible, converting other
  codecs as needed. Oversized MP4 gets one compression retry (up to 1280x720).
  A file still over the limit is rejected; videos are never silently truncated.
- Audio (MP3) appears alongside GIF and MP4 in the video-link menu. It extracts
  the full audio track as a 192 kbps MP3 and sends it with Telegram's audio
  player. Silent videos get a clear "no audio track" error. Like the other
  choices, a candidate link is validated when selected; an audio-only source
  is rejected. No menu is shown for ordinary chat or attachments without links.
  The bounded source video is downloaded and validated first, so the same source
  download/duration limits apply. An oversized MP3 gets one lower-bitrate retry;
  its duration is never silently trimmed to the GIF length cap.

## Resource limits

The bot replies to an accepted link with "processing..", then sends the format
buttons in a separate reply after 1.5 seconds. The "processing.." reply stays
in the chat.
The bot edits one compact status card through the whole request. It shows only
the current stage and output format above a progress bar. Download percentages
refer to the current stream when video and audio download separately.
FFmpeg processing uses encoded duration. Stages without a reliable
total show an animated activity bar instead of a made-up percentage.

Upload progress measures file bytes streamed into the HTTP client, not bytes
acknowledged by Telegram. At 100% the card shows "Waiting for Telegram"; only a
verified response triggers deletion of the progress message. Failures replace
it with the error. Normal edits are spaced at least three
seconds apart and respect Telegram's retry delay. Status updates do not block
the worker's time/storage monitoring.

Optional `.env` settings (sizes below are shown in decimal MB).
The `*_BYTES` settings still require integer byte values in `.env`;
multiply MB by 1,000,000 when configuring them.

| Variable | Default | Meaning |
| --- | ---: | --- |
| `MAX_UPLOAD_BYTES` | 45 MB | Final file; configurable up to 49 MB |
| `MAX_DOWNLOAD_BYTES` | 200 MB | Combined downloads, including fallback/retries |
| `MAX_TEMP_BYTES` | 500 MB | Temporary files for each job |
| `MAX_SOURCE_SECONDS` | 600 | Maximum source duration |
| `MAX_GIF_SECONDS` | 30 | First N seconds for GIF conversion |
| `MAX_JOB_SECONDS` | 600 | Download and conversion deadline |
| `MAX_PROCESS_SECONDS` | 120 | Each FFmpeg operation's deadline |
| `MAX_ACTIVE_JOBS` | 1 | Concurrent jobs, including uploads |

All limits must be positive integers. Metadata is checked before downloading;
byte counters also enforce limits during downloads without trustworthy size
metadata. Fallback is only attempted for X extractor failures, never to bypass
resource limits. It must return a video URL, and ffprobe validates actual media
and duration after download. Sources without a usable duration are rejected.

A separate worker performs media processing. The parent checks elapsed time and
temporary/source storage every 250ms and stops the worker and its descendants
on a limit, cancellation, or failure. Unix uses a separate process group;
Windows uses a kill-on-close Job Object and refuses to process if containment
cannot be established. Downloads also count bytes inside the worker. The source
directory check is conservative and includes merge intermediates. Filesystem
limits are monitored thresholds, not OS disk quotas: a write may briefly
overshoot between checks. Leave disk headroom, especially if increasing jobs.

Upload has a separate 240-second deadline, including any retry. Immediately
before sending, the bot checks that the file is readable, nonempty, and within
the upload limit. Success requires Telegram to return a message in the expected
chat with a video/animation/audio file ID. Timeouts, connection errors, rejected files,
missing permissions, invalid credentials and rate limits produce specific safe
messages. An explicit rate-limit rejection gets one retry if Telegram's wait is
at most 30 seconds and fits within the deadline. The file is reopened for retry.
Timeouts and connection failures are not automatically retried: Telegram may
have accepted the upload before the response was lost, so users are asked to
check the chat before resending. If editing the status fails, the bot tries to
send the error in the same chat/topic; if that also fails, it logs the failure.
Group migration is reported without automatically changing the allowed group.

All file handles close before the
job's temporary directory is removed. Busy requests are rejected promptly;
there is no unbounded job queue. Pending buttons are capped at 1000.

## Logging and verification

HTTPX/HTTPcore request logs are suppressed. Application logs redact the bot token
from formatted messages and exception traces. Media workers do not inherit
`TELEGRAM_*` environment variables and return only fixed error codes. Telegram
users receive fixed, safe error messages rather than raw exceptions.

Run offline tests:

```bash
python -m unittest discover -s tests -v
```

Tests cover permissions, button ownership, safe logging, byte/duration limits,
worker termination, fallback rejection, format validation, upload confirmation,
upload failures, safe retries, and notification fallback. Real conversion
checks run when FFmpeg and ffprobe are on PATH; otherwise they are skipped.
No tests use the real token or contact Telegram. Link tests cover text, captions,
hidden links, UTF-16 entity offsets, and installed extractor matching across
multiple sites; they do not assert that live downloads succeed. Keep dependencies
updated on the machine running the bot:

```bash
python -m pip install -U -r requirements.txt
```
