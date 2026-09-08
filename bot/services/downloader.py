import asyncio
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp

from bot.services.ph import (
    MAX_ATTEMPTS,
    PornHubBlockedError,
    _apex_of,
    _warm_cookies_for_apex,
    attempt_targets,
    canonical_view_url,
    get_proxy,
    is_bounce_error,
)

logger = logging.getLogger(__name__)


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

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                state["pct"] = min(int(done * 100 / total), 99)

    def _run() -> str:
        # Walk the same host/page matrix as ph.extract_info: the yt-dlp call
        # itself re-extracts the video, which can also be bounced, so we try
        # view_video.php then /embed/ on www/apex of each TLD, with warm
        # cookies and optional proxy.
        canon = canonical_view_url(url)
        targets = attempt_targets(canon)
        cookies_file = _warm_cookies_for_apex(_apex_of(urlsplit(canon).hostname or ""))
        last_error: Exception | None = None

        for index, target in enumerate(targets[:MAX_ATTEMPTS]):
            target_apex = _apex_of(urlsplit(target).hostname or "")
            if target_apex != _apex_of(urlsplit(canon).hostname or ""):
                cookies_file = _warm_cookies_for_apex(target_apex) or cookies_file

            opts = {
                "format": format_spec,
                "outtmpl": str(out_dir_p / "%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "retries": 3,
                "fragment_retries": 3,
                "concurrent_fragment_downloads": 4,
                "merge_output_format": "mp4",
                "noplaylist": True,
                "progress_hooks": [hook],
            }
            if cookies_file:
                opts["cookiefile"] = cookies_file
            proxy = get_proxy()
            if proxy:
                opts["proxy"] = proxy
            if rate_limit:
                opts["ratelimit"] = rate_limit

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target, download=True)
                    return ydl.prepare_filename(info)
            except Exception as exc:
                last_error = exc
                if not is_bounce_error(exc):
                    raise  # real failure (removed / no format / …)
                logger.warning(
                    "PornHub download attempt %d/%d bounced (%s)",
                    index + 1, min(MAX_ATTEMPTS, len(targets)), target,
                )
                if index + 1 < min(MAX_ATTEMPTS, len(targets)):
                    time.sleep(1.5 + index * 0.8)

        assert last_error is not None
        raise PornHubBlockedError(
            "PornHub bounced every download route for this server IP. "
            "See the startup logs / set PH_PROXY."
        ) from last_error

    filename = await asyncio.to_thread(_run)
    path = Path(filename)
    if not path.exists():
        # merged output may have a different extension
        candidates = list(out_dir_p.glob(f"{path.stem}.*"))
        if not candidates:
            raise FileNotFoundError("Downloaded file not found")
        path = candidates[0]
    return path, state
