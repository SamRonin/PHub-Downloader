from datetime import datetime, timezone

from bot.db import db
from bot.utils.helpers import fmt_size


async def get_quotas() -> tuple[int, int]:
    free = int(await db.get_setting("daily_quota_free"))
    pro = int(await db.get_setting("daily_quota_pro"))
    return free, pro


async def remaining_bytes(user_row) -> int:
    free_q, pro_q = await get_quotas()
    is_pro = user_row["is_pro"] and user_row["pro_until"] > int(__import__("time").time())
    quota = pro_q if is_pro else free_q
    used = await db.bytes_used_today(user_row["user_id"])
    return quota - used


async def quota_summary(user_row) -> dict:
    import time

    free_q, pro_q = await get_quotas()
    is_pro = bool(user_row["is_pro"] and user_row["pro_until"] > int(time.time()))
    quota = pro_q if is_pro else free_q
    used = await db.bytes_used_today(user_row["user_id"])
    return {"is_pro": is_pro, "quota": quota, "used": used, "remaining": quota - used}


async def check_quota_for_size(user_row, est_size: int | None) -> tuple[bool, dict]:
    summary = await quota_summary(user_row)
    if est_size and est_size > summary["remaining"]:
        return False, summary
    return True, summary


async def get_free_speed_limit() -> int | None:
    val = int(await db.get_setting("speed_limit_free"))
    return val if val > 0 else None


def fmt_quota(n: int) -> str:
    return fmt_size(n) or "0 B"
