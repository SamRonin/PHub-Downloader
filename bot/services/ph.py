import asyncio
import logging
import time
from urllib.parse import urlsplit, urlunsplit

import yt_dlp

from ..utils.helpers import fmt_size

logger = logging.getLogger(__name__)

QUALITIES = [360, 480, 720, 1080]

_EXTRACT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "retries": 3,
}

# How many times to re-run an extraction when PornHub serves a redirect /
# bot-check / transient error page. Known failures are usually momentary.
EXTRACT_ATTEMPTS = 4

_PH_APEX_HOSTS = ("pornhub.com", "pornhub.net", "pornhub.org")


def host_candidates(url: str) -> list[str]:
    """Return [original_url, same_url_with_www/apex_toggled] (deduplicated).

    PornHub sometimes answers one host with a redirect/bot page while the
    other works, so re-trying on the alternate host helps a lot from
    datacenter IPs.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    apex = host[4:] if host.startswith("www.") else host
    if apex not in _PH_APEX_HOSTS:
        return [url]
    alt = f"www.{apex}" if host == apex else apex
    out = []
    for h in (host, alt):
        candidate = urlunsplit((parts.scheme, h, parts.path, parts.query, parts.fragment))
        if candidate not in out:
            out.append(candidate)
    return out


def _extract_sync(url: str) -> dict:
    with yt_dlp.YoutubeDL(_EXTRACT_OPTS) as ydl:
        return ydl.extract_info(url, download=False)


def _extract_with_retry(url: str, attempts: int = EXTRACT_ATTEMPTS) -> dict:
    candidates = host_candidates(url) or [url]
    last_error: Exception | None = None
    for attempt in range(attempts):
        target = candidates[attempt % len(candidates)]
        try:
            return _extract_sync(target)
        except Exception as exc:  # DownloadError/ExtractorError/network…
            last_error = exc
            logger.warning(
                "PornHub extract attempt %d/%d failed (%s): %s",
                attempt + 1, attempts, target, exc,
            )
            if attempt + 1 < attempts:
                time.sleep(1.5 + attempt * 1.5)
    assert last_error is not None
    raise last_error


async def extract_info(url: str, attempts: int = EXTRACT_ATTEMPTS) -> dict:
    return await asyncio.to_thread(_extract_with_retry, url, attempts)


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
