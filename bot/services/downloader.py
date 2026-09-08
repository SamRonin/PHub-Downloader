import asyncio
import os
import time
from pathlib import Path

import yt_dlp

from bot.services.ph import (
    EXTRACT_ATTEMPTS,
    _warm_cookies_file,
    embed_candidates,
    get_proxy,
    host_candidates,
    is_bounce_error,
)


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

    def _build_opts(cookies_file: str | None):
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
        return opts

    def _run() -> str:
        # Same resilience strategy as ph.extract_info: try the classic
        # view_video.php page on every host/mirror, then — only if that failed
        # with an anti-bot bounce — retry on the /embed/ page (different route,
        # often not bounced). A warmed cookie jar (see ph._warm_cookies_file)
        # makes each attempt look like a returning visitor.
        cookies_file = _warm_cookies_file(url)
        last_error: Exception | None = None
        tried = 0
        try:
            def run_once(target: str):
                nonlocal last_error, tried
                tried += 1
                opts = _build_opts(cookies_file)
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(target, download=True)
                        return ydl.prepare_filename(info)
                except Exception as exc:
                    last_error = exc
                    if tried < EXTRACT_ATTEMPTS:
                        time.sleep(1.0 + tried * 0.7)
                    return None

            # stage 1: the classic video pages on every host/mirror
            for target in (host_candidates(url) or [url]):
                if tried >= EXTRACT_ATTEMPTS:
                    break
                filename = run_once(target)
                if filename is not None:
                    return filename

            # stage 2: only when stage 1 ended with an anti-bot bounce
            if last_error is not None and is_bounce_error(last_error):
                for target in embed_candidates(url):
                    if tried >= EXTRACT_ATTEMPTS:
                        break
                    filename = run_once(target)
                    if filename is not None:
                        return filename

            assert last_error is not None
            raise last_error
        finally:
            if cookies_file:
                try:
                    os.unlink(cookies_file)
                except OSError:
                    pass

    filename = await asyncio.to_thread(_run)
    path = Path(filename)
    if not path.exists():
        # merged output may have a different extension
        candidates = list(out_dir_p.glob(f"{path.stem}.*"))
        if not candidates:
            raise FileNotFoundError("Downloaded file not found")
        path = candidates[0]
    return path, state
