# X GIF/MP4 Telegram Bot

Paste an X (Twitter) link into the chat, tap **GIF** or **MP4**, get the media
back at the highest quality yt-dlp can find.

## 1. Create the bot on Telegram

1. Message **@BotFather** on Telegram.
2. Send `/newbot`, follow the prompts.
3. Copy the token it gives you — looks like `123456789:AAExampleToken...`.

## 2. Install dependencies

You need **Python 3.10+**, **ffmpeg**, and the packages in `requirements.txt`.

```bash
# ffmpeg (needed for the GIF conversion step)
sudo apt install ffmpeg        # Debian/Ubuntu/Raspberry Pi OS
# or: brew install ffmpeg      # macOS

# Python deps
pip install -r requirements.txt
```

## 3. Run it

Either export the token in your shell:

```bash
export TELEGRAM_BOT_TOKEN="123456789:AAExampleToken..."
python bot.py
```

...or create a `.env` file (copy `.env.example` and fill in your token):

```bash
cp .env.example .env
# edit .env, paste your token in
python bot.py
```

`python-dotenv` loads `.env` automatically — no extra flags needed. Don't
commit `.env` to git; add it to `.gitignore`.

Message your bot on Telegram with any x.com/twitter.com link. It'll reply
with GIF/MP4 buttons, then send the file back.

## Notes / things you might want to change later

- **Allowed sites**: edit the `ALLOWED_DOMAINS` list near the top of
  `bot.py` to add or remove sites. Currently: X/Twitter, YouTube, Reddit.
  Anything yt-dlp supports (hundreds of sites) works the same way once
  added to that list — no other code changes needed.
- **GIF length cap**: GIFs are trimmed to the first `MAX_GIF_SECONDS`
  (30s by default) of the source. This only kicks in for longer videos
  (e.g. YouTube) — X's own gifs are always short, so it never affects them.
- **Quality**: `download_best_video()` uses `bestvideo+bestaudio/best` — the
  actual max yt-dlp can see. X's own web player often serves several
  bitrates; yt-dlp picks the top one.
- **GIF size**: GIFs are capped at 480px width / 15fps in `convert_to_gif()`
  to keep file sizes reasonable for Telegram. Bump `scale`/`fps` in the code
  if you want higher quality GIFs (at the cost of bigger files — Telegram
  animations have a 50MB-ish practical ceiling before clients start
  complaining).
- **Multi-user**: `PENDING_LINKS` is a plain in-memory dict — perfectly fine
  for a personal bot used by you. If you ever add other users and want
  links not to leak/collide, that's the first thing to harden.
- **Running 24/7**: for a Pi or always-on box, wrap this in a systemd
  service (or just `tmux`/`screen` it) so it survives reboots and SSH
  disconnects.
- **Errors**: if X changes something and yt-dlp breaks, first thing to try
  is `pip install -U yt-dlp` — it gets patched for site changes very
  frequently.