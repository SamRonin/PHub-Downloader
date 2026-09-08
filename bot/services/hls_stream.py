"""Stream a PornHub HLS video straight to Pixeldrain without ever storing the
whole file on the (1 GB) Railway ephemeral disk.

Why this exists
---------------
The bot's big-file path (> 50 MB, delivered via Pixeldrain) used to first
download the complete file to ``TEMP_DIR`` and then upload it. On Railway's
free plan the ephemeral disk is capped at 1 GB, so a large 1080p video (or a
few concurrent downloads) fills the disk and Railway force-stops the service.

Instead, for big files we read PornHub's HLS stream segment-by-segment and
stream the bytes straight into a Pixeldrain ``PUT``:
  * a tiny init segment (EXT-X-MAP) first, then every ``seg-*.m4s`` in order;
  * the server keeps only a small in-memory buffer (one segment at a time);
  * before the upload we probe each segment's Content-Length (cheap keep-alive
    GETs that read only the header) so the PUT has an exact Content-Length and
    a real percent can be reported.

Retry strategy
--------------
Segment fetches are retried a few times; if a segment is genuinely gone the
upload aborts and the caller falls back to the classic local-disk path.
"""

import asyncio
import logging
import time
from urllib.parse import urljoin

import httpx

from bot.config import settings
from bot.services import pixeldrain
from bot.services.ph import extract_info

logger = logging.getLogger(__name__)

_PH_HEADERS = {
    "Accept": "*/*",
    "Origin": "https://www.pornhub.com",
    "Referer": "https://www.pornhub.com/",
}

_SEGMENT_ATTEMPTS = 3
_MAX_PLAYLIST_HOPS = 3
_STREAM_CHUNK = 256 * 1024


class HlsStreamError(Exception):
    pass


def _session():
    from curl_cffi import requests as cr

    return cr.Session(impersonate="chrome")


def _fetch_media_playlist(session, url: str):
    """Resolve (possibly via a master playlist) to a media playlist.

    Returns (media_text, media_base_url).
    """
    current = url
    for _ in range(_MAX_PLAYLIST_HOPS):
        resp = session.get(current, headers=_PH_HEADERS, timeout=30)
        resp.raise_for_status()
        text = resp.text
        if "#EXTINF" in text:
            return text, current
        next_uri = None
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                next_uri = line
                break
        if not next_uri:
            raise HlsStreamError("Unrecognised HLS playlist (no variants, no EXTINF)")
        current = urljoin(current, next_uri)
    raise HlsStreamError("Too many playlist hops")


def _parse_media_playlist(text: str, base_url: str):
    """Return (init_url|None, [segment_url, ...]) in play order."""
    init_url = None
    segments: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:URI="):
            raw = line.split('URI="', 1)[1].split('"', 1)[0]
            init_url = urljoin(base_url, raw)
        elif line and not line.startswith("#"):
            segments.append(urljoin(base_url, line))
    if not segments:
        raise HlsStreamError("Playlist contains no segments")
    return init_url, segments


def _probe_lengths(session, urls) -> int | None:
    """Total byte count of ``urls`` (incl. init), or None when any server
    fails to provide Content-Length (caller then falls back to local disk)."""
    total = 0
    for u in urls:
        try:
            resp = session.get(u, headers=_PH_HEADERS, timeout=30, stream=True)
        except Exception:
            return None
        try:
            length = resp.headers.get("Content-Length")
            if not length:
                return None
            total += int(length)
        finally:
            resp.close()
    return total


def _read_one(session, url: str) -> bytes:
    last: Exception | None = None
    for attempt in range(_SEGMENT_ATTEMPTS):
        try:
            resp = session.get(url, headers=_PH_HEADERS, timeout=60)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            last = exc
            if attempt + 1 < _SEGMENT_ATTEMPTS:
                time.sleep(0.5 * (attempt + 1))
    raise HlsStreamError(f"segment fetch failed: {last}")


def _stream_to_pixeldrain_sync(
    api_base: str,
    auth: tuple[str, str],
    name: str,
    fmt_url: str,
    state: dict,
    rate_limit: int = 0,
) -> tuple[str, int]:
    """Core streaming upload (runs in a worker thread). Returns (file_id, total).

    All PornHub HTTP happens through one curl_cffi (impersonated) session;
    the Pixeldrain PUT goes through a blocking httpx.Client streaming the
    bytes we read from PornHub — so memory stays bounded by one segment.
    ``rate_limit`` (bytes/sec, 0 = unlimited) enforces the free-tier speed cap
    the same way yt-dlp's ratelimit does on the local-disk path.
    """
    session = _session()
    try:
        text, media_url = _fetch_media_playlist(session, fmt_url)
        init_url, seg_urls = _parse_media_playlist(text, media_url)
        all_urls = ([init_url] if init_url else []) + seg_urls

        total = _probe_lengths(session, all_urls)
        if not total:
            raise HlsStreamError("segment Content-Length unavailable")

        sent = 0
        started = time.monotonic()
        state["phase"] = "upload"
        state["pct"] = 0

        def _gen():
            nonlocal sent
            for u in all_urls:
                chunk = _read_one(session, u)
                for i in range(0, len(chunk), _STREAM_CHUNK):
                    piece = chunk[i : i + _STREAM_CHUNK]
                    yield piece
                    sent += len(piece)
                    state["pct"] = min(int(sent * 100 / total), 99)
                    if rate_limit > 0:
                        # pace so average stays <= rate_limit bytes/sec
                        want = sent / rate_limit
                        extra = want - (time.monotonic() - started)
                        if extra > 0:
                            time.sleep(extra)

        headers = {"Content-Length": str(total)}
        with httpx.Client(timeout=httpx.Timeout(900.0), auth=auth) as client:
            resp = client.put(f"{api_base}/file/{name}", content=_gen(), headers=headers)

        if resp.status_code not in (200, 201):
            raise HlsStreamError(
                f"Pixeldrain upload failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        data = resp.json()
        file_id = data.get("id")
        if not file_id:
            raise HlsStreamError(f"Pixeldrain response missing id: {data}")
        logger.info(
            "Streamed %d bytes to Pixeldrain in %.0fs (%s)",
            sent, time.monotonic() - started, name,
        )
        state["pct"] = 100
        return file_id, sent
    finally:
        try:
            session.close()
        except Exception:
            pass


async def try_stream_to_pixeldrain(
    page_url: str,
    height: int,
    name: str,
    state: dict,
    rate_limit: int = 0,
) -> tuple[str, int] | None:
    """Best-effort streaming upload. Returns ``(file_id, total_bytes)``, or
    None when streaming is not possible (no HLS variant / no segment
    Content-Length / any failure) — the caller then falls back to the classic
    local-disk path."""
    try:
        info = await extract_info(page_url)
        fmt = None
        for f in info.get("formats") or []:
            if f.get("height") == height and f.get("protocol") == "m3u8_native":
                fmt = f
                break
        if fmt is None or not fmt.get("url"):
            logger.info("No HLS variant at %dp; streaming skipped", height)
            return None

        auth = ("", settings.PIXELDRAIN_API_KEY)
        return await asyncio.to_thread(
            _stream_to_pixeldrain_sync,
            pixeldrain.API_BASE,
            auth,
            name,
            fmt["url"],
            state,
            rate_limit,
        )
    except Exception as exc:
        logger.warning("HLS streaming failed (%s); falling back to local path", exc)
        return None
