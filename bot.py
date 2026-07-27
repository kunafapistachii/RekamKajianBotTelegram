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

MONTH_MAP = {
    "jan": 1, "januari": 1, "january": 1,
    "feb": 2, "februari": 2, "february": 2,
    "mar": 3, "maret": 3, "march": 3,
    "apr": 4, "april": 4,
    "mei": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7,
    "agu": 8, "agustus": 8, "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "okt": 10, "oktober": 10, "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "des": 12, "desember": 12, "dec": 12, "december": 12,
}


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
    ts_disp = ts.strftime("%Y-%m-%d")

    if caption and caption.strip():
        title = caption.strip()
        stem  = f"kajian_{ts_str}_{slugify(title)}"
    else:
        title = f"Kajian {ts_disp}"
        stem  = f"kajian_{ts_str}"

    return stem, title


def extract_date_from_text(text: str) -> tuple[datetime, str] | None:
    """Parse date/time from filename or text using various patterns."""
    if not text:
        return None

    # Pattern 0: Samsung / Android Voice Recorder format: YYMMDD_HHMMSS (e.g. Voice 260721_200611.m4a)
    m = re.search(r"(\d{2})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})", text)
    if m:
        try:
            yy, month, day, hour, minute, second = map(int, m.groups())
            year = 2000 + yy
            if 2020 <= year <= 2035 and 1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59:
                dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
                log.info("Parsed Samsung Voice YYMMDD_HHMMSS date '%s' from text '%s'", dt, text)
                return dt, "Samsung Voice (YYMMDD_HHMMSS)"
        except ValueError:
            pass

    # Pattern 1: ISO 4-digit year YYYY-MM-DD[_ -T]HH-MM-SS or YYYYMMDD[_ -T]HHMMSS
    m = re.search(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})[\s\-_T]?(\d{2})[-_]?(\d{2})[-_]?(\d{2})", text)
    if m:
        try:
            year, month, day, hour, minute, second = map(int, m.groups())
            if 2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59:
                dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
                return dt, "Pattern YYYY-MM-DD HH:MM:SS"
        except ValueError:
            pass

    # Pattern 2: DD-MM-YYYY[_ -T]HH-MM-SS or DD/MM/YYYY
    m = re.search(r"(\d{1,2})[-_\./](\d{1,2})[-_\./](\d{4})(?:[\s\-_T]?(\d{2})[-_]?(\d{2})[-_]?(\d{2}))?", text)
    if m:
        try:
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hour = int(m.group(4)) if m.group(4) else 12
            minute = int(m.group(5)) if m.group(5) else 0
            second = int(m.group(6)) if m.group(6) else 0
            if 2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
                return dt, "Pattern DD-MM-YYYY"
        except ValueError:
            pass

    # Pattern 3: YYYY-MM-DD or YYYYMMDD
    m = re.search(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})", text)
    if m:
        try:
            year, month, day = map(int, m.groups())
            if 2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                dt = datetime(year, month, day, 12, 0, 0, tzinfo=timezone.utc)
                return dt, "Pattern YYYY-MM-DD"
        except ValueError:
            pass

    # Pattern 4: Indonesian/English named date like '22 Juli 2026' or '22-Juli-2026'
    m = re.search(r"(\d{1,2})[\s\-_]+([a-zA-Z]+)[\s\-_]+(\d{4})", text)
    if m:
        try:
            day = int(m.group(1))
            month_str = m.group(2).lower()
            year = int(m.group(3))
            if month_str in MONTH_MAP and 1 <= day <= 31 and 2000 <= year <= 2100:
                month = MONTH_MAP[month_str]
                dt = datetime(year, month, day, 12, 0, 0, tzinfo=timezone.utc)
                return dt, f"Pattern DD Month YYYY ({month_str})"
        except ValueError:
            pass

    return None


def get_file_timestamp(
    tg_file_path: str | None,
    src_path: Path,
    filename: str | None,
    caption: str | None,
    default_ts: datetime,
) -> tuple[datetime, str]:
    """
    Extract audio creation/recording time from:
    1. Original filename (e.g. Voice 260721_200611.m4a or Record_20260722_143000.m4a)
    2. Caption text
    3. File mtime of tg_file_path or src_path (if earlier than upload time)
    4. ffprobe metadata tags
    5. Fallback to Telegram upload date
    """
    # 1. Check original filename FIRST
    if filename:
        res = extract_date_from_text(filename)
        if res:
            dt, src_name = res
            log.info("Found timestamp in filename '%s': %s", filename, dt)
            return dt, f"Filename ({filename})"

    # 2. Check caption text SECOND
    if caption:
        res = extract_date_from_text(caption)
        if res:
            dt, src_name = res
            log.info("Found timestamp in caption '%s': %s", caption, dt)
            return dt, "Caption Text"

    # 3. Check file modification time (mtime) of the original file on disk
    for path_obj in [Path(tg_file_path) if tg_file_path else None, src_path]:
        if path_obj and path_obj.exists():
            try:
                mtime = path_obj.stat().st_mtime
                if mtime > 0:
                    mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                    # If file mtime is earlier than upload time (by > 60 seconds), it's the recording date!
                    if (default_ts - mtime_dt).total_seconds() > 60:
                        log.info("Found file mtime earlier than upload time: %s", mtime_dt)
                        return mtime_dt, f"File MTime ({mtime_dt.strftime('%Y-%m-%d %H:%M')})"
            except Exception as exc:
                log.warning("Failed checking mtime for %s: %s", path_obj, exc)

    # 4. Check ffprobe tags
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_entries", "format_tags",
            str(src_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            tags = data.get("format", {}).get("tags", {})
            for key, val in tags.items():
                if not val:
                    continue
                val_str = str(val).strip().replace("Z", "+00:00")
                try:
                    dt = datetime.fromisoformat(val_str)
                    if not dt.tzinfo:
                        dt = dt.replace(tzinfo=timezone.utc)
                    log.info("Extracted recording timestamp from ffprobe tag '%s' (%s): %s", key, val, dt)
                    return dt, f"Audio Tag ({key})"
                except ValueError:
                    parsed = extract_date_from_text(val_str)
                    if parsed:
                        dt, _ = parsed
                        log.info("Extracted recording timestamp from ffprobe tag '%s' (%s): %s", key, val, dt)
                        return dt, f"Audio Tag ({key})"
    except Exception as exc:
        log.warning("ffprobe timestamp extraction failed: %s", exc)

    # 5. Fallback
    log.info("Using Telegram upload date fallback: %s", default_ts)
    return default_ts, "Telegram Upload Date (Fallback)"


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
        payload["date"]     = post_date.strftime("%Y-%m-%dT%H:%M:%S")
        payload["date_gmt"] = post_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    resp = requests.post(
        f"{WP_SITE_URL}/wp-json/wp/v2/posts",
        auth=(WP_USER, WP_APP_PASSWORD),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["link"]


def get_books_keyboard(msg_id: int, syaikh_filter_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
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
            keyboard.append([InlineKeyboardButton(label[:50], callback_data=f"cat_set_{msg_id}_{b['id']}_{b['parent']}")])

        action_row = []
        if syaikh_filter_id:
            action_row.append(InlineKeyboardButton("📚 Semua Buku", callback_data=f"cat_allbooks_{msg_id}"))
        else:
            action_row.append(InlineKeyboardButton("👤 Filter per Syaikh", callback_data=f"cat_filtersyaikh_{msg_id}"))

        action_row.append(InlineKeyboardButton("➕ Buku Baru", callback_data=f"cat_newbook_{msg_id}"))
        keyboard.append(action_row)

        keyboard.append([InlineKeyboardButton("⏩ Tanpa Buku (Umum)", callback_data=f"cat_skip_{msg_id}")])

        return heading, InlineKeyboardMarkup(keyboard)
    except Exception as exc:
        log.exception("Failed to build books keyboard: %s", exc)
        keyboard = [
            [InlineKeyboardButton("➕ Tambah Buku Baru", callback_data=f"cat_newbook_{msg_id}")],
            [InlineKeyboardButton("⏩ Tanpa Buku (Umum)", callback_data=f"cat_skip_{msg_id}")],
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
    msg_id = status_msg.message_id

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        src_ext  = Path(audio.file_name).suffix if getattr(audio, "file_name", None) else ".ogg"
        src_path = tmp / f"raw{src_ext}"

        try:
            tg_file = await audio.get_file()
            tg_local_path = getattr(tg_file, "file_path", None)

            # Use local file directly if available to save disk space and prevent 'No space left' errors
            if tg_local_path and Path(tg_local_path).exists():
                src_path = Path(tg_local_path)
            else:
                await tg_file.download_to_drive(src_path)
                log.info("Downloaded %s → %s", audio.file_id, src_path)

            orig_filename = getattr(audio, "file_name", None)
            rec_ts, ts_source = get_file_timestamp(
                tg_file_path=tg_local_path,
                src_path=src_path,
                filename=orig_filename,
                caption=msg.caption,
                default_ts=msg_ts,
            )

            stem, title = make_filename(msg.caption, rec_ts)
            out_filename = f"{stem}.{OUTPUT_EXT}"
            out_path = tmp / out_filename

            compress_audio(src_path, out_path)
            log.info("Compressed → %s (%.1f KB)", out_filename, out_path.stat().st_size / 1024)

            media = wp_upload_media(out_path, out_filename)
            pending_posts = context.user_data.setdefault("pending_posts", {})
            pending_posts[msg_id] = {
                "media_id": media["id"],
                "media_url": media["source_url"],
                "title": title,
                "post_date": rec_ts.isoformat(),
            }

            # Cleanup raw file from telegram-bot-api volume so disk never fills up
            if tg_local_path and Path(tg_local_path).exists():
                try:
                    Path(tg_local_path).unlink(missing_ok=True)
                    log.info("Cleaned up raw telegram-bot-api file: %s", tg_local_path)
                except Exception as clean_err:
                    log.warning("Could not clean raw file %s: %s", tg_local_path, clean_err)

            heading, reply_markup = get_books_keyboard(msg_id)
            await status_msg.edit_text(
                f"✅ Audio berhasil di-upload!\n📌 **Judul**: {title}\n📅 **Waktu Rekam**: {rec_ts.strftime('%Y-%m-%d %H:%M:%S')} *(Sumber: {ts_source})*\n\n{heading}",
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

    parts = data.split("_")
    if len(parts) < 3:
        return ConversationHandler.END

    action = parts[1]
    try:
        msg_id = int(parts[2])
    except ValueError:
        return ConversationHandler.END

    pending_posts = context.user_data.get("pending_posts", {})
    pending = pending_posts.get(msg_id)

    if not pending and not action.startswith("newsyaikh"):
        await query.edit_message_text("⚠️ Data postingan ini tidak ditemukan atau sudah diproses. Silakan kirim audio ulang.")
        return ConversationHandler.END

    post_date = datetime.fromisoformat(pending["post_date"]) if pending and pending.get("post_date") else None

    if action == "skip":
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
        pending_posts.pop(msg_id, None)
        return ConversationHandler.END

    elif action == "allbooks":
        heading, reply_markup = get_books_keyboard(msg_id)
        await query.edit_message_text(heading, reply_markup=reply_markup, parse_mode="Markdown")
        return ConversationHandler.END

    elif action == "filtersyaikh":
        try:
            categories = wp_get_categories()
            syaikhs = [c for c in categories if c["parent"] == 0]
            keyboard = []
            for s in syaikhs:
                keyboard.append([InlineKeyboardButton(f"👤 {s['name']}", callback_data=f"cat_syaikhfilter_{msg_id}_{s['id']}")])
            keyboard.append([InlineKeyboardButton("📚 Semua Buku", callback_data=f"cat_allbooks_{msg_id}")])

            await query.edit_message_text("Pilih Syaikh untuk menyaring daftar buku:", reply_markup=InlineKeyboardMarkup(keyboard))
            return ConversationHandler.END
        except Exception as exc:
            log.exception("Failed fetching Syaikh filter list")
            await query.edit_message_text(f"⚠️ Gagal mengambil daftar Syaikh: {exc}")
            return ConversationHandler.END

    elif action == "syaikhfilter":
        syaikh_id = int(parts[3])
        heading, reply_markup = get_books_keyboard(msg_id, syaikh_filter_id=syaikh_id)
        await query.edit_message_text(heading, reply_markup=reply_markup, parse_mode="Markdown")
        return ConversationHandler.END

    elif action == "set":
        book_id, syaikh_id = int(parts[3]), int(parts[4])
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

        pending_posts.pop(msg_id, None)
        return ConversationHandler.END

    elif action == "newbook":
        try:
            categories = wp_get_categories()
            syaikhs = [c for c in categories if c["parent"] == 0]
            keyboard = []
            for s in syaikhs:
                keyboard.append([InlineKeyboardButton(f"👤 {s['name']}", callback_data=f"cat_newsyaikhselect_{msg_id}_{s['id']}")])
            keyboard.append([InlineKeyboardButton("➕ Syaikh Baru", callback_data=f"cat_newsyaikhcreate_{msg_id}")])

            await query.edit_message_text("Pilih Syaikh yang mengajar atau buat Syaikh baru:", reply_markup=InlineKeyboardMarkup(keyboard))
            return ConversationHandler.END

        except Exception as exc:
            log.exception("Failed fetching Syaikh list")
            await query.edit_message_text(f"⚠️ Gagal mengambil daftar Syaikh: {exc}")
            return ConversationHandler.END

    elif action == "newsyaikhselect":
        syaikh_id = int(parts[3])
        context.user_data["active_msg_id"] = msg_id
        context.user_data["new_book_syaikh_id"] = syaikh_id
        await query.edit_message_text("Silakan ketik **Judul Buku Baru** yang ingin ditambahkan:")
        return WAITING_BOOK_TITLE

    elif action == "newsyaikhcreate":
        context.user_data["active_msg_id"] = msg_id
        await query.edit_message_text("Silakan ketik **Nama Syaikh** & **Judul Buku** baru\n(Format: `Nama Syaikh | Judul Buku`):", parse_mode="Markdown")
        return WAITING_SYAIKH_AND_BOOK

    return ConversationHandler.END


async def handle_input_book_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    book_title = update.message.text.strip()
    syaikh_id = context.user_data.get("new_book_syaikh_id")
    msg_id = context.user_data.get("active_msg_id")
    pending_posts = context.user_data.get("pending_posts", {})
    pending = pending_posts.get(msg_id) if msg_id else None

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

    if msg_id:
        pending_posts.pop(msg_id, None)
    return ConversationHandler.END


async def handle_input_syaikh_and_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    msg_id = context.user_data.get("active_msg_id")
    pending_posts = context.user_data.get("pending_posts", {})
    pending = pending_posts.get(msg_id) if msg_id else None

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

    if msg_id:
        pending_posts.pop(msg_id, None)
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
