"""PornHub access layer.

Everything that talks to PornHub goes through this module so the whole
"which host / which page / which cookies / which proxy" strategy lives in one
place.

Why this file looks the way it does
-----------------------------------
PornHub serves different answers depending on the *client IP* (Railway
egress IPs are shared/rotating; some are flagged and answered with an
anti-bot bounce: the video page is redirected to the homepage), on the *host*
(www vs apex, and the mirror TLDs .com/.net/.org), and on the *page type*
(the full ``view_video.php`` page can be bounced while the ``/embed/`` player
page for the same video is not).

So extraction tries, in order:
  * the video page on www.<apex> and <apex>,
  * then the same on the mirror TLDs,
  * then the /embed/ player page on each of those hosts,
with a shared, warm cookie jar (persisted briefly so repeated requests look
like one consistent visitor instead of a fresh cookie-less bot each time),
optionally through a proxy (PH_PROXY / PROXY_URL).

The working sample ``main.py`` downloads fine from its own host/IP with a
plain yt-dlp call; when this bot is deployed on an IP that PornHub flags, no
amount of yt-dlp options helps, and the fix is the *egress path* (see
``PornHubBlockedError`` and the startup health-check in bot/main.py).
"""

import asyncio
import logging
import os
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit

import yt_dlp

from ..utils.helpers import fmt_size

logger = logging.getLogger(__name__)

QUALITIES = [360, 480, 720, 1080]

#: PornHub serves the same videos on all three TLDs.
_PH_HOSTS = ("pornhub.com", "pornhub.net", "pornhub.org")

_EXTRACT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "retries": 3,
}

#: Browser-like User-Agent for every PornHub-facing request (the mirror of
#: what the stable MZ-Downloader sends to yt-dlp). PornHub serves different
#: (and slower/throttled) content to clients that do not look like a browser.
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def apply_browser_opts(opts: dict) -> dict:
    """Make a yt-dlp options dict look like a desktop Chrome visitor.

    Mirrors MZ-Downloader's ``social_gateway``: a Chrome UA header plus
    curl_cffi impersonation (when the running yt-dlp supports it). This is
    applied to BOTH extraction and download so the whole request chain
    presents as one browser client.
    """
    opts.setdefault("http_headers", {})["User-Agent"] = _CHROME_UA
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        opts["impersonate"] = ImpersonateTarget(client="chrome")
    except Exception:
        opts["impersonate"] = "chrome"
    return opts

#: Upper bound on page fetches per request before giving up. Enough to cover
#: the host/page matrix without hammering a flagged IP.
MAX_ATTEMPTS = 10

#: Cookie jars are reused for this long so repeated requests look like one
#: consistent visitor (and we do not re-fetch the homepage for every link).
COOKIE_TTL_SECONDS = 600

_PH_PAGE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pornhub.com/",
}

_BOUNCE_MARKERS = (
    "Redirection detected",
    "HTTP Error 403",
    "HTTP Error 429",
)


class PornHubLinkError(ValueError):
    """The URL is not a PornHub video URL we can act on."""


class PornHubBlockedError(RuntimeError):
    """Every access route was bounced by PornHub (anti-bot on this egress IP).

    The video is almost certainly fine — the *server* is being blocked.
    Remedies (in order): redeploy to get a fresh Railway egress IP, change the
    Railway region, or set PH_PROXY to a proxy on a clean IP.
    """


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------

def _apex_of(host: str) -> str:
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def canonical_view_url(url: str) -> str:
    """Normalise any PornHub video URL to the canonical form
    ``https://www.<apex>/view_video.php?viewkey=<id>`` and return it.

    Raises PornHubLinkError if the URL is not a PornHub video link.
    """
    parts = urlsplit(url)
    apex = _apex_of(parts.hostname or "")
    if apex not in _PH_HOSTS:
        raise PornHubLinkError(f"Not a PornHub link: {url}")

    viewkey = parse_qs(parts.query).get("viewkey", [""])[0]
    if not viewkey:
        # maybe an /embed/<id> or /video/show?viewkey= form
        segs = [s for s in parts.path.split("/") if s]
        for s in reversed(segs):
            if s and s.isalnum():
                viewkey = s
                break
    if not viewkey or not viewkey.isalnum():
        raise PornHubLinkError(f"No viewkey in link: {url}")

    return f"https://www.{apex}/view_video.php?viewkey={viewkey}"


def host_candidates(url: str) -> list[str]:
    """The ordered list of hosts to try for ``url``: its own TLD (www then
    apex) first, then the mirror TLDs (www then apex)."""
    apex = _apex_of(urlsplit(url).hostname or "")
    if apex not in _PH_HOSTS:
        return [urlsplit(url).netloc]
    order = [f"www.{apex}", apex]
    for other in _PH_HOSTS:
        if other != apex:
            order += [f"www.{other}", other]
    return order


def attempt_targets(url: str) -> list[str]:
    """Ordered list of page URLs to try for a canonical video URL.

    For every host we try the full page first, then the /embed/ page (a
    different route that is frequently not part of the anti-bot bounce).
    """
    canon = canonical_view_url(url)
    parts = urlsplit(canon)
    viewkey = parse_qs(parts.query)["viewkey"][0]
    targets: list[str] = []
    for host in host_candidates(canon):
        for kind, path in (
            ("view", f"/view_video.php?viewkey={viewkey}"),
            ("embed", f"/embed/{viewkey}"),
        ):
            targets.append(urlunsplit(("https", host, path, "", "")))
    return targets


# --------------------------------------------------------------------------
# Cookie warm-up (persistent, shared, TTL-cached)
# --------------------------------------------------------------------------

_cookie_lock = threading.Lock()
_cookie_cache: dict[str, tuple[float, str]] = {}  # apex -> (timestamp, path)


def get_proxy() -> str | None:
    """Optional proxy for all PornHub traffic (env PH_PROXY / PROXY_URL)."""
    return os.getenv("PH_PROXY") or os.getenv("PROXY_URL") or None


def _curl_session():
    import curl_cffi.requests as cr

    kwargs = {"impersonate": "chrome"}
    proxy = get_proxy()
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    return cr.Session(**kwargs)


def _warm_cookies_for_apex(apex: str) -> str | None:
    """Fetch the homepage of one apex host once and cache its cookies in a
    Netscape cookies file (TTL COOKIE_TTL_SECONDS). Returns the file path or
    None when warming is unavailable/failed (caller then retries blind)."""
    now = time.time()
    with _cookie_lock:
        cached = _cookie_cache.get(apex)
        if cached and now - cached[0] < COOKIE_TTL_SECONDS and os.path.exists(cached[1]):
            return cached[1]

    try:
        with _curl_session() as session:
            resp = session.get(
                f"https://www.{apex}/", headers=_PH_PAGE_HEADERS, timeout=25
            )
            if resp.status_code != 200:
                return None
            cookies = list(session.cookies.jar)
    except Exception:
        logger.debug("PornHub warm-up failed for %s", apex, exc_info=True)
        return None

    if not cookies:
        return None

    fd, path = tempfile.mkstemp(prefix=f"ph_cookies_{apex.split('.')[0]}_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            expires = int(c.expires) if c.expires else 0
            secure = "TRUE" if c.secure else "FALSE"
            domain = c.domain or f".{apex}"
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            fh.write(
                f"{domain}\t{include_sub}\t{c.path or '/'}\t{secure}\t{expires}\t"
                f"{c.name}\t{c.value}\n"
            )

    with _cookie_lock:
        old = _cookie_cache.get(apex)
        _cookie_cache[apex] = (time.time(), path)
    if old:
        try:
            os.unlink(old[1])
        except OSError:
            pass
    logger.info("PornHub warm-up OK for %s (%d cookies)", apex, len(cookies))
    return path


def warm_cookies_file(url: str) -> str | None:
    """Cookie file path for the apex of ``url`` (warming on first use)."""
    return _warm_cookies_for_apex(_apex_of(urlsplit(url).hostname or ""))


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def _extract_sync(url: str, cookies_file: str | None = None) -> dict:
    opts = apply_browser_opts(dict(_EXTRACT_OPTS))
    if cookies_file:
        opts["cookiefile"] = cookies_file
    proxy = get_proxy()
    if proxy:
        opts["proxy"] = proxy
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def is_bounce_error(exc: Exception) -> bool:
    """True when the failure looks like an anti-bot/access bounce rather than
    a genuine "video is gone / invalid" answer."""
    text = str(exc)
    return any(m in text for m in _BOUNCE_MARKERS) or "Unable to download webpage" in text


def _probe_redirect_target(url: str) -> str:
    """One extra browser-style request describing where PornHub actually sent
    us — turns the generic yt-dlp error into an actionable log line."""
    try:
        with _curl_session() as session:
            r = session.get(url, headers=_PH_PAGE_HEADERS, timeout=25)
        return f"final_url={r.url} status={r.status_code} len={len(r.content)}"
    except Exception as exc:
        return f"probe error: {type(exc).__name__}: {str(exc)[:200]}"


def _extract_with_retry(url: str, attempts: int = MAX_ATTEMPTS) -> dict:
    canon = canonical_view_url(url)
    targets = attempt_targets(canon)
    apex = _apex_of(urlsplit(canon).hostname or "")
    cookies_file = _warm_cookies_for_apex(apex)

    last_error: Exception | None = None
    for index, target in enumerate(targets[:attempts]):
        # Warm the mirror apex lazily the first time we touch it.
        target_apex = _apex_of(urlsplit(target).hostname or "")
        if target_apex != apex:
            cookies_file = _warm_cookies_for_apex(target_apex) or cookies_file
        try:
            return _extract_sync(target, cookies_file)
        except Exception as exc:  # DownloadError/ExtractorError/network…
            last_error = exc
            if not is_bounce_error(exc):
                # Real answer (removed/private/locked/…): do not keep hopping
                # hosts, surface it.
                raise
            logger.warning(
                "PornHub attempt %d/%d bounced (%s): %s",
                index + 1, min(attempts, len(targets)), target, exc,
            )
            if index + 1 < min(attempts, len(targets)):
                time.sleep(1.0 + index * 0.4)

    assert last_error is not None
    logger.warning("PornHub redirect probe (%s): %s", canon, _probe_redirect_target(canon))
    raise PornHubBlockedError(
        "PornHub bounced every access route for this server IP "
        "(video page redirected to homepage). The video is likely fine; "
        "the server's egress IP is being blocked. Redeploy for a fresh IP, "
        "change the Railway region, or set PH_PROXY to a clean proxy."
    ) from last_error


async def extract_info(url: str, attempts: int = MAX_ATTEMPTS) -> dict:
    return await asyncio.to_thread(_extract_with_retry, url, attempts)


# --------------------------------------------------------------------------
# Quality / summary helpers (unchanged behaviour)
# --------------------------------------------------------------------------

#: Rough per-quality average bitrate (kbps) used ONLY when PornHub gives no
#: tbr for a height (rare — HLS variants normally carry tbr). Calibrated so a
#: ~10:29 1080p video lands near 183 MB (the user's observed real-world size).
_QUALITY_REF_KBPS = {360: 800, 480: 1300, 720: 2600, 1080: 2450}


def estimate_bytes(duration: int | None, tbr_kbps: int | None, height: int) -> int | None:
    """Approximate size in bytes from duration × bitrate.

    PornHub never reports a filesize for HLS streams, so this is the only way
    to show a size in the menu. ``tbr`` (per-video bitrate, when present) makes
    it accurate to a few percent; otherwise a per-quality reference is used.
    """
    dur = duration or 0
    if not dur:
        return None
    kbps = tbr_kbps or _QUALITY_REF_KBPS.get(height)
    if not kbps:
        return None
    return int(dur * kbps * 1000 / 8)


def _best_tbr_for_height(info: dict, height: int) -> int | None:
    """Highest bitrate among ALL formats at ``height``.

    The menu picks a progressive format (which has no tbr) while yt-dlp will
    actually download the same-height HLS variant (which carries the tbr) —
    so look across the whole height, not just the picked format.
    """
    best = 0
    for f in info.get("formats") or []:
        if f.get("height") == height:
            best = max(best, f.get("tbr") or 0)
    return best or None


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
    """Selector resolving to the best format at the SAME height as ``fmt``.

    Selecting by height (not format id) is required: PornHub varies between
    direct mp4 ids (``720p``) and HLS ids (``hls-2512``) across requests, so
    an id captured earlier may not exist on the next request.
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
    """Compact summary used in captions.

    PornHub never sends a filesize, so sizes are estimated from
    duration × bitrate and marked with a ``~`` prefix; the real size is
    recorded after the actual download.
    """
    qualities = pick_qualities(info)
    duration = info.get("duration")
    sizes = {}
    for h, f in qualities.items():
        actual = f.get("filesize") or f.get("filesize_approx")
        approx = False
        if actual:
            size = actual
        else:
            size = estimate_bytes(duration, _best_tbr_for_height(info, h), h)
            approx = size is not None
        size_str = None if size is None else fmt_size(size)
        if size is not None and approx:
            size_str = "~" + size_str
        sizes[h] = {
            "size": size,  # estimate when approx is True
            "size_approx": approx,
            "size_str": size_str,
            "format_id": f.get("format_id"),
            "format_spec": format_spec_for(f),
            "protocol": f.get("protocol"),
            "tbr": f.get("tbr") or _best_tbr_for_height(info, h),  # kbps
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
