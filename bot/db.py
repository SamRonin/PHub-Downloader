import aiosqlite
import time
from datetime import datetime, timezone

from bot.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    lang TEXT DEFAULT 'fa',
    banned INTEGER DEFAULT 0,
    is_pro INTEGER DEFAULT 0,
    pro_until INTEGER DEFAULT 0,
    referrer_id INTEGER,
    referrals_count INTEGER DEFAULT 0,
    referrals_used INTEGER DEFAULT 0,
    downloads_count INTEGER DEFAULT 0,
    bytes_total INTEGER DEFAULT 0,
    joined_at INTEGER
);
CREATE TABLE IF NOT EXISTS downloads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    quality TEXT,
    size INTEGER,
    delivered TEXT,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS uploads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    file_id TEXT,
    size INTEGER,
    deleted INTEGER DEFAULT 0,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_downloads_user_time ON downloads(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_uploads_pending ON uploads(deleted, created_at);
"""

DEFAULT_SETTINGS = {
    "daily_quota_free": str(settings.DAILY_QUOTA_FREE),
    "daily_quota_pro": str(settings.DAILY_QUOTA_PRO),
    "speed_limit_free": str(settings.SPEED_LIMIT_FREE),
    "px_delete_minutes": str(settings.PX_DELETE_MINUTES),
    "referrals_for_pro": str(settings.REFERRALS_FOR_PRO),
    "pro_days": str(settings.PRO_DAYS),
}


def day_start_ts() -> int:
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(start.timestamp())


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = None

    async def init(self):
        import asyncio
        self._lock = asyncio.Lock()
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            await self._conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def execute(self, sql: str, params=()) -> None:
        async with self._lock:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def fetchone(self, sql: str, params=()):
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params=()):
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def fetchval(self, sql: str, params=()):
        row = await self.fetchone(sql, params)
        return row[0] if row else None

    # ---- users ----
    async def get_user(self, user_id: int):
        return await self.fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))

    async def create_user(self, user_id: int, username: str, full_name: str, lang: str) -> bool:
        """Returns True if the user is brand new."""
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT OR IGNORE INTO users(user_id, username, full_name, lang, joined_at) VALUES(?,?,?,?,?)",
                (user_id, username, full_name, lang, int(time.time())),
            )
            await self._conn.commit()
            return cur.rowcount > 0

    async def update_user_info(self, user_id: int, username: str, full_name: str):
        await self.execute(
            "UPDATE users SET username=?, full_name=? WHERE user_id=?", (username, full_name, user_id)
        )

    async def set_lang(self, user_id: int, lang: str):
        await self.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))

    async def set_banned(self, user_id: int, banned: bool):
        await self.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if banned else 0, user_id))

    async def set_pro(self, user_id: int, pro_until_ts: int):
        await self.execute(
            "UPDATE users SET is_pro=1, pro_until=? WHERE user_id=?", (pro_until_ts, user_id)
        )

    async def remove_pro(self, user_id: int):
        await self.execute("UPDATE users SET is_pro=0, pro_until=0 WHERE user_id=?", (user_id,))

    async def add_referral(self, referrer_id: int):
        await self.execute(
            "UPDATE users SET referrals_count=referrals_count+1 WHERE user_id=?", (referrer_id,)
        )

    async def consume_referrals(self, referrer_id: int, n: int):
        await self.execute(
            "UPDATE users SET referrals_used=referrals_used+? WHERE user_id=?", (n, referrer_id)
        )

    # ---- quota / stats ----
    async def bytes_used_today(self, user_id: int) -> int:
        return int(
            await self.fetchval(
                "SELECT COALESCE(SUM(size),0) FROM downloads WHERE user_id=? AND created_at>=?",
                (user_id, day_start_ts()),
            )
            or 0
        )

    async def record_download(self, user_id: int, quality: str, size: int, delivered: str):
        await self.execute(
            "INSERT INTO downloads(user_id, quality, size, delivered, created_at) VALUES(?,?,?,?,?)",
            (user_id, quality, size, delivered, int(time.time())),
        )
        await self.execute(
            "UPDATE users SET downloads_count=downloads_count+1, bytes_total=bytes_total+? WHERE user_id=?",
            (size, user_id),
        )

    async def reset_user_quota(self, user_id: int):
        await self.execute(
            "DELETE FROM downloads WHERE user_id=? AND created_at>=?", (user_id, day_start_ts())
        )

    # ---- settings ----
    async def get_setting(self, key: str) -> str:
        v = await self.fetchval("SELECT value FROM settings WHERE key=?", (key,))
        if v is None:
            return DEFAULT_SETTINGS.get(key, "")
        return v

    async def set_setting(self, key: str, value: str):
        await self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- uploads (pixeldrain) ----
    async def record_upload(self, user_id: int, file_id: str, size: int):
        await self.execute(
            "INSERT INTO uploads(user_id, file_id, size, deleted, created_at) VALUES(?,?,?,0,?)",
            (user_id, file_id, size, int(time.time())),
        )

    async def mark_upload_deleted(self, file_id: str):
        await self.execute("UPDATE uploads SET deleted=1 WHERE file_id=?", (file_id,))

    async def pending_expired_uploads(self, max_age_sec: int):
        return await self.fetchall(
            "SELECT file_id FROM uploads WHERE deleted=0 AND created_at<=?",
            (int(time.time()) - max_age_sec,),
        )

    async def active_uploads(self, limit: int = 20):
        return await self.fetchall(
            "SELECT * FROM uploads WHERE deleted=0 ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    # ---- admin stats ----
    async def stats(self):
        total_users = await self.fetchval("SELECT COUNT(*) FROM users") or 0
        now = int(time.time())
        pro_users = await self.fetchval(
            "SELECT COUNT(*) FROM users WHERE is_pro=1 AND pro_until>?", (now,)
        ) or 0
        today = day_start_ts()
        dl_today = await self.fetchval("SELECT COUNT(*) FROM downloads WHERE created_at>=?", (today,)) or 0
        bytes_today = (
            await self.fetchval("SELECT COALESCE(SUM(size),0) FROM downloads WHERE created_at>=?", (today,))
            or 0
        )
        bytes_total = await self.fetchval("SELECT COALESCE(SUM(bytes_total),0) FROM users") or 0
        active_uploads = await self.fetchval("SELECT COUNT(*) FROM uploads WHERE deleted=0") or 0
        return {
            "total_users": total_users,
            "pro_users": pro_users,
            "dl_today": dl_today,
            "bytes_today": bytes_today,
            "bytes_total": bytes_total,
            "active_uploads": active_uploads,
        }

    async def all_user_ids(self):
        rows = await self.fetchall("SELECT user_id FROM users WHERE banned=0")
        return [r[0] for r in rows]

    async def users_page(self, offset: int, limit: int):
        return await self.fetchall(
            "SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    async def count_users(self) -> int:
        return await self.fetchval("SELECT COUNT(*) FROM users") or 0


db = Database(settings.DB_PATH)
