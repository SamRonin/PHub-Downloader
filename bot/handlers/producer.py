"""PornHub producer search: name -> profile card -> top-10 downloads.

Flow
----
1. User sends ``/producer <name>`` (alias ``/channel``) or a channel /
   pornstar / model URL.
2. We scrape PornHub's channel + pornstar search pages (same impersonated
   session as video search), then the profile page + the most-viewed
   listing.
3. The profile photo is fetched (CDN hotlink-protects) and sent with a
   caption of name / followers / videos / views / rank.
4. Ten most-viewed videos are buttons; tapping one runs the normal
   extraction path and shows the usual quality menu.

This router MUST be included before ``bot.handlers.info`` so a channel URL
is not swallowed by the video-link catch-all.
"""

import asyncio
import logging
import re

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)

from bot.db import db
from bot.services.ph import (
    PornHubBlockedError,
    ProducerNotFoundError,
    compact_count,
    extract_info,
    fetch_producer,
    format_count,
    parse_producer_url,
    search_producers,
    summarize,
)
from bot.utils.helpers import esc, row_get
from bot.utils.i18n import t
from bot.handlers.cache import producer_cache, info_cache, detect_lang
from bot.handlers.info import _answer_with_photo, _fetch_thumbnail_bytes, _to_jpeg
from bot.handlers.search import _ensure_user

logger = logging.getLogger(__name__)
router = Router(name="producer")

PER_PAGE = 6
_BTN_TITLE_LEN = 34
_MIN_QUERY_LEN = 2

_KIND_I18N = {
    "channel": "producer_kind_channel",
    "pornstar": "producer_kind_pornstar",
    "model": "producer_kind_model",
}


def _kind_label(kind: str, lang: str) -> str:
    return t(lang, _KIND_I18N.get(kind, "producer_kind_channel"))


def _page_slice(results: list[dict], page: int) -> tuple[list[dict], int]:
    pages = max(1, (len(results) + PER_PAGE - 1) // PER_PAGE)
    page = min(max(page, 1), pages)
    start = (page - 1) * PER_PAGE
    return results[start : start + PER_PAGE], pages


def _list_text(query: str, results: list[dict], page: int, pages: int, lang: str) -> str:
    lines = [t(lang, "producer_header", q=esc(query), page=page, pages=pages), ""]
    offset = (page - 1) * PER_PAGE
    for i, item in enumerate(results, start=offset + 1):
        name = esc(item.get("name") or item.get("slug") or "?")
        kind = _kind_label(item.get("kind") or "channel", lang)
        lines.append(f"<b>{i}.</b> {name}  <i>{kind}</i>")
        meta = []
        if item.get("subscribers"):
            meta.append(f"👥 {esc(compact_count(item['subscribers']) or item['subscribers'])}")
        if item.get("video_count"):
            meta.append(f"🎬 {esc(compact_count(item['video_count']) or item['video_count'])}")
        if item.get("views"):
            meta.append(f"👀 {esc(compact_count(item['views']) or item['views'])}")
        if meta:
            lines.append("    <i>" + " • ".join(meta) + "</i>")
    lines.append("")
    lines.append(t(lang, "producer_hint"))
    return "\n".join(lines)


def _list_keyboard(results: list[dict], cid: str, page: int, pages: int, lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    offset = (page - 1) * PER_PAGE
    for i, item in enumerate(results, start=offset + 1):
        title = item.get("name") or item.get("slug") or "?"
        label = title if len(title) <= _BTN_TITLE_LEN else title[: _BTN_TITLE_LEN - 1] + "…"
        rows.append(
            [InlineKeyboardButton(text=f"{i}. {label}", callback_data=f"psi:{cid}:{i - 1}")]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(text=t(lang, "search_prev"), callback_data=f"psp:{cid}:{page - 1}")
        )
    if page < pages:
        nav.append(
            InlineKeyboardButton(text=t(lang, "search_next"), callback_data=f"psp:{cid}:{page + 1}")
        )
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _profile_caption(profile: dict, lang: str) -> str:
    name = esc(profile.get("name") or profile.get("slug") or "?")
    lines = [f"🎭 <b>{name}</b>", _kind_label(profile.get("kind") or "channel", lang), ""]
    if profile.get("subscribers"):
        lines.append(
            t(lang, "producer_followers", n=esc(format_count(profile["subscribers"]) or profile["subscribers"]))
        )
    if profile.get("video_count"):
        lines.append(
            t(lang, "producer_videos_count", n=esc(format_count(profile["video_count"]) or profile["video_count"]))
        )
    if profile.get("views"):
        lines.append(
            t(lang, "producer_views_count", n=esc(format_count(profile["views"]) or profile["views"]))
        )
    if profile.get("rank"):
        lines.append(t(lang, "producer_rank", n=esc(str(profile["rank"]))))
    if profile.get("joined"):
        lines.append(t(lang, "producer_joined", n=esc(profile["joined"])))
    bio = re.sub(r"\s+", " ", (profile.get("bio") or "")).strip()
    if bio:
        if len(bio) > 180:
            bio = bio[:180] + "…"
        lines.append("")
        lines.append(f"📝 {esc(bio)}")
    lines.append("")
    if profile.get("videos"):
        lines.append(t(lang, "producer_top_hint"))
    else:
        lines.append(t(lang, "producer_no_videos"))
    return "\n".join(lines)


def _profile_keyboard(profile: dict, cid: str) -> InlineKeyboardMarkup | None:
    videos = profile.get("videos") or []
    if not videos:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for i, item in enumerate(videos):
        views = item.get("views") or ""
        prefix = f"{i + 1}. "
        suffix = f" · {views}" if views else ""
        budget = 64 - len(prefix) - len(suffix)
        title = item.get("title") or "?"
        if budget < 2:
            label = (prefix + title)[:64]
        else:
            if len(title) > budget:
                title = title[: max(budget - 1, 1)] + "…"
            label = prefix + title + suffix
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"pvv:{cid}:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_profile(message: Message, profile: dict, lang: str) -> None:
    cid = producer_cache.put(profile)
    caption = _profile_caption(profile, lang)[:1024]
    kb = _profile_keyboard(profile, cid)
    avatar = profile.get("avatar")
    if avatar:
        try:
            data = await _fetch_thumbnail_bytes(avatar)
            jpeg = await asyncio.to_thread(_to_jpeg, data)
            await message.answer_photo(
                BufferedInputFile(jpeg, filename="avatar.jpg"),
                caption=caption,
                reply_markup=kb,
            )
            return
        except Exception:
            logger.warning("producer avatar fetch failed", exc_info=True)
        try:
            await message.answer_photo(photo=avatar, caption=caption, reply_markup=kb)
            return
        except Exception:
            logger.warning("producer avatar URL send failed", exc_info=True)
    await message.answer(
        _profile_caption(profile, lang)[:4096],
        reply_markup=kb,
        disable_web_page_preview=True,
    )


def _exact_match(results: list[dict], query: str) -> dict | None:
    q = (query or "").lower().strip()
    if not q:
        return None
    for item in results:
        if (item.get("name") or "").lower() == q:
            return item
    return None


async def _open_profile(
    message: Message,
    kind: str,
    slug: str,
    lang: str,
    status: Message | None,
    extra: dict | None = None,
) -> None:
    try:
        profile = await fetch_producer(kind, slug)
    except ProducerNotFoundError:
        if status is not None:
            try:
                await status.edit_text(t(lang, "producer_no_results", q=esc(slug)))
            except Exception:
                pass
        else:
            await message.answer(t(lang, "producer_no_results", q=esc(slug)))
        return
    except PornHubBlockedError as exc:
        logger.error("PornHub blocked producer %s/%s: %s", kind, slug, exc)
        text = t(lang, "ph_blocked")
        if status is not None:
            try:
                await status.edit_text(text)
            except Exception:
                pass
        else:
            await message.answer(text)
        return
    except Exception as exc:
        logger.warning("producer fetch failed %s/%s: %s", kind, slug, exc)
        text = t(lang, "failed")
        if status is not None:
            try:
                await status.edit_text(text)
            except Exception:
                pass
        else:
            await message.answer(text)
        return

    if extra:
        # Search cards often have a more accurate video_count than the profile
        # page; fill any hole the profile parser left.
        for key in ("subscribers", "video_count", "views", "rank", "avatar"):
            if not profile.get(key) and extra.get(key):
                profile[key] = extra[key]
    if status is not None:
        try:
            await status.delete()
        except Exception:
            pass
    await _send_profile(message, profile, lang)


async def _run_producer_search(message: Message, query: str, lang: str) -> None:
    status = await message.answer(t(lang, "producer_searching", q=esc(query)))
    try:
        results = await search_producers(query)
    except PornHubBlockedError as exc:
        logger.error("PornHub producer search blocked for %r: %s", query, exc)
        try:
            await status.edit_text(t(lang, "ph_blocked"))
        except Exception:
            pass
        return
    except Exception as exc:
        logger.warning("producer search failed for %r: %s", query, exc)
        try:
            await status.edit_text(t(lang, "failed"))
        except Exception:
            pass
        return

    if not results:
        try:
            await status.edit_text(t(lang, "producer_no_results", q=esc(query)))
        except Exception:
            pass
        return

    hit = results[0] if len(results) == 1 else _exact_match(results, query)
    if hit:
        try:
            await status.edit_text(t(lang, "producer_fetching"))
        except Exception:
            pass
        await _open_profile(message, hit["kind"], hit["slug"], lang, status)
        return

    try:
        await status.delete()
    except Exception:
        pass
    page_items, pages = _page_slice(results, 1)
    cid = producer_cache.put({"type": "search", "query": query, "results": results})
    await message.answer(
        _list_text(query, page_items, 1, pages, lang),
        reply_markup=_list_keyboard(page_items, cid, 1, pages, lang),
        disable_web_page_preview=True,
    )


def _looks_like_producer_url(message: Message) -> bool:
    return parse_producer_url((message.text or "").strip()) is not None


@router.message(Command("producer", "channel"))
async def cmd_producer(message: Message, bot: Bot):
    user = await _ensure_user(message.from_user)
    lang = row_get(user, "lang") or detect_lang(message.from_user.language_code)
    if user["banned"]:
        await message.answer(t(lang, "banned"))
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or len(args[1].strip()) < _MIN_QUERY_LEN:
        await message.answer(t(lang, "producer_usage"), disable_web_page_preview=True)
        return
    await _run_producer_search(message, args[1].strip(), lang)


@router.message(F.text, _looks_like_producer_url)
async def handle_producer_url(message: Message):
    parsed = parse_producer_url((message.text or "").strip())
    if not parsed:
        return
    kind, slug = parsed
    user = await _ensure_user(message.from_user)
    lang = row_get(user, "lang") or detect_lang(message.from_user.language_code)
    if user["banned"]:
        await message.answer(t(lang, "banned"))
        return
    status = await message.answer(t(lang, "producer_fetching"))
    await _open_profile(message, kind, slug, lang, status)


@router.callback_query(F.data.startswith("psp:"))
async def cb_page(cq: CallbackQuery):
    try:
        _, cid, page_s = cq.data.split(":")
        page = int(page_s)
    except ValueError:
        await cq.answer()
        return
    user = await db.get_user(cq.from_user.id)
    lang = row_get(user, "lang") or detect_lang(cq.from_user.language_code)
    cached = producer_cache.get(cid)
    if not cached or cached.get("type") != "search":
        await cq.answer(t(lang, "producer_expired"), show_alert=True)
        return
    await cq.answer()
    items, pages = _page_slice(cached["results"], page)
    try:
        await cq.message.edit_text(
            _list_text(cached["query"], items, page, pages, lang),
            reply_markup=_list_keyboard(items, cid, page, pages, lang),
            disable_web_page_preview=True,
        )
    except Exception:
        logger.debug("producer page edit failed", exc_info=True)


@router.callback_query(F.data.startswith("psi:"))
async def cb_pick_producer(cq: CallbackQuery):
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
    cached = producer_cache.get(cid)
    if not cached or cached.get("type") != "search" or not (0 <= idx < len(cached["results"])):
        await cq.answer(t(lang, "producer_expired"), show_alert=True)
        return
    item = cached["results"][idx]
    await cq.answer()
    status = await cq.message.answer(t(lang, "producer_fetching"))
    await _open_profile(cq.message, item["kind"], item["slug"], lang, status, extra=item)


@router.callback_query(F.data.startswith("pvv:"))
async def cb_pick_video(cq: CallbackQuery, bot: Bot):
    """A top-N video was picked: show the normal quality menu for it."""
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
    cached = producer_cache.get(cid)
    videos = (cached or {}).get("videos") or []
    if not cached or not (0 <= idx < len(videos)):
        await cq.answer(t(lang, "producer_expired"), show_alert=True)
        return
    item = videos[idx]
    await cq.answer()
    status = await cq.message.answer(t(lang, "fetching"))
    try:
        raw = await extract_info(item["url"])
        info = summarize(raw)
        if not info["qualities"]:
            raise RuntimeError("no qualities")
    except PornHubBlockedError as exc:
        logger.error("PornHub BLOCKED for producer video %s: %s", item["url"], exc)
        try:
            await status.edit_text(t(lang, "ph_blocked"))
        except Exception:
            pass
        return
    except Exception as exc:
        logger.warning("extract failed for producer video %s: %s", item["url"], exc)
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
