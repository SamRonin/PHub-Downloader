import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings
from bot.db import db
from bot.handlers import start, info, download, admin
from bot.utils.cleanup import pixeldrain_expiry_loop, temp_cleanup_loop

try:
    from yt_dlp.dependencies import curl_cffi as _yt_curl_cffi
except Exception:  # pragma: no cover - very old yt-dlp
    _yt_curl_cffi = None

try:
    import yt_dlp as _yt_dlp
    _YTDLP_VERSION = getattr(getattr(_yt_dlp, "version", None), "__version__", "?")
except Exception:  # pragma: no cover
    _YTDLP_VERSION = "?"


def check_impersonation() -> None:
    """Pornhub blocks non-browser HTTP clients. yt-dlp's extractor needs
    browser impersonation, which requires the optional 'curl_cffi' package.
    Without it every Pornhub request is redirected/blocked and downloads
    fail with 'Redirection detected' or HTTP 403."""
    logging.info("yt-dlp %s + curl_cffi %s",
                 _YTDLP_VERSION,
                 getattr(_yt_curl_cffi, "__version__", "NOT INSTALLED"))
    if _yt_curl_cffi is None:
        logging.warning(
            "yt-dlp browser impersonation is DISABLED: 'curl_cffi' is not installed. "
            "Pornhub requests will be blocked (HTTP 403 / 'Redirection detected'). "
            "Add 'curl_cffi' to requirements.txt and redeploy."
        )


def _log_egress_ip() -> None:
    """Log the outbound public IP once at startup.

    Railway egress IPs vary between deployments and some are flagged by
    PornHub (which then answers with a redirect). Knowing the exact IP of a
    "working" vs a "failing" deploy makes it obvious whether that is the
    cause — and lets you compare with whatever IP runs main.py.
    """
    import json
    import urllib.request

    for url in ("https://ipinfo.io/json", "https://api.ipify.org?format=json"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            ip = payload.get("ip")
            if ip:
                org = payload.get("org") or payload.get("as") or ""
                logging.info("Egress IP: %s %s (source %s)", ip, org, url.split("/")[2])
                return
        except Exception:
            continue
    logging.warning("Could not determine egress IP")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    check_impersonation()
    await asyncio.to_thread(_log_egress_ip)

    os.makedirs(settings.TEMP_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)

    await db.init()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # order matters: commands first, then link/text, then admin FSM handlers
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(download.router)
    dp.include_router(info.router)

    asyncio.create_task(pixeldrain_expiry_loop())
    asyncio.create_task(temp_cleanup_loop())

    logging.info("PH Downloader bot starting (admins: %s)", settings.ADMIN_IDS)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
