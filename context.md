<role>
You are a senior backend/automation engineer with expertise in Telegram Bot API integrations, audio processing (ffmpeg), Dockerized self-hosted infrastructure, and WordPress REST API development.
Your audience: A self-taught sysadmin running a homelab (Proxmox + Docker + CasaOS), comfortable reading and modifying Python code, wants a working, deployable solution rather than a conceptual explanation.
Communication style: Direct, practical, code-first. Explain non-obvious decisions with brief inline comments rather than long prose. Assume familiarity with Docker and basic networking, but not with the Telegram Bot API or WordPress REST API internals.
</role>

<task>
Build a self-hosted pipeline that automatically archives voice recordings sent via Telegram into WordPress blog posts.

Key requirements:
- Runs as a Docker container (with docker-compose) inside an existing homelab stack (Proxmox/CasaOS)
- Listens for audio/voice files sent to a private Telegram bot, using a self-hosted Local Bot API Server (not api.telegram.org) to support files up to 2GB instead of the default 20MB limit
- Reads the message caption as the intended title (if provided); falls back to a timestamp-based name if no caption is given
- Renames the file to a consistent convention: kajian_YYYY-MM-DD_HH-MM-SS_<slugified-title>.<ext>
- Compresses the audio using ffmpeg to mono Opus (or MP3 if Opus unsupported by target), targeting ~32-64kbps — optimized for spoken-word voice, not music
- Uploads the compressed file directly to the WordPress Media Library via the REST API, authenticated with a WordPress Application Password (no external object storage like S3/R2)
- Creates a new WordPress post embedding the uploaded audio (native HTML5 audio block), using the derived title as the post title
- Sends a reply back to the same Telegram chat confirming success, including the published post URL
- On any failure (ffmpeg error, upload error, WP API error), replies to the Telegram chat with a clear error message instead of failing silently, and logs the error
- Cleans up all temporary/local files after each run
</task>

<context>
Background and architecture decided so far:

- Source: Android phone. User records a kajian (Islamic lecture) with a voice recorder app, then uses Android's native Share sheet to send the file straight to a private Telegram bot (no folder-sync, no laptop step).
- Why a Local Bot API Server: the standard Telegram Bot API caps file downloads at 20MB, which many longer kajian recordings exceed before compression. Running the official `telegram-bot-api` Docker image locally raises this to 2GB.
- Processing happens on the user's homelab (Proxmox host running Docker/CasaOS), which already hosts other services, so this should be one more well-behaved container in that stack (env-based config, no hardcoded secrets, clean logging).
- Destination: an existing WordPress installation on a subdomain of the user's hosting account. The hosting plan has effectively unlimited storage, so audio files are uploaded straight into the WP Media Library rather than to external object storage. Cloudflare (already used for other homelab services) sits in front of the WP domain in proxy mode purely as a caching/bandwidth-saving layer — it is not part of this pipeline's code.
- Compression is still done before upload (not skipped just because storage is unlimited), because it reduces bandwidth cost per playback and speeds up the pipeline.
- End-to-end flow:
  1. User shares a recording (with optional caption = lecture title) to the Telegram bot
  2. Bot (via Local Bot API Server) receives and downloads the file
  3. Script renames the file
  4. Script compresses the file with ffmpeg
  5. Script uploads the compressed file to WP Media Library via REST API
  6. Script creates a WP post embedding that audio file
  7. Bot replies in Telegram with the live post URL
</context>

<examples>
Example 1 — with caption:
Input: User shares `recording_20260726.m4a` with caption "Kitab Riyadhus Shalihin - Bab Taubah"
Expected behavior: file renamed to `kajian_2026-07-26_14-32-10_kitab-riyadhus-shalihin-bab-taubah.opus`, compressed, uploaded, WP post created with title "Kitab Riyadhus Shalihin - Bab Taubah", bot replies:
"✅ Kajian tayang: https://blog.example.com/2026/07/26/kitab-riyadhus-shalihin-bab-taubah/"

Example 2 — no caption:
Input: User shares a voice file with no caption
Expected behavior: file renamed to `kajian_2026-07-26_09-15-02.opus`, WP post title falls back to "Kajian 2026-07-26 09:15", same reply pattern with the resulting URL.

Example 3 — failure case:
Input: ffmpeg fails to process a corrupted file
Expected behavior: bot replies "⚠️ Gagal memproses file: [short error reason]. File asli belum terhapus, coba kirim ulang." Original temp file is not deleted so the user can retry investigation; error is logged with a timestamp and traceback.
</examples>

<output>
Format: A working project, delivered as:
1. Project folder structure (tree view)
2. Full source code for the bot/pipeline script(s) (Python)
3. Dockerfile
4. docker-compose.yml (including the Local Bot API Server service alongside the bot service)
5. `.env.example` listing all required environment variables (Telegram bot token, Local Bot API Server config, WordPress site URL, WP username, WP Application Password, ffmpeg bitrate/format settings)
6. A short README covering: how to create a WordPress Application Password, how to get a Telegram bot token from BotFather, how to point the bot at the local API server instead of api.telegram.org, how to build/run the containers, and how to test end-to-end
Length: No fixed word limit — completeness over brevity, but avoid filler explanation between code blocks.
Structure: Project structure → source code → Dockerfile → docker-compose.yml → .env.example → README/setup instructions → testing checklist.
</output>

<constraints>
- No hardcoded secrets anywhere in code — all via environment variables
- Must use the self-hosted Local Bot API Server, not the public api.telegram.org endpoint, for the reasons stated above
- Do not introduce external cloud storage (no S3/R2/B2) — final audio file lives in the WordPress Media Library
- Keep dependencies minimal (e.g., python-telegram-bot or a lightweight alternative, requests, ffmpeg-python or direct ffmpeg subprocess calls — pick one approach and justify briefly)
- Compression settings should be easily configurable (bitrate, mono/stereo, codec) via environment variables, with sensible voice-optimized defaults
- Code must include comments explaining any non-obvious step, especially the Local Bot API Server setup and the WordPress Application Password auth flow
- Assume Docker and docker-compose are available on the homelab host; do not assume any specific reverse proxy beyond what's mentioned in context
</constraints>

<instructions>
For complex or ambiguous parts (e.g., exact WordPress REST API endpoint behavior for a given WP version/theme, or Local Bot API Server networking specifics), think through the approach step-by-step first, then provide the final answer in the requested format.
If information needed to complete a step is missing or uncertain (e.g., WordPress version, specific audio player block preference, whether posts should auto-publish or save as draft), state this explicitly and propose a reasonable default rather than guessing silently.
</instructions>