import asyncio
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp

from bot.services.ph import (
    PornHubBlockedError,
    _apex_of,
    _warm_cookies_for_apex,
    apply_browser_opts,
    attempt_targets,
    canonical_view_url,
    get_proxy,
    is_bounce_error,
)

logger = logging.getLogger(__name__)

# Downloads can take minutes; each *network call* inside yt-dlp must never hang
# forever (that is exactly the "Downloading... 0% forever" symptom: a request
# to the CDN that never answers, with no socket timeout configured).
_DOWNLOAD_SOCKET_TIMEOUT = 30

# NOTE on speed (no format_sort here — deliberately):
# We do NOT set yt-dlp's ``format_sort``. PornHub serves the same quality as a
# single progressive ``https`` mp4 AND as an HLS (``m3u8_native``) stream. The
# progressive file comes off a single throttled connection (~0.5-0.6 MB/s),
# while the HLS stream is downloaded as parallel fragments
# (``concurrent_fragment_downloads``) at the full CDN rate (~20+ MB/s; a
# 300 MB file in ~20 s). yt-dlp's *default* sort picks the HLS stream for the
# chosen height, so overriding ``format_sort`` to prefer ``proto:https`` (as
# an earlier revision did) is exactly what made downloads crawl. We therefore
# leave the default in place — same choice as the stable MZ-Downloader.
_DOWNLOAD_FRAGMENT_WORKERS = 4

# A download attempt that fails its network calls (bounce, timeout, CDN error)
# can retry on another host/page, but we cap the total so a flagged IP can't
# keep the user waiting forever.
_MAX_DOWNLOAD_ATTEMPTS = 6

# yt-dlp "download" hook dictionary fields
_STATUS_FINISHED = "finished"
_STATUS_DOWNLOADING = "downloading"


def _is_fatal_error(exc: Exception) -> bool:
    """Errors that retrying on another host/page will NOT fix (the video is
    really gone / private / region-locked / etc.). Everything else — bounces,
    network timeouts, CDN errors — is worth retrying."""
    text = str(exc)
    fatal = (
        "has been removed",
        "removed by",
        "video is private",
        "This video is private",
        "is locked",
        "geo-restricted",
        "unavailable in your country",
        "This content is unavailable in your country",
        "PornHub said",
    )
    return any(m in text for m in fatal)


async def download_video(
    url: str,
    format_spec: str,
    out_dir: str,
    rate_limit: int | None = None,
) -> tuple[Path, dict]:
    """Download a video, returns (file_path, progress_state).

    progress_state = {"phase": "download", "pct": 0-100}
    """
    state = {"phase": "download", "pct": 0}
    out_dir_p = Path(out_dir)
    last_logged_pct = -1

    def hook(d):
        nonlocal last_logged_pct
        status = d.get("status")
        if status == _STATUS_FINISHED:
            state["pct"] = 100
            logger.info("Download finished: %s", d.get("filename"))
            return
        if status != _STATUS_DOWNLOADING:
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        if not total:
            return
        pct = min(int(done * 100 / total), 99)
        state["pct"] = pct
        # Log progress every ~10% so the Railway log shows the download is
        # actually moving (yt-dlp itself is configured quiet).
        if pct >= last_logged_pct + 10 or (pct == 0 and last_logged_pct == -1):
            last_logged_pct = pct
            logger.info("Downloading... %d%% (%s of %s)",
                        pct, _human(done), _human(total))

    def _build_opts(cookies_file: str | None) -> dict:
        opts = {
            "format": format_spec,
            "outtmpl": str(out_dir_p / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 2,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": _DOWNLOAD_FRAGMENT_WORKERS,
            # 1 MiB I/O buffer — far fewer syscalls on large files than the
            # tiny default, same as MZ-Downloader.
            "buffersize": 1024 * 1024,
            "socket_timeout": _DOWNLOAD_SOCKET_TIMEOUT,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "progress_hooks": [hook],
        }
        apply_browser_opts(opts)
        if cookies_file:
            opts["cookiefile"] = cookies_file
        proxy = get_proxy()
        if proxy:
            opts["proxy"] = proxy
        if rate_limit:
            opts["ratelimit"] = rate_limit
        return opts

    def _run() -> str:
        canon = canonical_view_url(url)
        targets = attempt_targets(canon)
        apex = _apex_of(urlsplit(canon).hostname or "")
        cookies_file = _warm_cookies_for_apex(apex)
        last_error: Exception | None = None

        for index, target in enumerate(targets[:_MAX_DOWNLOAD_ATTEMPTS]):
            target_apex = _apex_of(urlsplit(target).hostname or "")
            if target_apex != apex:
                cookies_file = _warm_cookies_for_apex(target_apex) or cookies_file

            logger.info("Download attempt %d/%d: %s", index + 1, _MAX_DOWNLOAD_ATTEMPTS, target)
            started = time.monotonic()
            try:
                opts = _build_opts(cookies_file)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target, download=True)
                    filename = ydl.prepare_filename(info)
                logger.info(
                    "Download OK from %s in %.0fs", target, time.monotonic() - started,
                )
                return filename
            except Exception as exc:
                last_error = exc
                elapsed = time.monotonic() - started
                if _is_fatal_error(exc):
                    logger.warning("Fatal download error from %s: %s", target, exc)
                    raise
                logger.warning(
                    "Download attempt %d/%d failed after %.0fs (%s): %s",
                    index + 1, _MAX_DOWNLOAD_ATTEMPTS, elapsed, target, exc,
                )
                if index + 1 < _MAX_DOWNLOAD_ATTEMPTS:
                    time.sleep(1.0)

        assert last_error is not None
        if is_bounce_error(last_error):
            raise PornHubBlockedError(
                "PornHub bounced every download route for this server IP. "
                "See the startup logs / set PH_PROXY."
            ) from last_error
        raise last_error

    filename = await asyncio.to_thread(_run)
    path = Path(filename)
    if not path.exists():
        # merged output may have a different extension
        candidates = list(out_dir_p.glob(f"{path.stem}.*"))
        if not candidates:
            raise FileNotFoundError("Downloaded file not found")
        path = candidates[0]
    return path, state


def _human(num: int) -> str:
    try:
        num = int(num or 0)
    except (TypeError, ValueError):
        return str(num)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"
