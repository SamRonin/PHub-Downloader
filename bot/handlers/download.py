import asyncio
import logging
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

from bot.config import settings
from bot.db import db
from bot.services import pixeldrain
from bot.services.downloader import download_video
from bot.services.hls_stream import try_stream_to_pixeldrain
from bot.services.ph import PornHubBlockedError
from bot.services.quota import check_quota_for_size, get_free_speed_limit, fmt_quota
from bot.utils.cleanup import delete_path
from bot.utils.helpers import fmt_size
from bot.utils.i18n import t
from bot.handlers.cache import info_cache

logger = logging.getLogger(__name__)
router = Router(name="download")

sem = asyncio.Semaphore(settings.MAX_CONCURRENT_DOWNLOADS)
busy_users: set[int] = set()
_safe_name_re = re.compile(r"[^A-Za-z0-9._-]+")

# When a big file would not fit comfortably in the free-tier 1 GB ephemeral
# disk, stream it straight to Pixeldrain instead of staging it locally.
_DISK_HEADROOM = 96 * 1024 * 1024  # keep ~100 MB free for the DB / other tasks


def _free_disk_bytes() -> int | None:
    try:
        d = Path(settings.TEMP_DIR)
        d.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(d).free
    except Exception:
        return None


def _estimated_size(info: dict, height: int) -> int | None:
    """Approximate size (bytes) from duration × bitrate, or None when unknown."""
    dur = info.get("duration") or 0
    tbr = (info.get("qualities") or {}).get(height, {}).get("tbr") or 0
    if not dur or not tbr:
        return None
    return int(dur * tbr * 1000 / 8)


async def _progress_editor(
    bot: Bot, chat_id: int, message_id: int, state: dict, lang: str, stop: asyncio.Event
):
    _PHASE_KEY = {"download": "downloading", "upload": "uploading"}
    last_pct = -1
    last_phase = None
    try:
        while not stop.is_set():
            phase = state["phase"]
            pct = state["pct"]
            if phase != last_phase or pct != last_pct:
                last_phase = phase
                last_pct = pct
                key = _PHASE_KEY.get(phase, "sending")
                try:
                    await bot.edit_message_text(
                        t(lang, key, pct=pct), chat_id=chat_id, message_id=message_id
                    )
                except Exception:
                    pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass


@router.callback_query(F.data.startswith("dl:"))
async def cb_download(cq: CallbackQuery, bot: Bot):
    try:
        _, height_s, cid = cq.data.split(":")
        height = int(height_s)
    except ValueError:
        await cq.answer()
        return

    user = await db.get_user(cq.from_user.id)
    lang = (user["lang"] if user else None) or "fa"

    if user is None or user["banned"]:
        await cq.answer(t(lang, "banned"), show_alert=True)
        return

    info = info_cache.get(cid)
    if not info or height not in info["qualities"]:
        await cq.answer(t(lang, "expired"), show_alert=True)
        return

    if cq.from_user.id in busy_users:
        await cq.answer(t(lang, "already_processing"), show_alert=True)
        return

    q = info["qualities"][height]
    ok, summary = await check_quota_for_size(user, q["size"])
    if not ok:
        await cq.answer()
        await bot.send_message(
            cq.from_user.id,
            t(
                lang,
                "quota_exceeded",
                used=fmt_quota(summary["used"]),
                quota=fmt_quota(summary["quota"]),
            ),
        )
        return

    await cq.answer()
    busy_users.add(cq.from_user.id)
    status_msg = None
    task_dir = None
    stop = asyncio.Event()
    editor_task = None
    try:
        status_msg = await bot.send_message(cq.from_user.id, t(lang, "downloading", pct=0))
        state = {"phase": "download", "pct": 0}
        editor_task = asyncio.create_task(
            _progress_editor(bot, cq.from_user.id, status_msg.message_id, state, lang, stop)
        )

        is_pro = bool(user["is_pro"] and user["pro_until"] > int(time.time()))
        rate_limit = None if is_pro else await get_free_speed_limit()

        url = info.get("webpage_url")
        fmt_spec = q.get("format_spec") or str(q["format_id"])
        path: Path | None = None
        file_id: str | None = None
        size: int | None = None
        delivered: str | None = None

        # --- Streaming path: big files (Pixeldrain-bound anyway) that would
        # not fit safely in the 1 GB ephemeral disk are streamed straight from
        # PornHub to Pixeldrain, never staged on the local disk.
        est = _estimated_size(info, height)
        free = _free_disk_bytes()
        if est and est > settings.TELEGRAM_LIMIT and free and free < est + _DISK_HEADROOM:
            safe_name = _safe_name_re.sub("_", f"{info.get('id') or 'video'}_{height}p")[:80]
            safe_name += ".mp4"
            logger.info(
                "Streaming %dp direct to Pixeldrain (est %s, disk free %s)",
                height, fmt_size(est), fmt_size(free),
            )
            state["phase"] = "upload"
            async with sem:
                streamed = await try_stream_to_pixeldrain(
                    url, height, safe_name, state, rate_limit or 0
                )
            if streamed:
                file_id, size = streamed
                delivered = "pixeldrain"
                await db.record_upload(cq.from_user.id, file_id, size)
                logger.info("Streamed upload OK for user %s", cq.from_user.id)

        # --- Classic path: stage locally (small files, or when streaming was
        # not possible / not needed).
        if delivered is None:
            task_dir = tempfile.mkdtemp(prefix=f"dl_{cq.from_user.id}_", dir=settings.TEMP_DIR)
            async with sem:
                # state is passed in so download_video updates the SAME dict
                # the editor is watching — the chat % used to sit at 0%.
                path, _ = await download_video(url, fmt_spec, task_dir, rate_limit, state)
            size = path.stat().st_size

            if size <= settings.TELEGRAM_LIMIT:
                delivered = "telegram"
                state["phase"] = "send"
                try:
                    await bot.edit_message_text(
                        t(lang, "sending"), chat_id=cq.from_user.id, message_id=status_msg.message_id
                    )
                except Exception:
                    pass
                await bot.send_video(
                    cq.from_user.id,
                    video=FSInputFile(path),
                    caption=t(lang, "done_direct", quality=height, size=fmt_size(size)),
                    supports_streaming=True,
                )
            else:
                state["phase"] = "upload"
                safe_name = _safe_name_re.sub("_", f"{info.get('id') or 'video'}_{height}p")[:80]
                safe_name += path.suffix or ".mp4"
                file_id = await pixeldrain.upload_file(str(path), safe_name, state)
                await db.record_upload(cq.from_user.id, file_id, size)
                delivered = "pixeldrain"

        # --- Pixeldrain delivery result (streamed or uploaded) ---
        if delivered == "pixeldrain" and file_id:
            minutes = int(await db.get_setting("px_delete_minutes"))
            await bot.send_message(
                cq.from_user.id,
                t(lang, "done_link", quality=height, size=fmt_size(size),
                  link=pixeldrain.file_page_url(file_id), minutes=minutes),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=t(lang, "direct_btn"),
                                url=pixeldrain.file_direct_url(file_id),
                            )
                        ]
                    ]
                ),
                disable_web_page_preview=True,
            )

        if delivered:
            await db.record_download(cq.from_user.id, str(height), size or 0, delivered)
        state["pct"] = 100
        stop.set()
        try:
            await bot.edit_message_text(
                t(lang, "done_direct", quality=height, size=fmt_size(size))
                if delivered == "telegram"
                else t(lang, "done"),
                chat_id=cq.from_user.id,
                message_id=status_msg.message_id,
            )
        except Exception:
            pass

    except PornHubBlockedError:
        logger.error("PornHub BLOCKED for download (user %s)", cq.from_user.id)
        stop.set()
        if status_msg:
            try:
                await bot.edit_message_text(
                    t(lang, "ph_blocked"), chat_id=cq.from_user.id, message_id=status_msg.message_id
                )
            except Exception:
                pass
    except Exception as e:
        logger.exception("Download failed for user %s", cq.from_user.id)
        stop.set()
        if status_msg:
            try:
                await bot.edit_message_text(
                    t(lang, "failed"), chat_id=cq.from_user.id, message_id=status_msg.message_id
                )
            except Exception:
                pass
    finally:
        stop.set()
        if editor_task:
            editor_task.cancel()
        if task_dir:
            delete_path(task_dir)
        busy_users.discard(cq.from_user.id)
