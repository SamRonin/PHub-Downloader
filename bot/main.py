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


def _ph_health_check() -> None:
    """At startup, test PornHub reachability from *this* deployment's IP.

    Railway gives every deployment a (sometimes flagged) egress IP. This check
    makes the verdict visible in the logs immediately, so a failing deploy is
    obviously "the IP is blocked" instead of a confusing per-video error.
    """
    from bot.services.ph import get_proxy

    try:
        import curl_cffi.requests as cr
    except Exception:
        logging.warning("PornHub health check skipped (curl_cffi missing)")
        return

    kwargs = {"impersonate": "chrome", "timeout": 15}
    proxy = get_proxy()
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
        logging.info("PornHub traffic routed via proxy: %s", proxy.split("@")[-1])

    viewkey = "6a430c07e9be0"  # any live video id — only used for the probe
    try:
        with cr.Session(**kwargs) as s:
            r = s.get("https://www.pornhub.com/", headers={"Accept-Language": "en-US,en;q=0.9"})
            homepage = f"homepage HTTP {r.status_code}"
            try:
                v = s.get(
                    f"https://www.pornhub.com/view_video.php?viewkey={viewkey}",
                    headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                ok = v.status_code == 200 and f"viewkey={viewkey}" in str(v.url)
                video = f"video HTTP {v.status_code} final={v.url}"
            except Exception as exc:
                ok = False
                video = f"video error: {type(exc).__name__}: {str(exc)[:120]}"
    except Exception as exc:
        logging.warning(
            "PornHub health check failed (%s) — check network/proxy",
            f"{type(exc).__name__}: {str(exc)[:120]}",
        )
        return

    if ok:
        logging.info("PornHub reachable from this deploy: OK (%s)", homepage)
    else:
        logging.error(
            "PornHub BLOCKED from this deploy (%s; %s). The videos are fine — "
            "this server IP is being bounced. Fix: redeploy (fresh egress IP), "
            "change the Railway region, or set PH_PROXY to a proxy on a clean IP.",
            homepage, video,
        )


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    check_impersonation()
    await asyncio.to_thread(_log_egress_ip)
    await asyncio.to_thread(_ph_health_check)

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
