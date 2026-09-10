"""PornHub search: keyword -> browsable results -> download.

Flow
----
1. User sends plain keywords (or ``/search <keywords>``).
2. We scrape the PornHub results page through the same impersonated,
   cookie-warmed, host-rotating session that extraction uses (see
   ``bot.services.ph.search_videos``).
3. The results are shown as a numbered list with prev/next buttons; each
   result is a button.
4. Tapping a result runs the normal extraction path and shows the usual
   quality menu (thumbnail card + ``dl:<height>:<cid>`` buttons), so the rest
   of the download flow is completely unchanged.

This router MUST be included before ``bot.handlers.info``: aiogram stops
propagating a message as soon as a handler matched (even when it returns
None), and ``info`` has a catch-all text handler.
"""

import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)

from bot.db import db
from bot.services.ph import PornHubBlockedError, extract_info, summarize, search_videos
from bot.utils.helpers import esc, row_get
from bot.utils.i18n import t
from bot.handlers.cache import search_cache, info_cache
from bot.handlers.cache import detect_lang
from bot.handlers.info import _answer_with_photo

logger = logging.getLogger(__name__)
router = Router(name="search")

#: How many results per page of the Telegram message.
PER_PAGE = 6
#: Longest title we put on one button.
_BTN_TITLE_LEN = 34
#: Titles shown in the message body.
_MSG_TITLE_LEN = 70
_MIN_QUERY_LEN = 3


def _looks_like_query(message: Message) -> bool:
    """Only plain keywords count as a search (never links or commands)."""
    text = (message.text or "").strip()
    if len(text) < _MIN_QUERY_LEN or text.startswith("/"):
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in ("http://", "https://", "www.", "t.me/")):
        return False
    return True


def _page_slice(results: list[dict], page: int) -> tuple[list[dict], int]:
    pages = max(1, (len(results) + PER_PAGE - 1) // PER_PAGE)
    page = min(max(page, 1), pages)
    start = (page - 1) * PER_PAGE
    return results[start : start + PER_PAGE], pages


def _results_text(query: str, results: list[dict], page: int, pages: int, lang: str) -> str:
    lines = [t(lang, "search_header", q=esc(query), page=page, pages=pages), ""]
    offset = (page - 1) * PER_PAGE
    for i, item in enumerate(results, start=offset + 1):
        title = esc(item["title"][:_MSG_TITLE_LEN])
        meta = []
        if item.get("duration"):
            meta.append(f"⏱ {esc(item['duration'])}")
        if item.get("views"):
            meta.append(f"👀 {esc(item['views'])}")
        suffix = f"  <i>{' • '.join(meta)}</i>" if meta else ""
        lines.append(f"<b>{i}.</b> {title}{suffix}")
    lines.append("")
    lines.append(t(lang, "search_hint"))
    return "\n".join(lines)


def _results_keyboard(results: list[dict], cid: str, page: int, pages: int, lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    offset = (page - 1) * PER_PAGE
    for i, item in enumerate(results, start=offset + 1):
        title = item["title"]
        label = title if len(title) <= _BTN_TITLE_LEN else title[: _BTN_TITLE_LEN - 1] + "…"
        rows.append([InlineKeyboardButton(text=f"{i}. {label}", callback_data=f"srv:{cid}:{i - 1}")])

    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(text=t(lang, "search_prev"), callback_data=f"srp:{cid}:{page - 1}")
        )
    if page < pages:
        nav.append(
            InlineKeyboardButton(text=t(lang, "search_next"), callback_data=f"srp:{cid}:{page + 1}")
        )
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_results(message: Message, query: str, results: list[dict], page: int, lang: str) -> None:
    page_items, pages = _page_slice(results, page)
    cid = search_cache.put({"query": query, "results": results})
    await message.answer(
        _results_text(query, page_items, page, pages, lang),
        reply_markup=_results_keyboard(page_items, cid, page, pages, lang),
        disable_web_page_preview=True,
    )


async def _run_search(message: Message, query: str, lang: str) -> None:
    status = await message.answer(t(lang, "searching", q=esc(query)))
    try:
        results = await search_videos(query, 1)
    except PornHubBlockedError as exc:
        logger.error("PornHub search blocked for %r: %s", query, exc)
        try:
            await status.edit_text(t(lang, "ph_blocked"))
        except Exception:
            pass
        return
    except Exception as exc:
        logger.warning("search failed for %r: %s", query, exc)
        try:
            await status.edit_text(t(lang, "failed"))
        except Exception:
            pass
        return

    if not results:
        try:
            await status.edit_text(t(lang, "search_no_results", q=esc(query)))
        except Exception:
            pass
        return

    try:
        await status.delete()
    except Exception:
        pass
    await _send_results(message, query, results, 1, lang)


@router.message(Command("search"))
async def cmd_search(message: Message, bot: Bot, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    lang = row_get(user, "lang") or detect_lang(message.from_user.language_code)
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or len(args[1].strip()) < _MIN_QUERY_LEN:
        await message.answer(t(lang, "search_usage"), disable_web_page_preview=True)
        return
    await _run_search(message, args[1].strip(), lang)


async def _ensure_user(tg_user):
    """Fetch the DB row for a Telegram user, creating it on first contact.

    Without this a brand-new user (who never sent /start) would be treated as
    "unknown" and could be told they are banned.
    """
    user = await db.get_user(tg_user.id)
    if user is not None:
        return user
    lang = detect_lang(getattr(tg_user, "language_code", None))
    await db.create_user(
        tg_user.id, tg_user.username or "", tg_user.full_name or "", lang
    )
    return await db.get_user(tg_user.id)


@router.message(StateFilter(None), F.text, _looks_like_query)
async def handle_query(message: Message, bot: Bot):
    """Bare keywords are treated as a search query."""
    user = await _ensure_user(message.from_user)
    lang = row_get(user, "lang") or detect_lang(message.from_user.language_code)
    if user["banned"]:
        await message.answer(t(lang, "banned"))
        return

    await _run_search(message, (message.text or "").strip(), lang)


@router.callback_query(F.data.startswith("srp:"))
async def cb_page(cq: CallbackQuery):
    try:
        _, cid, page_s = cq.data.split(":")
        page = int(page_s)
    except ValueError:
        await cq.answer()
        return
    user = await db.get_user(cq.from_user.id)
    lang = row_get(user, "lang") or detect_lang(cq.from_user.language_code)
    cached = search_cache.get(cid)
    if not cached:
        await cq.answer(t(lang, "search_expired"), show_alert=True)
        return
    await cq.answer()
    items, pages = _page_slice(cached["results"], page)
    try:
        await cq.message.edit_text(
            _results_text(cached["query"], items, page, pages, lang),
            reply_markup=_results_keyboard(items, cid, page, pages, lang),
            disable_web_page_preview=True,
        )
    except Exception:
        logger.debug("page edit failed", exc_info=True)


@router.callback_query(F.data.startswith("srv:"))
async def cb_pick(cq: CallbackQuery, bot: Bot):
    """A search result was picked: show the normal quality menu for it."""
    try:
        _, cid, idx_s = cq.data.split(":")
        idx = int(idx_s)
    except ValueError:
        await cq.answer()
        return

    user = await _ensure_user(cq.from_user)
    lang = row_get(user, "lang") or detect_lang(cq.from_user.language_code)
    if user["banned"]:
        await cq.answer(t(lang, "banned"), show_alert=True)
        return

    cached = search_cache.get(cid)
    if not cached or not (0 <= idx < len(cached["results"])):
        await cq.answer(t(lang, "search_expired"), show_alert=True)
        return

    item = cached["results"][idx]
    await cq.answer()
    status = await cq.message.answer(t(lang, "fetching"))
    try:
        raw = await extract_info(item["url"])
        info = summarize(raw)
        if not info["qualities"]:
            raise RuntimeError("no qualities")
    except PornHubBlockedError as exc:
        logger.error("PornHub BLOCKED for search pick %s: %s", item["url"], exc)
        try:
            await status.edit_text(t(lang, "ph_blocked"))
        except Exception:
            pass
        return
    except Exception as exc:
        logger.warning("extract failed for search pick %s: %s", item["url"], exc)
        try:
            await status.edit_text(t(lang, "failed"))
        except Exception:
            pass
        return

    try:
        await status.delete()
    except Exception:
        pass
    new_cid = info_cache.put(info)
    await _answer_with_photo(cq.message, info, lang, new_cid)
