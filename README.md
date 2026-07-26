# Telegram Kajian Auto-Upload

Shares a voice recording on Android → bot compresses it → publishes a WordPress post → replies with the URL.

## How it works

```
Android share → Telegram bot (Local API Server, 2 GB limit)
             → ffmpeg (mono Opus, 48 kbps)
             → WordPress Media Library (REST API + Application Password)
             → WordPress post (native audio block)
             → Telegram reply with post URL
```

---

## Setup

### 1. Get a Telegram Bot Token

1. Open Telegram → search **@BotFather** → `/newbot`
2. Follow the prompts; copy the token (looks like `123456789:ABC...`)

### 2. Get Telegram API ID & Hash (for the Local Bot API Server)

1. Go to <https://my.telegram.org> → Log in → **API development tools**
2. Create an app; copy **App api_id** and **App api_hash**

> **Why?** The official `telegram-bot-api` Docker image (which removes the 20 MB file cap) requires your API credentials to run its own local server.

### 3. Create a WordPress Application Password

1. In your WordPress admin → **Users → Profile**
2. Scroll to **Application Passwords** → enter a name (e.g. `kajian-bot`) → **Add New Application Password**
3. Copy the generated password immediately — it won't be shown again

### 4. Configure the environment

```bash
cp .env.example .env
# Edit .env with your actual values
nano .env
```

### 5. Build and run

```bash
docker compose up -d --build
```

Check logs:
```bash
docker compose logs -f kajian-bot
docker compose logs -f telegram-bot-api
```

---

## Testing end-to-end

1. Open Telegram → find your bot → send any audio file (voice message or file)
2. Optionally add a caption → that becomes the post title
3. Bot replies `⏳ Sedang diproses…` then `✅ Kajian tayang: <url>`
4. Visit the URL in your browser → confirm the audio player works

### Quick smoke test without a real audio file

```bash
# Enter the running bot container
docker compose exec kajian-bot bash

# Check ffmpeg is available
ffmpeg -version

# Generate a 5-second test tone and compress it
ffmpeg -f lavfi -i sine=frequency=440:duration=5 /tmp/test.wav
ffmpeg -i /tmp/test.wav -c:a libopus -b:a 48k -ac 1 /tmp/test.opus
ls -lh /tmp/test.opus
```

---

## Switching to MP3 (if Opus is unsupported by your WP theme)

In `.env`:
```
AUDIO_CODEC=libmp3lame
OUTPUT_EXT=mp3
AUDIO_BITRATE=48k
```

---

## Defaults & design decisions

| Setting | Default | Reason |
|---|---|---|
| Codec | `libopus` | Best quality/size ratio for speech |
| Bitrate | `48k` | Transparent for mono voice; halve to `32k` to save more space |
| Channels | `1` (mono) | Lectures are mono; stereo doubles size with no perceptible gain |
| Post status | `publish` | Change to `draft` to review before going live |
| File cap | 2 GB | Via Local Bot API Server; default api.telegram.org cap is 20 MB |

---

## Failure behavior

- **ffmpeg error**: bot replies with the short error message; original temp file is not deleted; full traceback logged
- **WordPress API error**: bot replies with HTTP status and truncated response body
- **Any other error**: bot replies with the exception message; full traceback logged
- All temp files (in a system `tempfile` directory) are deleted automatically after each run regardless of success or failure
