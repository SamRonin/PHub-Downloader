from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from bot.services.ph import extract_info, summarize
from bot.utils.helpers import esc, is_phub_url
from bot.utils.i18n import t
from bot.handlers.cache import info_cache, detect_lang

router = Router(name="info")


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


@router.message(F.text)
async def handle_link(message: Message):
    text = message.text or ""
    if not is_phub_url(text):
        return
    lang = detect_lang(message.from_user.language_code)
    status = await message.answer(t(lang, "fetching"))
    try:
        raw = await extract_info(text.strip())
        info = summarize(raw)
    except Exception:
        try:
            await status.edit_text(t(lang, "invalid_link"))
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
    try:
        await message.answer_photo(
            photo=info.get("thumbnail"),
            caption=build_caption(info, lang)[:1024],
            reply_markup=build_keyboard(info, lang, cid),
        )
    except Exception:
        # thumbnail fetch failed -> send as text
        await message.answer(
            build_caption(info, lang)[:4096],
            reply_markup=build_keyboard(info, lang, cid),
            disable_web_page_preview=True,
        )
