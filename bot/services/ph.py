import asyncio

import yt_dlp

from ..config import settings
from ..utils.helpers import fmt_size

QUALITIES = [360, 480, 720, 1080]

_EXTRACT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "socket_timeout": 20,
    "retries": 2,
}


def _extract_sync(url: str) -> dict:
    with yt_dlp.YoutubeDL(_EXTRACT_OPTS) as ydl:
        return ydl.extract_info(url, download=False)


async def extract_info(url: str) -> dict:
    return await asyncio.to_thread(_extract_sync, url)


def pick_qualities(info: dict) -> dict:
    """Pick the best format per target height. Returns {height: format_dict}."""
    result = {}
    for f in info.get("formats") or []:
        h = f.get("height")
        if h not in QUALITIES:
            continue
        progressive = f.get("vcodec") != "none" and f.get("acodec") != "none"
        score = (1 if progressive else 0, f.get("tbr") or 0)
        cur = result.get(h)
        if cur is None or score > cur[0]:
            result[h] = (score, f)
    return {h: v[1] for h, v in result.items()}


def format_spec_for(fmt: dict) -> str:
    """Return a yt-dlp selector that resolves to the best format at the SAME
    height as ``fmt``.

    Selecting by *height* instead of the exact format id is required: PornHub
    varies which representations it serves between requests. Sometimes the
    page contains direct mp4 formats (ids like ``720p``) and sometimes only
    HLS variants (ids like ``hls-2512``, same heights). Locking onto a format
    id taken from an earlier extraction then fails with
    "Requested format is not available" on the next request.
    """
    h = fmt.get("height")
    if not h:
        fid = fmt.get("format_id")
        return fid if fid else "best"
    return (
        f"b[height={h}]/bv*[height={h}]+ba/"
        f"b[height<={h}]/bv*[height<={h}]+ba/b"
    )


def summarize(info: dict) -> dict:
    """Compact summary used in captions."""
    qualities = pick_qualities(info)
    sizes = {}
    for h, f in qualities.items():
        size = f.get("filesize") or f.get("filesize_approx")
        sizes[h] = {
            "size": size,
            "size_str": fmt_size(size),
            "format_id": f.get("format_id"),
            "format_spec": format_spec_for(f),
        }
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "description": info.get("description"),
        "view_count": info.get("view_count"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or info.get("original_url"),
        "qualities": sizes,
    }
