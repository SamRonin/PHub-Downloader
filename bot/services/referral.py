import time
from datetime import datetime, timedelta, timezone

from bot.db import db
from bot.utils.i18n import t


def _fmt_date(ts: int, lang: str) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


async def register_referral(referrer_id: int, new_user_id: int, bot) -> None:
    """Called after a brand-new user registers via a referral link."""
    referrer = await db.get_user(referrer_id)
    if not referrer or referrer["banned"]:
        return
    await db.add_referral(referrer_id)

    need = int(await db.get_setting("referrals_for_pro"))
    available = referrer["referrals_count"] + 1 - referrer["referrals_used"]
    lang = referrer["lang"] or "fa"

    if available >= need:
        days = int(await db.get_setting("pro_days"))
        now = int(time.time())
        current_until = referrer["pro_until"] or 0
        base = max(current_until, now)
        new_until = base + days * 86400
        await db.set_pro(referrer_id, new_until)
        await db.consume_referrals(referrer_id, need)
        try:
            await bot.send_message(
                referrer_id,
                t(lang, "pro_granted", date=_fmt_date(new_until, lang)),
                disable_web_page_preview=True,
            )
        except Exception:
            pass
    else:
        try:
            await bot.send_message(
                referrer_id,
                t(lang, "ref_ok", refs=need - available),
                disable_web_page_preview=True,
            )
        except Exception:
            pass
