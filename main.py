import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path

import requests
import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from requests_toolbelt.multipart.encoder import MultipartEncoder


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PIXELDRAIN_API_KEY = os.environ.get("PIXELDRAIN_API_KEY")

MAX_FILE_SIZE = 950 * 1024 * 1024

PIXELDRAIN_UPLOAD_URL = (
    "https://pixeldrain.com/api/file"
)

PIXELDRAIN_API_BASE = (
    "https://pixeldrain.com/api"
)


# ============================================================
# Validation
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is missing."
    )

if not PIXELDRAIN_API_KEY:
    raise RuntimeError(
        "PIXELDRAIN_API_KEY environment variable is missing."
    )


# ============================================================
# URL validation
# ============================================================

URL_REGEX = re.compile(
    r"^https?://[^\s]+$",
    re.IGNORECASE
)


def is_valid_url(text: str) -> bool:
    return bool(
        URL_REGEX.match(
            text.strip()
        )
    )


# ============================================================
# Quality levels
# ============================================================

QUALITY_LEVELS = [
    720,
    576,
    540,
    480,
    360,
    240,
    144,
]


# ============================================================
# Find downloaded media file
# ============================================================

def find_media_file(
    workdir: str,
) -> Path | None:

    media_extensions = {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
    }

    files = [
        p
        for p in Path(workdir).glob("*")
        if p.is_file()
        and p.suffix.lower()
        in media_extensions
    ]

    if not files:
        return None

    return max(
        files,
        key=lambda p: p.stat().st_mtime
    )


# ============================================================
# Clean work directory
# ============================================================

def clean_workdir(
    workdir: str,
):

    for item in Path(workdir).glob("*"):

        if item.is_file():

            try:
                item.unlink()

            except Exception as exc:

                print(
                    f"[CLEANUP] Could not remove "
                    f"{item}: {exc}"
                )


# ============================================================
# yt-dlp download
# ============================================================

def download_video(
    url: str,
    workdir: str,
):
    """
    Try:

    720p
    576p
    540p
    480p
    360p
    240p
    144p

    If the final file is larger than 950 MiB,
    delete it and try the next lower quality.
    """

    output_template = os.path.join(
        workdir,
        "%(title).180s.%(ext)s"
    )

    last_error = None

    for quality in QUALITY_LEVELS:

        print()
        print("=" * 60)

        print(
            f"[DOWNLOAD] Trying maximum quality: "
            f"{quality}p"
        )

        format_selector = (
            f"bv*[height<={quality}]+ba/"
            f"b[height<={quality}]"
        )

        print(
            f"[DOWNLOAD] Format selector: "
            f"{format_selector}"
        )

        clean_workdir(workdir)

        ydl_opts = {
            "format": format_selector,

            "outtmpl": output_template,

            "merge_output_format": "mp4",

            "noplaylist": True,

            "retries": 3,
            "fragment_retries": 3,

            "quiet": True,
            "no_warnings": True,

            "restrictfilenames": True,
        }

        try:

            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                info = ydl.extract_info(
                    url,
                    download=True,
                )

                downloaded_path = Path(
                    ydl.prepare_filename(
                        info
                    )
                )

            # ------------------------------------------------
            # Locate merged file
            # ------------------------------------------------

            if not downloaded_path.exists():

                downloaded_path = (
                    find_media_file(
                        workdir
                    )
                )

                if downloaded_path is None:

                    raise RuntimeError(
                        "Downloaded file could "
                        "not be located."
                    )

            # ------------------------------------------------
            # Check actual final size
            # ------------------------------------------------

            actual_size = (
                downloaded_path.stat().st_size
            )

            actual_size_mib = (
                actual_size
                / (1024 * 1024)
            )

            print(
                f"[DOWNLOAD] Final file: "
                f"{downloaded_path.name}"
            )

            print(
                f"[DOWNLOAD] Final size: "
                f"{actual_size_mib:.2f} MiB"
            )

            # ------------------------------------------------
            # Accept
            # ------------------------------------------------

            if actual_size <= MAX_FILE_SIZE:

                print(
                    f"[DOWNLOAD] Accepted: "
                    f"{quality}p"
                )

                print("=" * 60)

                return downloaded_path

            # ------------------------------------------------
            # Too large
            # ------------------------------------------------

            print(
                f"[DOWNLOAD] {quality}p is larger "
                f"than 950 MiB."
            )

            print(
                "[DOWNLOAD] Removing file..."
            )

            clean_workdir(
                workdir
            )

            print(
                "[DOWNLOAD] Trying lower quality..."
            )

        except yt_dlp.utils.DownloadError as exc:

            last_error = exc

            print(
                f"[DOWNLOAD] {quality}p failed:"
            )

            print(exc)

            clean_workdir(
                workdir
            )

            continue

        except Exception as exc:

            last_error = exc

            print(
                f"[DOWNLOAD] {quality}p failed:"
            )

            print(exc)

            clean_workdir(
                workdir
            )

            continue

    print("=" * 60)

    if last_error:

        raise RuntimeError(
            "Could not download a suitable "
            "quality below 950 MiB. "
            f"Last error: {last_error}"
        )

    raise RuntimeError(
        "Could not download a suitable "
        "quality below 950 MiB."
    )


# ============================================================
# Pixeldrain upload
# ============================================================

def upload_to_pixeldrain(
    file_path: Path,
):
    """
    Upload using streaming multipart.
    """

    print()
    print(
        "[PIXELDRAIN] Starting upload:"
    )

    print(
        f"[PIXELDRAIN] File: "
        f"{file_path}"
    )

    print(
        f"[PIXELDRAIN] Size: "
        f"{file_path.stat().st_size} bytes"
    )

    file = file_path.open("rb")

    try:

        encoder = MultipartEncoder(
            fields={
                "file": (
                    file_path.name,
                    file,
                    "application/octet-stream",
                )
            }
        )

        response = requests.post(
            PIXELDRAIN_UPLOAD_URL,

            auth=(
                "",
                PIXELDRAIN_API_KEY,
            ),

            data=encoder,

            headers={
                "Content-Type":
                    encoder.content_type,
            },

            timeout=60 * 60,
        )

        print(
            f"[PIXELDRAIN] HTTP "
            f"{response.status_code}"
        )

        print(
            f"[PIXELDRAIN] Response: "
            f"{response.text[:2000]}"
        )

        if not response.ok:

            raise RuntimeError(
                "Pixeldrain upload failed: "
                f"HTTP {response.status_code} - "
                f"{response.text[:1000]}"
            )

        data = response.json()

        file_id = data.get("id")

        if not file_id:

            raise RuntimeError(
                "Pixeldrain did not return "
                f"file ID: {data}"
            )

        pixeldrain_url = (
            f"https://pixeldrain.com/u/"
            f"{file_id}"
        )

        print(
            f"[PIXELDRAIN] Upload successful:"
        )

        print(
            pixeldrain_url
        )

        return (
            pixeldrain_url,
            file_id,
        )

    finally:

        file.close()


# ============================================================
# Pixeldrain delete one file
# ============================================================

def delete_pixeldrain_file(
    file_id: str,
):
    """
    Delete one file owned by the API-key account.
    """

    url = (
        f"{PIXELDRAIN_API_BASE}"
        f"/file/{file_id}"
    )

    print(
        f"[PIXELDRAIN] Deleting file: "
        f"{file_id}"
    )

    response = requests.delete(
        url,

        auth=(
            "",
            PIXELDRAIN_API_KEY,
        ),

        timeout=60,
    )

    print(
        f"[PIXELDRAIN] Delete HTTP: "
        f"{response.status_code}"
    )

    print(
        f"[PIXELDRAIN] Delete response: "
        f"{response.text[:1000]}"
    )

    if not response.ok:

        raise RuntimeError(
            "Pixeldrain delete failed: "
            f"HTTP {response.status_code} - "
            f"{response.text[:1000]}"
        )

    return True


# ============================================================
# Pixeldrain list all files
# ============================================================

def get_pixeldrain_files():
    """
    Get files belonging to the API-key account.
    """

    url = (
        f"{PIXELDRAIN_API_BASE}"
        f"/user/files"
    )

    print(
        "[PIXELDRAIN] Getting user files..."
    )

    response = requests.get(
        url,

        auth=(
            "",
            PIXELDRAIN_API_KEY,
        ),

        timeout=60,
    )

    print(
        f"[PIXELDRAIN] List HTTP: "
        f"{response.status_code}"
    )

    print(
        f"[PIXELDRAIN] List response: "
        f"{response.text[:2000]}"
    )

    if not response.ok:

        raise RuntimeError(
            "Could not get Pixeldrain "
            "file list: "
            f"HTTP {response.status_code} - "
            f"{response.text[:1000]}"
        )

    data = response.json()

    # Pixeldrain returns the user's files
    # in the "files" field.
    return data.get(
        "files",
        []
    )


# ============================================================
# Delete all Pixeldrain files
# ============================================================

def delete_all_pixeldrain_files():
    """
    Delete every file belonging to the
    configured Pixeldrain API-key account.
    """

    files = get_pixeldrain_files()

    print(
        f"[PIXELDRAIN] Files found: "
        f"{len(files)}"
    )

    deleted = 0
    failed = 0

    for file_info in files:

        file_id = file_info.get("id")

        if not file_id:
            continue

        try:

            delete_pixeldrain_file(
                file_id
            )

            deleted += 1

        except Exception as exc:

            failed += 1

            print(
                f"[PIXELDRAIN] Failed to "
                f"delete {file_id}: "
                f"{exc}"
            )

    return deleted, failed


# ============================================================
# Delete button keyboard
# ============================================================

def get_delete_keyboard(
    file_id: str,
):

    keyboard = [
        [
            InlineKeyboardButton(
                "🗑 حذف این فایل",
                callback_data=(
                    f"delete_file:{file_id}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "⚠️ حذف همه فایل‌ها",
                callback_data="delete_all",
            )
        ],
    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# ============================================================
# Telegram /start
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "لینک ویدئو را ارسال کن.\n\n"
        "ربات ابتدا 720p را امتحان می‌کند. "
        "اگر فایل نهایی بیشتر از 950 MiB "
        "باشد، کیفیت پایین‌تر را امتحان "
        "می‌کند."
    )


# ============================================================
# Telegram URL handler
# ============================================================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if (
        not update.message
        or not update.message.text
    ):
        return

    url = update.message.text.strip()

    if not is_valid_url(url):

        await update.message.reply_text(
            "یک URL معتبر با "
            "http:// یا https:// ارسال کن."
        )

        return

    status = await update.message.reply_text(
        "در حال بررسی لینک..."
    )

    workdir = tempfile.mkdtemp(
        prefix="ytbot_"
    )

    try:

        # ====================================================
        # Download
        # ====================================================

        await status.edit_text(
            "در حال دانلود...\n\n"
            "اول 720p امتحان می‌شود؛ "
            "اگر حجم فایل نهایی بیشتر "
            "از 950 MiB باشد، کیفیت "
            "پایین‌تر انتخاب می‌شود."
        )

        file_path = await asyncio.to_thread(
            download_video,
            url,
            workdir,
        )

        file_size_mib = (
            file_path.stat().st_size
            / (1024 * 1024)
        )

        await status.edit_text(
            f"دانلود انجام شد.\n"
            f"حجم: {file_size_mib:.1f} MiB\n\n"
            f"در حال آپلود به Pixeldrain..."
        )

        # ====================================================
        # Upload
        # ====================================================

        (
            pixeldrain_url,
            file_id,
        ) = await asyncio.to_thread(
            upload_to_pixeldrain,
            file_path,
        )

        await status.edit_text(
            "آپلود انجام شد."
        )

        # ====================================================
        # Send link + buttons
        # ====================================================

        await update.message.reply_text(
            f"لینک Pixeldrain:\n"
            f"{pixeldrain_url}",

            reply_markup=(
                get_delete_keyboard(
                    file_id
                )
            ),
        )

    except yt_dlp.utils.DownloadError as exc:

        error_text = str(exc)

        if (
            "403" in error_text
            or "Forbidden" in error_text
        ):

            message = (
                "سایت مقصد درخواست دانلود "
                "را با HTTP 403 رد کرد."
            )

        else:

            message = (
                f"خطای yt-dlp:\n"
                f"{error_text}"
            )

        if len(message) > 1000:

            message = (
                message[:1000]
                + "..."
            )

        await status.edit_text(
            message
        )

    except Exception as exc:

        error_text = str(exc)

        print()
        print("[ERROR]")
        print(error_text)

        if len(error_text) > 1000:

            error_text = (
                error_text[:1000]
                + "..."
            )

        await status.edit_text(
            f"خطا:\n"
            f"{error_text}"
        )

    finally:

        print(
            f"[CLEANUP] Removing: "
            f"{workdir}"
        )

        shutil.rmtree(
            workdir,
            ignore_errors=True,
        )


# ============================================================
# Callback query handler
# ============================================================

async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    data = query.data or ""

    # ========================================================
    # Delete ONE file
    # ========================================================

    if data.startswith(
        "delete_file:"
    ):

        file_id = data.split(
            ":",
            1
        )[1].strip()

        if not file_id:

            await query.answer(
                "شناسه فایل نامعتبر است.",
                show_alert=True,
            )

            return

        try:

            await query.edit_message_reply_markup(
                reply_markup=None
            )

            await asyncio.to_thread(
                delete_pixeldrain_file,
                file_id,
            )

            await query.edit_message_text(
                "🗑 فایل حذف شد."
            )

        except Exception as exc:

            print(
                f"[DELETE ONE ERROR] "
                f"{exc}"
            )

            await query.edit_message_text(
                "حذف فایل انجام نشد.\n\n"
                f"خطا: {str(exc)[:700]}"
            )

        return

    # ========================================================
    # Ask confirmation for ALL files
    # ========================================================

    if data == "delete_all":

        keyboard = [
            [
                InlineKeyboardButton(
                    "بله، همه را حذف کن",
                    callback_data=(
                        "confirm_delete_all"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "لغو",
                    callback_data=(
                        "cancel_delete_all"
                    ),
                )
            ],
        ]

        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        await query.answer(
            "تأیید لازم است.",
            show_alert=True,
        )

        return

    # ========================================================
    # Confirm ALL
    # ========================================================

    if data == "confirm_delete_all":

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        try:

            await query.edit_message_text(
                "⏳ در حال حذف همه فایل‌های "
                "Pixeldrain..."
            )

            deleted, failed = (
                await asyncio.to_thread(
                    delete_all_pixeldrain_files
                )
            )

            if failed == 0:

                message = (
                    "✅ همه فایل‌ها حذف شدند.\n"
                    f"تعداد حذف‌شده: {deleted}"
                )

            else:

                message = (
                    "حذف انجام شد، اما برخی "
                    "فایل‌ها حذف نشدند.\n\n"
                    f"حذف‌شده: {deleted}\n"
                    f"ناموفق: {failed}"
                )

            await query.edit_message_text(
                message
            )

        except Exception as exc:

            print(
                f"[DELETE ALL ERROR] "
                f"{exc}"
            )

            await query.edit_message_text(
                "❌ حذف همه فایل‌ها انجام نشد.\n\n"
                f"خطا: {str(exc)[:700]}"
            )

        return

    # ========================================================
    # Cancel ALL
    # ========================================================

    if data == "cancel_delete_all":

        keyboard = []

        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        await query.answer(
            "لغو شد.",
            show_alert=True,
        )

        return


# ============================================================
# Main
# ============================================================

def main():

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_url,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            handle_callback
        )
    )

    print(
        "Bot is running..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
