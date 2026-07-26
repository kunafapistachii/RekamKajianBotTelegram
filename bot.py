"""
Telegram → ffmpeg → WordPress pipeline.

Receives audio/voice files from a private Telegram bot, compresses them,
uploads to WordPress Media Library, creates a post, and replies with the URL.

Dependencies: python-telegram-bot[job-queue]>=21, requests
Runtime deps: ffmpeg must be on PATH inside the container.
"""

import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ---------------------------------------------------------------------------
# Logging — stdlib only
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — all from environment, fail fast if missing
# ---------------------------------------------------------------------------
BOT_TOKEN        = os.environ["BOT_TOKEN"]
LOCAL_API_URL    = os.environ.get("LOCAL_API_URL", "http://telegram-bot-api:8081")

WP_SITE_URL      = os.environ["WP_SITE_URL"].rstrip("/")
WP_USER          = os.environ["WP_USER"]
WP_APP_PASSWORD  = os.environ["WP_APP_PASSWORD"]
WP_POST_STATUS   = os.environ.get("WP_POST_STATUS", "publish")

# ffmpeg compression settings — voice-optimized defaults
AUDIO_CODEC      = os.environ.get("AUDIO_CODEC", "libopus")   # libopus or libmp3lame
AUDIO_BITRATE    = os.environ.get("AUDIO_BITRATE", "48k")
AUDIO_CHANNELS   = os.environ.get("AUDIO_CHANNELS", "1")       # 1 = mono
OUTPUT_EXT       = os.environ.get("OUTPUT_EXT", "opus")        # opus or mp3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """ASCII slug: lowercase, hyphens, no consecutive hyphens."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:80]  # cap length so filenames don't explode


def make_filename(caption: str | None, ts: datetime) -> tuple[str, str]:
    """
    Returns (stem, title).
    Filename: kajian_YYYY-MM-DD_HH-MM-SS[_slugified-title]
    """
    ts_str  = ts.strftime("%Y-%m-%d_%H-%M-%S")
    ts_disp = ts.strftime("%Y-%m-%d %H:%M")

    if caption and caption.strip():
        title = caption.strip()
        stem  = f"kajian_{ts_str}_{slugify(title)}"
    else:
        title = f"Kajian {ts_disp}"
        stem  = f"kajian_{ts_str}"

    return stem, title


def compress_audio(src: Path, dest: Path) -> None:
    """
    Compress src → dest via ffmpeg.
    Raises subprocess.CalledProcessError on failure.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vn",                          # drop video stream if any
        "-c:a", AUDIO_CODEC,
        "-b:a", AUDIO_BITRATE,
        "-ac", AUDIO_CHANNELS,
        str(dest),
    ]
    log.info("ffmpeg: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("ffmpeg stderr: %s", result.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stderr)


def wp_upload_media(file_path: Path, filename: str) -> dict:
    """
    Upload file to WP Media Library via REST API.
    Uses HTTP Basic Auth with a WordPress Application Password.

    WP Application Password: generate under Users → Profile → Application Passwords.
    Format "username:xxxx xxxx xxxx xxxx xxxx xxxx" — spaces are fine, requests encodes them.
    """
    url  = f"{WP_SITE_URL}/wp-json/wp/v2/media"
    mime = "audio/ogg" if OUTPUT_EXT == "opus" else "audio/mpeg"

    with file_path.open("rb") as fh:
        resp = requests.post(
            url,
            auth=(WP_USER, WP_APP_PASSWORD),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": mime,
            },
            data=fh,
            timeout=120,
        )
    resp.raise_for_status()
    return resp.json()


def wp_create_post(title: str, media_id: int, media_url: str) -> str:
    """
    Create a WP post with a native Gutenberg audio block.
    Returns the published post URL.
    """
    # Native WordPress audio block — no plugin required
    audio_block = (
        f'<!-- wp:audio {{"id":{media_id}}} -->\n'
        f'<figure class="wp-block-audio">'
        f'<audio controls src="{media_url}"></audio>'
        f'</figure>\n'
        f'<!-- /wp:audio -->'
    )

    resp = requests.post(
        f"{WP_SITE_URL}/wp-json/wp/v2/posts",
        auth=(WP_USER, WP_APP_PASSWORD),
        json={
            "title":   title,
            "content": audio_block,
            "status":  WP_POST_STATUS,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["link"]


# ---------------------------------------------------------------------------
# Telegram handler
# ---------------------------------------------------------------------------

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg   = update.message
    audio = msg.voice or msg.audio   # covers both Voice messages and Audio files
    if not audio:
        return

    ts = msg.date.replace(tzinfo=timezone.utc)
    stem, title = make_filename(msg.caption, ts)
    out_filename = f"{stem}.{OUTPUT_EXT}"

    await msg.reply_text("⏳ Sedang diproses…")

    # tempfile.TemporaryDirectory auto-deletes on exit (success or exception)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        src_ext  = Path(audio.file_name).suffix if getattr(audio, "file_name", None) else ".ogg"
        src_path = tmp / f"raw{src_ext}"
        out_path = tmp / out_filename

        try:
            # Download via Local Bot API Server.
            # The Application is built with local_mode=True so PTB routes
            # file downloads through LOCAL_API_URL, not api.telegram.org.
            tg_file = await audio.get_file()
            await tg_file.download_to_drive(src_path)
            log.info("Downloaded %s → %s", audio.file_id, src_path)

            compress_audio(src_path, out_path)
            log.info("Compressed → %s (%.1f KB)", out_filename, out_path.stat().st_size / 1024)

            media    = wp_upload_media(out_path, out_filename)
            post_url = wp_create_post(title, media["id"], media["source_url"])

            log.info("Post created: %s", post_url)
            await msg.reply_text(f"✅ Kajian tayang: {post_url}")

        except subprocess.CalledProcessError as exc:
            short = (exc.output or "")[:200]
            log.exception("ffmpeg failed")
            await msg.reply_text(
                f"⚠️ Gagal memproses file: {short}\n"
                "File asli belum terhapus, coba kirim ulang."
            )

        except requests.HTTPError as exc:
            log.exception("WordPress API error")
            await msg.reply_text(
                f"⚠️ WordPress error: {exc.response.status_code} — {exc.response.text[:200]}"
            )

        except Exception as exc:  # noqa: BLE001 — catch-all for Telegram reply
            log.exception("Unexpected error")
            await msg.reply_text(f"⚠️ Error tidak terduga: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Build the app pointed at the Local Bot API Server.
    # local_mode=True: PTB skips api.telegram.org and uses LOCAL_API_URL for
    # both API calls and file downloads (no 20 MB cap, up to 2 GB).
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(f"{LOCAL_API_URL}/bot")
        .base_file_url(f"{LOCAL_API_URL}/file/bot")
        .local_mode(True)
        .build()
    )

    app.add_handler(
        MessageHandler(filters.AUDIO | filters.VOICE, handle_audio)
    )

    log.info("Bot starting (local API: %s)", LOCAL_API_URL)
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
