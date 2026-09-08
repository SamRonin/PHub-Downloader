import asyncio
import io
import logging

import httpx
from aiogram import Router, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BufferedInputFile,
)
from PIL import Image

from bot.services.ph import PornHubBlockedError, extract_info, summarize
from bot.utils.helpers import esc, is_phub_url, row_get
from bot.utils.i18n import t
from bot.handlers.cache import info_cache, detect_lang
from bot.db import db

router = Router(name="info")
logger = logging.getLogger(__name__)

_THUMB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # The CDN hotlink-protects: without a PornHub referer it answers with an
    # HTML block page instead of the image.
    "Referer": "https://www.pornhub.com/",
    "Origin": "https://www.pornhub.com",
}

_THUMB_ATTEMPTS = 3
_MAX_THUMB_EDGE = 1280


def _to_jpeg(data: bytes) -> bytes:
    """Decode whatever the CDN served (JPEG/WebP/AVIF/PNG…) and return a
    plain JPEG. Telegram only reliably shows photos as JPEG/PNG, and the CDN
    varies the codec per request/edge."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        rgb = im.convert("RGB")
        rgb.thumbnail((_MAX_THUMB_EDGE, _MAX_THUMB_EDGE))
        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=85)
        return out.getvalue()


async def _fetch_thumbnail_bytes(url: str, timeout: float = 20.0) -> bytes:
    """Download the thumbnail ourselves right after extraction.

    The thumbnail URL is a short-lived signed CDN link. Passing it to
    Telegram and letting *their* servers fetch it fails intermittently
    (hotlink block / codec not accepted), which is why the cover sometimes
    disappeared. Fetching it here with browser headers and uploading the
    bytes is reliable.
    """
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers=_THUMB_HEADERS
    ) as client:
        last: Exception | None = None
        for attempt in range(_THUMB_ATTEMPTS):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content
                if not data:
                    raise ValueError("empty thumbnail response")
                # check it actually decodes as an image (not an HTML block page)
                with Image.open(io.BytesIO(data)) as probe:
                    probe.load()
                return data
            except Exception as exc:
                last = exc
                if attempt + 1 < _THUMB_ATTEMPTS:
                    await asyncio.sleep(1.0 + attempt * 1.0)
        raise last if last is not None else RuntimeError("thumbnail fetch failed")


def build_caption(info: dict, lang: str) -> str:
    lines = [f"🎬 <b>{esc(info['title'] or '-')}</b>"]
    desc = (info.get("description") or "").strip()
    if desc:
        if len(desc) > 300:
            desc = desc[:300] + "…"
        lines.append(f"📝 {esc(desc)}")
    views = info.get("view_count")
    if views:
        lines.append(t(lang, "views", n=f"{views:,}"))
    lines.append("")
    lines.append(t(lang, "sizes_header"))
    for h in sorted(info["qualities"].keys()):
        q = info["qualities"][h]
        size_str = q["size_str"] or t(lang, "sizes_unknown")
        lines.append(f"  • <b>{h}p</b> — {size_str}")
    return "\n".join(lines)


def build_keyboard(info: dict, lang: str, cid: str) -> InlineKeyboardMarkup:
    rows = []
    for h in sorted(info["qualities"].keys()):
        q = info["qualities"][h]
        size_str = f" ({q['size_str']})" if q["size_str"] else ""
        rows.append(
            [InlineKeyboardButton(text=f"⬇️ {h}p{size_str}", callback_data=f"dl:{h}:{cid}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _answer_with_photo(
    message: Message,
    info: dict,
    lang: str,
    cid: str,
) -> bool:
    """Send the info card. Returns True if a photo was sent."""
    caption = build_caption(info, lang)[:1024]
    kb = build_keyboard(info, lang, cid)
    thumb_url = info.get("thumbnail")

    if thumb_url:
        # 1) download bytes ourselves and upload a clean JPEG
        try:
            data = await _fetch_thumbnail_bytes(thumb_url)
            jpeg = await asyncio.to_thread(_to_jpeg, data)
            await message.answer_photo(
                BufferedInputFile(jpeg, filename="preview.jpg"),
                caption=caption,
                reply_markup=kb,
            )
            return True
        except Exception:
            logger.warning("thumbnail prefetch/conversion failed; trying URL", exc_info=True)
        # 2) let Telegram's servers try the signed URL
        try:
            await message.answer_photo(photo=thumb_url, caption=caption, reply_markup=kb)
            return True
        except Exception:
            logger.warning("answer_photo(thumbnail URL) failed", exc_info=True)

    # 3) text-only card
    try:
        await message.answer(
            build_caption(info, lang)[:4096],
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("failed to send text card")
    return False


@router.message(F.text)
async def handle_link(message: Message):
    text = message.text or ""
    if not is_phub_url(text):
        return
    # Respect the language the user picked (/lang) — a Persian user whose
    # Telegram UI is English used to get the whole card in English.
    user = await db.get_user(message.from_user.id)
    lang = row_get(user, "lang") or detect_lang(message.from_user.language_code)
    status = await message.answer(t(lang, "fetching"))
    try:
        raw = await extract_info(text.strip())
        info = summarize(raw)
    except PornHubBlockedError as e:
        # The video is fine; the server's egress IP is being bounced by
        # PornHub. Tell the user (and log it) instead of a generic failure.
        logger.error("PornHub BLOCKED for %s: %s", text.strip(), e)
        try:
            await status.edit_text(t(lang, "ph_blocked"))
        except Exception:
            pass
        return
    except Exception as e:
        # A wrong reply ("invalid link") would be misleading here: the URL may be
        # perfectly fine while Pornhub blocks/redirects the server (missing
        # curl_cffi impersonation) or the video is removed/private.
        logger.warning("Failed to extract info for %s: %s", text.strip(), e)
        try:
            await status.edit_text(t(lang, "failed"))
        except Exception:
            pass
        return

    if not info["qualities"]:
        try:
            await status.edit_text(t(lang, "failed"))
        except Exception:
            pass
        return

    cid = info_cache.put(info)
    try:
        await status.delete()
    except Exception:
        pass
    await _answer_with_photo(message, info, lang, cid)
