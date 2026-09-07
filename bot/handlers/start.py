from aiogram import Router, Bot
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.db import db
from bot.services.referral import register_referral
from bot.services.quota import quota_summary, fmt_quota
from bot.utils.helpers import esc
from bot.utils.i18n import t
from bot.handlers.cache import detect_lang

router = Router(name="start")


def status_line(user_row, lang: str) -> str:
    import time

    if user_row["banned"]:
        return t(lang, "banned")
    if user_row["is_pro"] and user_row["pro_until"] > int(time.time()):
        from datetime import datetime, timezone

        date = datetime.fromtimestamp(user_row["pro_until"], tz=timezone.utc).strftime("%Y-%m-%d")
        return t(lang, "pro_active", date=date)
    return t(lang, "free_user")


def ref_bot_link(username: str, user_id: int) -> str:
    return f"https://t.me/{username}?start=ref_{user_id}"


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    tg_user = message.from_user
    lang = detect_lang(tg_user.language_code)

    # referral payload
    referrer_id = None
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1][4:])
        except ValueError:
            referrer_id = None

    user = await db.get_user(tg_user.id)
    is_new = user is None
    if is_new:
        lang = detect_lang(tg_user.language_code)
        await db.create_user(tg_user.id, tg_user.username or "", tg_user.full_name or "", lang)
        user = await db.get_user(tg_user.id)

    if referrer_id and is_new and referrer_id != tg_user.id:
        await register_referral(referrer_id, tg_user.id, bot)

    # best referral link uses the bot's username
    try:
        me = await bot.me()
        link = ref_bot_link(me.username, tg_user.id)
    except Exception:
        link = ref_link(tg_user.id)

    summary = await quota_summary(user)
    await message.answer(
        t(
            lang,
            "start",
            name=esc(tg_user.first_name or "User"),
            quota=fmt_quota(summary["quota"]),
            status=status_line(user, lang),
            ref_needed=int(await db.get_setting("referrals_for_pro")),
            pro_days=int(await db.get_setting("pro_days")),
            ref_link=link,
        ),
        disable_web_page_preview=True,
    )


@router.message(Command("lang"))
async def cmd_lang(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        lang = detect_lang(message.from_user.language_code)
        await db.create_user(
            message.from_user.id, message.from_user.username or "", message.from_user.full_name or "", lang
        )
        new_lang = lang
    else:
        new_lang = "en" if user["lang"] == "fa" else "fa"
    await db.set_lang(message.from_user.id, new_lang)
    await message.answer(t(new_lang, "lang_set"))


@router.message(Command("help"))
async def cmd_help(message: Message):
    lang = detect_lang(message.from_user.language_code)
    await message.answer(t(lang, "help"), disable_web_page_preview=True)


@router.message(Command("status"))
async def cmd_status(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        await cmd_start(message, message.bot)
        return
    lang = user["lang"] or "fa"
    summary = await quota_summary(user)
    await message.answer(
        t(
            lang,
            "status",
            status=status_line(user, lang),
            used=fmt_quota(summary["used"]),
            quota=fmt_quota(summary["quota"]),
            refs=user["referrals_count"],
            dls=user["downloads_count"],
        )
    )
