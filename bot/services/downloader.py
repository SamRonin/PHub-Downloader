import asyncio
import os
import time
from pathlib import Path

import yt_dlp

from bot.services.ph import EXTRACT_ATTEMPTS, _warm_cookies_file, host_candidates


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
        # PornHub sometimes answers with a redirect/bot-check page for one
        # request and works on the next (or on the www/apex twin host), so
        # retry the whole operation a few times. A warmed cookie jar (see
        # ph._warm_cookies_file) makes each attempt look like a returning
        # visitor instead of a cookie-less bot.
        candidates = host_candidates(url) or [url]
        cookies_file = _warm_cookies_file(url)
        try:
            last_error: Exception | None = None
            for attempt in range(EXTRACT_ATTEMPTS):
                target = candidates[attempt % len(candidates)]
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
                if rate_limit:
                    opts["ratelimit"] = rate_limit
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(target, download=True)
                        return ydl.prepare_filename(info)
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < EXTRACT_ATTEMPTS:
                        time.sleep(2.0 + attempt * 2.0)
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
