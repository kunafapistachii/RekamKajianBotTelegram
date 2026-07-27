# Project Knowledge & Rules: TelegramAutoUploadKajian

## Project Overview
- **Repository**: TelegramAutoUploadKajian (`https://github.com/kunafapistachii/RekamKajianBotTelegram.git`)
- **Deployment**: Proxmox VE (LXC) + Portainer + Docker Compose.
- **Services**:
  1. `telegram-bot-api`: Official Telegram Local Bot API Server (port 8081, 2 GB limit, volume `telegram-bot-api-data:/var/lib/telegram-bot-api`).
  2. `kajian-bot`: Python 3.12 bot app with ffmpeg & ffprobe.

## Key Technical Decisions & Architecture
1. **Audio Compression**: ffmpeg mono Opus (`libopus`, 48k, `.opus`) for high-quality speech compression.
2. **WordPress API**: REST API with Application Password auth (`/wp-json/wp/v2/media`, `/wp-json/wp/v2/categories`, `/wp-json/wp/v2/posts`).
3. **Category Structure**:
   - Parent Category: **Syaikh** (e.g. `Syaikh Utsaimin`).
   - Child Category: **Judul Buku** (e.g. `Syarah Kitabut Tauhid`).
4. **Interactive Telegram UI**:
   - Shows list of Book titles directly by default after upload.
   - Inline buttons: `Filter per Syaikh`, `+ Buku Baru`, `Tanpa Buku (Umum)`.
5. **Recording Timestamp Detection**:
   - Parses Samsung Voice format `YYMMDD_HHMMSS` (e.g. `Voice 260721_200611.m4a` -> 2026-07-21 20:06:11).
   - Parses ISO dates and `ffprobe` format tags.
   - Sets both `date` and `date_gmt` in WordPress REST API so published posts match the exact recording timestamp.
6. **Disk & State Optimization**:
   - Reads files directly from local volume without duplicating to `/tmp`.
   - Auto-deletes raw files from `/var/lib/telegram-bot-api` after upload to prevent `No space left on device`.
   - Uses `msg_id` keying (`context.user_data["pending_posts"][msg_id]`) to handle multi-file uploads concurrently without state collision.
