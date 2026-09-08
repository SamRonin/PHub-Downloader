import os
import sys
import tempfile


def _parse_admin_ids(raw: str):
    return [int(x) for x in raw.replace(" ", "").split(",") if x.strip()]


def _pick_path(env_key: str, filename: str) -> str:
    custom = os.getenv(env_key)
    if custom:
        return custom
    if os.path.isdir("/data"):
        return f"/data/{filename}"
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", filename)


class Settings:
    def __init__(self):
        self.BOT_TOKEN = os.getenv("BOT_TOKEN", "")
        self.PIXELDRAIN_API_KEY = os.getenv("PIXELDRAIN_API_KEY", "")
        self.ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))

        if not self.BOT_TOKEN:
            print("ERROR: BOT_TOKEN environment variable is missing.", file=sys.stderr)
            raise SystemExit(1)

        self.DB_PATH = _pick_path("DB_PATH", "phdownloader.db")
        # Downloads NEVER go on the mounted volume. TEMP_DIR is scratch space
        # (video files can be >1 GB) and must live on Railway's own ephemeral
        # disk — the /data volume is reserved for the database and user data
        # only. Default: a dedicated dir in the container's temp area; the
        # TEMP_DIR env var still overrides it if you want scratch elsewhere.
        self.TEMP_DIR = os.getenv("TEMP_DIR") or os.path.join(
            tempfile.gettempdir(), "phub_dl"
        )

        self.DAILY_QUOTA_FREE = int(float(os.getenv("DAILY_QUOTA_FREE_MB", "500"))) * 1024 * 1024
        self.DAILY_QUOTA_PRO = int(float(os.getenv("DAILY_QUOTA_PRO_MB", "2048"))) * 1024 * 1024
        self.SPEED_LIMIT_FREE = int(float(os.getenv("SPEED_LIMIT_FREE_MBPS", "2")) * 1024 * 1024)  # bytes/sec
        self.TELEGRAM_LIMIT = int(float(os.getenv("TELEGRAM_LIMIT_MB", "50"))) * 1024 * 1024
        self.PX_DELETE_MINUTES = int(os.getenv("PX_DELETE_MINUTES", "30"))
        self.REFERRALS_FOR_PRO = int(os.getenv("REFERRALS_FOR_PRO", "2"))
        self.PRO_DAYS = int(os.getenv("PRO_DAYS", "30"))
        self.MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
        self.MAX_DESCRIPTION_LEN = 300


settings = Settings()
