import asyncio
import logging
import os
import tempfile
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit

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

_PH_PAGE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pornhub.com/",
}


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


# ---------------------------------------------------------------------------
# Cookie warm-up
#
# PornHub's anti-bot front door (mostly noticeable from datacenter IPs)
# answers the FIRST request of a cookie-less session with a redirect. After
# the visitor's homepage has set its cookies (ua / ss / sessid / bs / …),
# the video page loads normally. So before extracting we fetch the homepage
# once and hand those cookies to yt-dlp.
# ---------------------------------------------------------------------------

def _warm_cookies_file(url: str) -> str | None:
    """Fetch the PornHub homepage with a fresh browser-impersonating session,
    export the cookies to a Netscape cookies file and return its path.
    Returns None if warming is unavailable/failed (caller then retries blind).
    """
    try:
        import curl_cffi.requests as cr
    except Exception:
        return None
    host = urlsplit(host_candidates(url)[0]).netloc
    try:
        with cr.Session(impersonate="chrome") as session:
            resp = session.get(f"https://{host}/", headers=_PH_PAGE_HEADERS, timeout=25)
            if resp.status_code != 200:
                return None
            cookies = list(session.cookies.jar)
        if not cookies:
            return None
        fd, path = tempfile.mkstemp(prefix="ph_cookies_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# Netscape HTTP Cookie File\n")
            for c in cookies:
                # format: domain  include_subdomains  path  secure  expires  name  value
                # A cookie that arrived with a leading-dot domain is a domain
                # cookie: keep the dot and include_subdomains=TRUE. A host-only
                # cookie has no dot -> include_subdomains=FALSE. (python's
                # cookiejar loader is strict about this.)
                expires = int(c.expires) if c.expires else 0
                secure = "TRUE" if c.secure else "FALSE"
                domain = c.domain or host
                if domain.startswith("."):
                    include_sub = "TRUE"
                else:
                    include_sub = "FALSE"
                fh.write(
                    f"{domain}\t{include_sub}\t{c.path or '/'}\t{secure}\t{expires}\t"
                    f"{c.name}\t{c.value}\n"
                )
        logger.info("PornHub warm-up OK: %d cookies -> %s", len(cookies), os.path.basename(path))
        return path
    except Exception:
        logger.debug("PornHub warm-up failed", exc_info=True)
        return None


def _extract_sync(url: str, cookies_file: str | None = None) -> dict:
    opts = dict(_EXTRACT_OPTS)
    if cookies_file:
        opts["cookiefile"] = cookies_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _probe_redirect_target(url: str) -> str:
    """After every attempt fails with a redirect, fetch the URL once more with
    a browser session and describe where it actually went. This turns the
    generic yt-dlp error into an actionable log line."""
    try:
        import curl_cffi.requests as cr
    except Exception:
        return "(curl_cffi unavailable for probe)"
    viewkey = parse_qs(urlsplit(url).query).get("viewkey", [""])[0]
    host = urlsplit(host_candidates(url)[0]).netloc
    try:
        with cr.Session(impersonate="chrome") as session:
            r = session.get(
                f"https://{host}/view_video.php?viewkey={viewkey}",
                headers=_PH_PAGE_HEADERS, timeout=25,
            )
        return f"final_url={r.url} status={r.status_code} len={len(r.content)}"
    except Exception as exc:
        return f"probe error: {type(exc).__name__}: {str(exc)[:200]}"


def _extract_with_retry(url: str, attempts: int = EXTRACT_ATTEMPTS) -> dict:
    candidates = host_candidates(url) or [url]
    cookies_file = _warm_cookies_file(url)
    last_error: Exception | None = None
    try:
        for attempt in range(attempts):
            target = candidates[attempt % len(candidates)]
            try:
                return _extract_sync(target, cookies_file)
            except Exception as exc:  # DownloadError/ExtractorError/network…
                last_error = exc
                logger.warning(
                    "PornHub extract attempt %d/%d failed (%s): %s",
                    attempt + 1, attempts, target, exc,
                )
                if attempt + 1 < attempts:
                    time.sleep(1.5 + attempt * 1.5)
    finally:
        if cookies_file:
            try:
                os.unlink(cookies_file)
            except OSError:
                pass
    assert last_error is not None
    if "Redirection detected" in str(last_error):
        logger.warning("PornHub redirect probe: %s", _probe_redirect_target(url))
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
