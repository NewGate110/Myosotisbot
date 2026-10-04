# Session handoff — 4 October 2026

## Project and environment

- Repository: `E:\workshop\linux server stuff\video-downloader-bot`.
- Telegram video downloader using Python, python-telegram-bot, yt-dlp, FFmpeg/ffprobe, and an X-only gallery-dl fallback.
- The user said the bot is currently running on this Windows computer; Linux deployment is also documented.
- Local Python environment: `.venv\Scripts\python.exe`.
- Git working tree was clean immediately before creating this handoff. No commit was created by the assistant; a suggested commit message was supplied earlier.
- Keep `.env` private. Never print its contents or tokens. Preserve the user's configured credentials and IDs.

## Implemented behavior

- Access is restricted to `TELEGRAM_ALLOWED_GROUP_ID` and private messages from `TELEGRAM_OWNER_USER_ID`. These are numeric IDs, not Telegram sessions. Missing/invalid configuration fails closed.
- Callback buttons are scoped to the original requester, chat, and menu message. Pending requests expire after 15 minutes.
- HTTP/HTTPS links from message text, captions, and hidden hyperlinks go through yt-dlp. There is no fixed platform allowlist. Playlists, multiple-video posts, live streams, and audio-only sources are rejected.
- Output choices: GIF, MP4, and Audio (MP3). The menu is offered for candidate links; the worker validates that the source is a video after selection.
- MP4 output is checked for H.264, yuv420p, AAC when audio is present, and fast-start layout; incompatible media is converted. Oversized output gets a bounded compression retry.
- MP3 extraction uses the video's audio track, with a lower-bitrate retry when needed. Silent videos get a specific error.
- Token redaction and quieter HTTP logging prevent bot credentials appearing in logs.
- Media processing runs in a disposable worker with time/storage monitoring and descendant-process cleanup on Windows and Unix.
- Uploads check file size and Telegram's returned attachment. Explicit short rate-limit responses get a bounded retry; ambiguous network failures are not automatically retried, to avoid duplicates. Errors remain visible.

## User's current UI preferences

1. Reply directly to an accepted link with exactly `processing..`.
2. Keep that reply in the chat.
3. After **1.5 seconds**, send a separate reply containing GIF / MP4 / Audio buttons. The original requested delay was 3 seconds, but the user later edited it to 1.5; preserve the current value.
4. Reuse the menu message for progress while downloading, merging, converting, extracting audio, and uploading.
5. Status contains only the current process/output format and the bar, for example:

   ```text
   📥 Downloading · MP4
   ######---- 62%
   ```

6. Do not display sizes, speed, elapsed time, ETA, or a stage checklist.
7. Unknown totals use an activity bar. Upload completion waits for Telegram confirmation.
8. Delete the progress message after confirmed delivery. Keep the separate `processing..` reply. A failed deletion must not report the upload itself as failed.

## Limits and configuration

Defaults: upload 45 MB (maximum configurable 49 MB), combined download 200 MB, temporary storage 500 MB, source duration 600 seconds, GIF duration 30 seconds, processing job 600 seconds, individual process 120 seconds, one active job.

README now displays sizes in decimal MB. The `MAX_*_BYTES` variables in `.env` still require integer bytes (1 MB = 1,000,000 bytes). `.env.example` should remain tracked.

## TikTok fix and validation

- Normalize direct TikTok and `/t/` links without `www` to the extractor's expected hostname. Preserve `vm.tiktok.com` and `vt.tiktok.com` share URLs for yt-dlp to resolve.
- The user reported failure for `https://vt.tiktok.com/ZSbuFMt5N/`.
- Reproduced an unexpected TikTok webpage response and a warning that browser impersonation support was unavailable.
- Changed the requirement to `yt-dlp[default,curl-cffi]>=2026.8.19` and installed the extra in this repository's virtual environment.
- Retested the exact link successfully: both extraction and a real download through `media.download` worked (493,375 bytes). Temporary test files were removed. No Telegram upload was performed.
- User was told to restart the bot and resend the link. A successful end-to-end Telegram delivery has not been confirmed in this session.
- Local versions observed during that fix: yt-dlp 2026.08.19 and curl-cffi 0.16.3. `pip check` passed.

## Tests and remaining checks

Run from the repository root in PowerShell:

```powershell
& .venv\Scripts\python.exe -m unittest discover -s tests -v
```

- Latest full run: **96 tests, 94 passed, 2 skipped**.
- The two skipped tests exercise real FFmpeg MP4/GIF and MP3 conversion. FFmpeg/ffprobe were not visible on the assistant's test-process PATH. This does not establish what PATH the user's running bot has.
- Two reported failures were stale test expectations: 3 seconds versus 1.5, and `▱` versus `-`. Updated tests and README to match current behavior, then reran the full suite successfully.
- A python-telegram-bot `RetryAfter` deprecation warning can appear; it is not a test failure.
- A deliberate deletion-failure test emits `Upload succeeded, but the progress message could not be deleted.`; the test passes and verifies that cleanup failure does not become upload failure.
- Network access from the assistant sandbox is restricted. Live TikTok checks needed approved network execution; an initial WinError 10013 was sandbox-related, not the reproduced TikTok failure.

## Main files

- `bot.py`: access control, link/menu handlers, worker orchestration, logging.
- `links.py`: link discovery, validation, normalization.
- `media.py`: bounded extraction, downloading, FFmpeg conversion, worker protocol.
- `uploads.py`: upload validation, confirmation, error classification, retries, streamed progress.
- `progress.py`: compact status rendering and message lifecycle.
- `process_control.py`: worker/process-tree cleanup.
- `settings.py`: environment configuration and limits.
- `tests/`: unit tests and optional real-conversion tests.
- `README.md`, `.env.example`, `requirements.txt`: setup and dependencies.

## Scope and caveats

- yt-dlp platform availability is not a guarantee that every URL works. Only the supplied TikTok video was live download-tested during this session. Private/login-required, geographically restricted, and DRM-protected media may fail; no browser cookies or account sessions are imported automatically.
- Link validation rejects literal private/local addresses and embedded credentials, but is not full SSRF protection against DNS resolution or redirects.
- No new feature request remains outstanding; this file is the requested next-session rundown.
- Current `.gitignore` ignores `.env`, Python caches, `.venv`, and `.idea`. Additional patterns such as `.env.*` with `!.env.example`, `.vscode/`, logs, and test artifacts were suggested but are not currently present. Do not claim they were implemented.
