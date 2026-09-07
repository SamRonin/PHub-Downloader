import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from ..config import settings
from ..db import db
from ..utils.helpers import esc, fmt_size
from ..utils.i18n import t
from ..utils.cleanup import delete_pixeldrain_file
from .start import status_line

logger = logging.getLogger(__name__)
router = Router(name="admin")

USERS_PER_PAGE = 10

SETTING_KEYS = [
    ("daily_quota_free", "Daily quota free (bytes)"),
    ("daily_quota_pro", "Daily quota pro (bytes)"),
    ("speed_limit_free", "Free speed limit (bytes/sec, 0=off)"),
    ("px_delete_minutes", "Pixeldrain delete (minutes)"),
    ("referrals_for_pro", "Referrals for Pro"),
    ("pro_days", "Pro days"),
]


class AdminStates(StatesGroup):
    broadcast = State()
    setting_value = State()


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


def admin_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_stats"), callback_data="adm:stats"),
                InlineKeyboardButton(text=t(lang, "btn_users"), callback_data="adm:users:0"),
            ],
            [
                InlineKeyboardButton(text=t(lang, "btn_broadcast"), callback_data="adm:bcast"),
                InlineKeyboardButton(text=t(lang, "btn_settings"), callback_data="adm:settings"),
            ],
            [InlineKeyboardButton(text=t(lang, "btn_files"), callback_data="adm:files")],
        ]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    lang = "fa"
    s = await db.stats()
    await message.answer(
        t(
            lang,
            "admin_panel",
            users=s["total_users"],
            pro=s["pro_users"],
            dls=s["dl_today"],
            files=s["active_uploads"],
        ),
        reply_markup=admin_kb(lang),
    )


async def show_panel(cq: CallbackQuery, lang: str):
    s = await db.stats()
    try:
        await cq.message.edit_text(
            t(
                lang,
                "admin_panel",
                users=s["total_users"],
                pro=s["pro_users"],
                dls=s["dl_today"],
                files=s["active_uploads"],
            ),
            reply_markup=admin_kb(lang),
        )
    except Exception:
        pass


@router.callback_query(F.data == "adm:panel")
async def cb_panel(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    await show_panel(cq, "fa")


@router.callback_query(F.data == "adm:stats")
async def cb_stats(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    s = await db.stats()
    text = t(
        "fa",
        "stats_full",
        total_users=s["total_users"],
        pro_users=s["pro_users"],
        dl_today=s["dl_today"],
        bytes_today=fmt_size(s["bytes_today"]) or "0 B",
        bytes_total=fmt_size(s["bytes_total"]) or "0 B",
        active_uploads=s["active_uploads"],
    )
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("fa", "btn_back"), callback_data="adm:panel")]]
    ))


@router.callback_query(F.data.startswith("adm:users:"))
async def cb_users(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    page = max(0, int(cq.data.split(":")[2]))
    total = await db.count_users()
    pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = min(page, pages - 1)
    rows = await db.users_page(page * USERS_PER_PAGE, USERS_PER_PAGE)
    kb = []
    for r in rows:
        name = (r["full_name"] or r["username"] or str(r["user_id"]))[:30]
        kb.append([InlineKeyboardButton(text=name, callback_data=f"adm:user:{r['user_id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:users:{page-1}"))
    nav.append(InlineKeyboardButton(text=t("fa", "btn_back"), callback_data="adm:panel"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:users:{page+1}"))
    kb.append(nav)
    await cq.message.edit_text(
        t("fa", "user_page", page=page + 1, pages=pages),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


def user_card_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("fa", "btn_ban"), callback_data=f"adm:act:ban:{uid}"),
                InlineKeyboardButton(text=t("fa", "btn_unban"), callback_data=f"adm:act:unban:{uid}"),
            ],
            [
                InlineKeyboardButton(text=t("fa", "btn_give_pro"), callback_data=f"adm:act:givepro:{uid}"),
                InlineKeyboardButton(text=t("fa", "btn_remove_pro"), callback_data=f"adm:act:rmpro:{uid}"),
            ],
            [
                InlineKeyboardButton(text=t("fa", "btn_reset_quota"), callback_data=f"adm:act:resetq:{uid}"),
            ],
            [InlineKeyboardButton(text=t("fa", "btn_back"), callback_data="adm:users:0")],
        ]
    )


async def render_user_card(uid: int) -> str:
    u = await db.get_user(uid)
    if not u:
        return "⚠️ user not found"
    used_today = await db.bytes_used_today(uid)
    import time as _t

    is_pro = bool(u["is_pro"] and u["pro_until"] > int(_t.time()))
    status = t("fa", "pro_active", date=datetime.fromtimestamp(u["pro_until"], tz=timezone.utc).strftime("%Y-%m-%d")) if is_pro else t("fa", "free_user")
    available = u["referrals_count"] - u["referrals_used"]
    return t(
        "fa",
        "user_card",
        name=esc(u["full_name"] or "-"),
        uid=uid,
        username="@" + u["username"] if u["username"] else "-",
        status=status,
        used_today=fmt_size(used_today) or "0 B",
        bytes_total=fmt_size(u["bytes_total"]) or "0 B",
        refs=u["referrals_count"],
        available=available,
    )


@router.callback_query(F.data.startswith("adm:user:"))
async def cb_user_card(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    uid = int(cq.data.split(":")[2])
    await cq.message.edit_text(await render_user_card(uid), reply_markup=user_card_kb(uid))


@router.callback_query(F.data.startswith("adm:act:"))
async def cb_user_action(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    _, _, action, uid_s = cq.data.split(":")
    uid = int(uid_s)
    if action == "ban":
        await db.set_banned(uid, True)
    elif action == "unban":
        await db.set_banned(uid, False)
    elif action == "givepro":
        u = await db.get_user(uid)
        now = int(time.time())
        base = max(u["pro_until"] or 0, now) if u else now
        await db.set_pro(uid, base + 30 * 86400)
    elif action == "rmpro":
        await db.remove_pro(uid)
    elif action == "resetq":
        await db.reset_user_quota(uid)
    await cq.answer(t("fa", "done"))
    try:
        await cq.message.edit_text(await render_user_card(uid), reply_markup=user_card_kb(uid))
    except Exception:
        pass


@router.callback_query(F.data == "adm:bcast")
async def cb_broadcast(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    await state.set_state(AdminStates.broadcast)
    await cq.message.edit_text(t("fa", "broadcast_prompt"))


@router.message(AdminStates.broadcast)
async def do_broadcast(message: Message, bot: Bot, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    if message.text and message.text.startswith("/cancel"):
        await message.answer(t("fa", "broadcast_cancelled"))
        return

    ids = await db.all_user_ids()
    sent = failed = 0
    status = await message.answer(t("fa", "broadcast_prompt").split("\n")[0] + f" 0/{len(ids)}")
    for i, uid in enumerate(ids):
        try:
            await message.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
        if (i + 1) % 25 == 0:
            try:
                await status.edit_text(f"📢 {i+1}/{len(ids)}")
            except Exception:
                pass
            await asyncio.sleep(1)
    await status.edit_text(t("fa", "broadcast_done", sent=sent, failed=failed))


@router.callback_query(F.data == "adm:settings")
async def cb_settings(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    kb = []
    for key, label in SETTING_KEYS:
        val = await db.get_setting(key)
        kb.append([InlineKeyboardButton(text=f"{label}: {val}", callback_data=f"adm:set:{key}")])
    kb.append([InlineKeyboardButton(text=t("fa", "btn_back"), callback_data="adm:panel")])
    await cq.message.edit_text(
        t("fa", "settings_header"), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )


@router.callback_query(F.data.startswith("adm:set:"))
async def cb_setting_edit(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return
    key = cq.data.split(":")[2]
    await state.set_state(AdminStates.setting_value)
    await state.update_data(key=key)
    await cq.message.edit_text(t("fa", "settings_hint") + f"\n\n<code>{key}</code>")


@router.message(AdminStates.setting_value)
async def save_setting(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer(t("fa", "broadcast_cancelled"))
        return
    data = await state.get_data()
    key = data.get("key")
    try:
        value = str(int(message.text.strip()))
    except (ValueError, AttributeError):
        await message.answer(t("fa", "settings_hint"))
        return
    await state.clear()
    await db.set_setting(key, value)
    await message.answer(t("fa", "settings_saved", key=key, value=value))


@router.callback_query(F.data == "adm:files")
async def cb_files(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    rows = await db.active_uploads(15)
    if not rows:
        await cq.message.edit_text(t("fa", "no_files"), reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t("fa", "btn_back"), callback_data="adm:panel")]]
        ))
        return
    kb = []
    for r in rows:
        created = datetime.fromtimestamp(r["created_at"], tz=timezone.utc).strftime("%H:%M")
        kb.append([
            InlineKeyboardButton(
                text=f"{r['file_id'][:10]}… | {fmt_size(r['size']) or '?'} | {created}",
                url=f"https://pixeldrain.com/u/{r['file_id']}",
            ),
            InlineKeyboardButton(text=t("fa", "btn_delete"), callback_data=f"adm:delfile:{r['file_id']}"),
        ])
    kb.append([InlineKeyboardButton(text=t("fa", "btn_back"), callback_data="adm:panel")])
    await cq.message.edit_text(
        t("fa", "files_list"), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )


@router.callback_query(F.data.startswith("adm:delfile:"))
async def cb_del_file(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    file_id = cq.data.split(":", 2)[2]
    ok = await delete_pixeldrain_file(file_id)
    await cq.answer(t("fa", "file_deleted") if ok else "⚠️ delete failed, will retry", show_alert=True)
    await cb_files(cq)
