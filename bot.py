"""
Telegram → ffmpeg → WordPress pipeline.

Receives audio/voice files from a private Telegram bot, compresses them,
uploads to WordPress Media Library, organizes into Syaikh & Book categories,
creates a post using the original recording timestamp, and replies with the URL.

Dependencies: python-telegram-bot[job-queue]>=21, requests
Runtime deps: ffmpeg and ffprobe must be on PATH inside the container.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

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

# Conversation states
WAITING_BOOK_TITLE, WAITING_SYAIKH_AND_BOOK = range(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """ASCII slug: lowercase, hyphens, no consecutive hyphens."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:80]


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


def get_file_timestamp(src_path: Path, default_ts: datetime) -> datetime:
    """Extract audio creation/recording time via ffprobe, falling back to mtime or default_ts."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_entries", "format_tags=creation_time,date,datetime,encoded_date",
            str(src_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            tags = data.get("format", {}).get("tags", {})
            for key in ["creation_time", "date", "datetime", "encoded_date"]:
                val = tags.get(key)
                if val:
                    val_str = str(val).strip().replace("Z", "+00:00")
                    try:
                        dt = datetime.fromisoformat(val_str)
                        if not dt.tzinfo:
                            dt = dt.replace(tzinfo=timezone.utc)
                        log.info("Extracted recording timestamp from ffprobe tags (%s): %s", key, dt)
                        return dt
                    except ValueError:
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                            try:
                                dt = datetime.strptime(val_str, fmt).replace(tzinfo=timezone.utc)
                                log.info("Extracted recording timestamp from ffprobe tags (%s): %s", key, dt)
                                return dt
                            except ValueError:
                                pass
    except Exception as exc:
        log.warning("ffprobe timestamp extraction failed: %s", exc)

    try:
        mtime = src_path.stat().st_mtime
        if mtime > 0:
            mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            if abs((default_ts - mtime_dt).total_seconds()) > 300:
                log.info("Using file st_mtime timestamp: %s", mtime_dt)
                return mtime_dt
    except Exception:
        pass

    log.info("Using Telegram message timestamp: %s", default_ts)
    return default_ts


def compress_audio(src: Path, dest: Path) -> None:
    """
    Compress src → dest via ffmpeg.
    Raises subprocess.CalledProcessError on failure.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vn",
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
    """Upload file to WP Media Library via REST API."""
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


def wp_get_categories() -> list[dict]:
    """Fetch all categories from WP REST API."""
    url = f"{WP_SITE_URL}/wp-json/wp/v2/categories?per_page=100"
    resp = requests.get(
        url,
        auth=(WP_USER, WP_APP_PASSWORD),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def wp_create_category(name: str, parent_id: int | None = None) -> dict:
    """Create a WP Category (or Subcategory if parent_id provided)."""
    url = f"{WP_SITE_URL}/wp-json/wp/v2/categories"
    payload = {"name": name}
    if parent_id:
        payload["parent"] = parent_id
    resp = requests.post(
        url,
        auth=(WP_USER, WP_APP_PASSWORD),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def wp_create_post(
    title: str,
    media_id: int,
    media_url: str,
    category_ids: list[int] | None = None,
    post_date: datetime | None = None,
) -> str:
    """Create a WP post with native Gutenberg audio block, categories, and recording date."""
    audio_block = (
        f'<!-- wp:audio {{"id":{media_id}}} -->\n'
        f'<figure class="wp-block-audio">'
        f'<audio controls src="{media_url}"></audio>'
        f'</figure>\n'
        f'<!-- /wp:audio -->'
    )

    payload = {
        "title":   title,
        "content": audio_block,
        "status":  WP_POST_STATUS,
    }
    if category_ids:
        payload["categories"] = category_ids
    if post_date:
        payload["date_gmt"] = post_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    resp = requests.post(
        f"{WP_SITE_URL}/wp-json/wp/v2/posts",
        auth=(WP_USER, WP_APP_PASSWORD),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["link"]


def get_books_keyboard(syaikh_filter_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Build keyboard showing list of Books directly by default."""
    try:
        categories = wp_get_categories()
        cat_map = {c["id"]: c for c in categories}
        books = [c for c in categories if c["parent"] != 0]

        if syaikh_filter_id:
            books = [b for b in books if b["parent"] == syaikh_filter_id]
            syaikh_name = cat_map.get(syaikh_filter_id, {}).get("name", "Syaikh")
            heading = f"📖 Pilih Buku (Filter: **{syaikh_name}**):"
        else:
            heading = "📖 Pilih Buku untuk postingan kajian ini:"

        keyboard = []
        for b in books:
            parent_name = cat_map.get(b["parent"], {}).get("name", "Umum")
            label = f"📖 {b['name']} ({parent_name})"
            keyboard.append([InlineKeyboardButton(label[:50], callback_data=f"cat_set_{b['id']}_{b['parent']}")])

        action_row = []
        if syaikh_filter_id:
            action_row.append(InlineKeyboardButton("📚 Semua Buku", callback_data="cat_show_all_books"))
        else:
            action_row.append(InlineKeyboardButton("👤 Filter per Syaikh", callback_data="cat_filter_syaikh"))

        action_row.append(InlineKeyboardButton("➕ Buku Baru", callback_data="cat_new_book"))
        keyboard.append(action_row)

        keyboard.append([InlineKeyboardButton("⏩ Tanpa Buku (Umum)", callback_data="cat_skip")])

        return heading, InlineKeyboardMarkup(keyboard)
    except Exception as exc:
        log.exception("Failed to build books keyboard: %s", exc)
        keyboard = [
            [InlineKeyboardButton("➕ Tambah Buku Baru", callback_data="cat_new_book")],
            [InlineKeyboardButton("⏩ Tanpa Buku (Umum)", callback_data="cat_skip")],
        ]
        return "Pilih opsi kategori:", InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Telegram Handlers
# ---------------------------------------------------------------------------

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg   = update.message
    audio = msg.voice or msg.audio
    if not audio:
        return ConversationHandler.END

    msg_ts = msg.date.replace(tzinfo=timezone.utc)
    status_msg = await msg.reply_text("⏳ Sedang memproses audio...")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        src_ext  = Path(audio.file_name).suffix if getattr(audio, "file_name", None) else ".ogg"
        src_path = tmp / f"raw{src_ext}"

        try:
            tg_file = await audio.get_file()
            await tg_file.download_to_drive(src_path)
            log.info("Downloaded %s → %s", audio.file_id, src_path)

            rec_ts = get_file_timestamp(src_path, default_ts=msg_ts)
            stem, title = make_filename(msg.caption, rec_ts)
            out_filename = f"{stem}.{OUTPUT_EXT}"
            out_path = tmp / out_filename

            compress_audio(src_path, out_path)
            log.info("Compressed → %s (%.1f KB)", out_filename, out_path.stat().st_size / 1024)

            media = wp_upload_media(out_path, out_filename)
            context.user_data["pending_post"] = {
                "media_id": media["id"],
                "media_url": media["source_url"],
                "title": title,
                "post_date": rec_ts.isoformat(),
            }

            heading, reply_markup = get_books_keyboard()
            await status_msg.edit_text(
                f"✅ Audio berhasil di-upload!\n📌 **Judul**: {title}\n📅 **Waktu Rekam**: {rec_ts.strftime('%Y-%m-%d %H:%M:%S')}\n\n{heading}",
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        except TimedOut as exc:
            log.warning("Telegram API TimedOut during file processing: %s", exc)
            await status_msg.edit_text("⚠️ Proses timeout (koneksi lambat). Silakan kirim ulang audio kamu.")
            return ConversationHandler.END

        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
            await status_msg.edit_text(f"⚠️ Error tidak terduga ({type(exc).__name__}): {exc}")
            return ConversationHandler.END


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    pending = context.user_data.get("pending_post")

    if not pending and not data.startswith("cat_newsyaikh_"):
        await query.edit_message_text("⚠️ Data postingan tidak ditemukan atau sudah diproses. Silakan kirim audio ulang.")
        return ConversationHandler.END

    post_date = datetime.fromisoformat(pending["post_date"]) if pending and pending.get("post_date") else None

    if data == "cat_skip":
        try:
            post_url = wp_create_post(
                pending["title"],
                pending["media_id"],
                pending["media_url"],
                post_date=post_date,
            )
            await query.edit_message_text(f"✅ Kajian tayang: {post_url}")
        except Exception as exc:
            log.exception("Failed creating post")
            await query.edit_message_text(f"⚠️ Gagal membuat post: {exc}")
        context.user_data.pop("pending_post", None)
        return ConversationHandler.END

    elif data == "cat_show_all_books":
        heading, reply_markup = get_books_keyboard()
        await query.edit_message_text(heading, reply_markup=reply_markup, parse_mode="Markdown")
        return ConversationHandler.END

    elif data == "cat_filter_syaikh":
        try:
            categories = wp_get_categories()
            syaikhs = [c for c in categories if c["parent"] == 0]
            keyboard = []
            for s in syaikhs:
                keyboard.append([InlineKeyboardButton(f"👤 {s['name']}", callback_data=f"cat_syaikh_filter_{s['id']}")])
            keyboard.append([InlineKeyboardButton("📚 Semua Buku", callback_data="cat_show_all_books")])

            await query.edit_message_text("Pilih Syaikh untuk menyaring daftar buku:", reply_markup=InlineKeyboardMarkup(keyboard))
            return ConversationHandler.END
        except Exception as exc:
            log.exception("Failed fetching Syaikh filter list")
            await query.edit_message_text(f"⚠️ Gagal mengambil daftar Syaikh: {exc}")
            return ConversationHandler.END

    elif data.startswith("cat_syaikh_filter_"):
        syaikh_id = int(data.split("_")[3])
        heading, reply_markup = get_books_keyboard(syaikh_filter_id=syaikh_id)
        await query.edit_message_text(heading, reply_markup=reply_markup, parse_mode="Markdown")
        return ConversationHandler.END

    elif data.startswith("cat_set_"):
        parts = data.split("_")
        book_id, syaikh_id = int(parts[2]), int(parts[3])
        category_ids = [book_id]
        if syaikh_id:
            category_ids.append(syaikh_id)

        try:
            post_url = wp_create_post(
                pending["title"],
                pending["media_id"],
                pending["media_url"],
                category_ids=category_ids,
                post_date=post_date,
            )
            await query.edit_message_text(f"✅ Kajian tayang: {post_url}")
        except Exception as exc:
            log.exception("Failed creating post")
            await query.edit_message_text(f"⚠️ Gagal membuat post: {exc}")

        context.user_data.pop("pending_post", None)
        return ConversationHandler.END

    elif data == "cat_new_book":
        try:
            categories = wp_get_categories()
            syaikhs = [c for c in categories if c["parent"] == 0]
            keyboard = []
            for s in syaikhs:
                keyboard.append([InlineKeyboardButton(f"👤 {s['name']}", callback_data=f"cat_newsyaikh_select_{s['id']}")])
            keyboard.append([InlineKeyboardButton("➕ Syaikh Baru", callback_data="cat_newsyaikh_create")])

            await query.edit_message_text("Pilih Syaikh yang mengajar atau buat Syaikh baru:", reply_markup=InlineKeyboardMarkup(keyboard))
            return ConversationHandler.END

        except Exception as exc:
            log.exception("Failed fetching Syaikh list")
            await query.edit_message_text(f"⚠️ Gagal mengambil daftar Syaikh: {exc}")
            return ConversationHandler.END

    elif data.startswith("cat_newsyaikh_select_"):
        syaikh_id = int(data.split("_")[3])
        context.user_data["new_book_syaikh_id"] = syaikh_id
        await query.edit_message_text("Silakan ketik **Judul Buku Baru** yang ingin ditambahkan:")
        return WAITING_BOOK_TITLE

    elif data == "cat_newsyaikh_create":
        await query.edit_message_text("Silakan ketik **Nama Syaikh** & **Judul Buku** baru\n(Format: `Nama Syaikh | Judul Buku`):", parse_mode="Markdown")
        return WAITING_SYAIKH_AND_BOOK

    return ConversationHandler.END


async def handle_input_book_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    book_title = update.message.text.strip()
    syaikh_id = context.user_data.get("new_book_syaikh_id")
    pending = context.user_data.get("pending_post")

    if not pending or not syaikh_id:
        await update.message.reply_text("⚠️ Sesi habis atau data tidak valid. Silakan kirim audio lagi.")
        return ConversationHandler.END

    post_date = datetime.fromisoformat(pending["post_date"]) if pending.get("post_date") else None

    try:
        new_book_cat = wp_create_category(book_title, parent_id=syaikh_id)
        cat_ids = [new_book_cat["id"], syaikh_id]
        post_url = wp_create_post(
            pending["title"],
            pending["media_id"],
            pending["media_url"],
            category_ids=cat_ids,
            post_date=post_date,
        )
        await update.message.reply_text(f"✅ Buku baru '{book_title}' dibuat!\n✅ Kajian tayang: {post_url}")
    except Exception as exc:
        log.exception("Failed creating book category")
        await update.message.reply_text(f"⚠️ Gagal membuat kategori buku: {exc}")

    context.user_data.clear()
    return ConversationHandler.END


async def handle_input_syaikh_and_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    pending = context.user_data.get("pending_post")

    if not pending:
        await update.message.reply_text("⚠️ Sesi habis. Silakan kirim audio lagi.")
        return ConversationHandler.END

    if "|" not in text:
        await update.message.reply_text("⚠️ Format salah! Gunakan format: `Nama Syaikh | Judul Buku`\nContoh: `Syaikh Al-Utsaimin | Syarah Kitabut Tauhid`", parse_mode="Markdown")
        return WAITING_SYAIKH_AND_BOOK

    syaikh_name, book_title = [p.strip() for p in text.split("|", 1)]
    post_date = datetime.fromisoformat(pending["post_date"]) if pending.get("post_date") else None

    try:
        syaikh_cat = wp_create_category(syaikh_name)
        book_cat = wp_create_category(book_title, parent_id=syaikh_cat["id"])
        cat_ids = [book_cat["id"], syaikh_cat["id"]]
        post_url = wp_create_post(
            pending["title"],
            pending["media_id"],
            pending["media_url"],
            category_ids=cat_ids,
            post_date=post_date,
        )
        await update.message.reply_text(f"✅ Syaikh '{syaikh_name}' & Buku '{book_title}' dibuat!\n✅ Kajian tayang: {post_url}")
    except Exception as exc:
        log.exception("Failed creating Syaikh & Book categories")
        await update.message.reply_text(f"⚠️ Gagal membuat kategori: {exc}")

    context.user_data.clear()
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(f"{LOCAL_API_URL}/bot")
        .base_file_url(f"{LOCAL_API_URL}/file/bot")
        .local_mode(True)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.AUDIO | filters.VOICE, handle_audio),
            CallbackQueryHandler(handle_callback_query),
        ],
        states={
            WAITING_BOOK_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input_book_title)
            ],
            WAITING_SYAIKH_AND_BOOK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input_syaikh_and_book)
            ],
        },
        fallbacks=[],
        per_user=True,
        per_message=False,
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    log.info("Bot starting (local API: %s)", LOCAL_API_URL)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
