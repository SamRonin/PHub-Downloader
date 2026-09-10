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
import html as _html
import logging
import os
import re
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

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
# Search
# --------------------------------------------------------------------------
#
# PornHub has no public JSON search API, so we read the same results page the
# website uses (``/video/search?search=...&page=N``) with our normal
# browser-impersonated session and parse the result cards out of the HTML.
# This reuses the whole host-rotation / warm-cookie / proxy machinery above,
# so search survives the same anti-bot bounces as extraction does.

#: Results live in <li class="pcVideoListItem …"> cards.
_SEARCH_ITEM_RE = re.compile(
    r'<li[^>]*class="[^"]*pcVideoListItem[^"]*"[\s\S]*?</li>', re.IGNORECASE
)
_SEARCH_LINK_RE = re.compile(
    r'<a\s+href="/view_video\.php\?viewkey=([A-Za-z0-9]+)"\s+title="([^"]*)"',
    re.IGNORECASE,
)
_SEARCH_VIEWKEY_RE = re.compile(r'data-video-vkey="([A-Za-z0-9]+)"')
#: Two card variants exist on the listing page:
#:   <var class="duration">12:57</var>
#:   <var class="bgShadeEffect duration tooltipTrig" …>12:57</var>
_SEARCH_DURATION_RE = re.compile(r'<var class="[^"]*duration[^"]*"[^>]*>([^<]*)</var>')
#:   <span class="views"><var>266K</var> views</span>
#:   <span class="views"><i class="ph-icon-view-on …"></i><var>47.5M</var>
_SEARCH_VIEWS_RE = re.compile(
    r'<span class="views">(?:\s*<i[^>]*></i>)?\s*<var>([^<]*)</var>', re.DOTALL
)
_SEARCH_THUMB_RE = re.compile(r'data-image="([^"]+)"')

#: Cap on how many results we keep from a single page (the page ships ~38).
SEARCH_PAGE_SIZE = 24


def search_targets(query: str, page: int = 1) -> list[str]:
    """Ordered list of search URLs to try (same host rotation as extraction)."""
    from urllib.parse import urlencode

    qs = urlencode({"search": query, "page": max(int(page), 1)})
    path = f"/video/search?{qs}"
    return [f"https://{host}{path}" for host in host_candidates("https://www.pornhub.com/x")]


def parse_search_html(html: str) -> list[dict]:
    """Extract result cards from a PornHub search/listing page."""
    results: list[dict] = []
    seen: set[str] = set()
    for block in _SEARCH_ITEM_RE.findall(html or ""):
        link = _SEARCH_LINK_RE.search(block)
        viewkey_m = _SEARCH_VIEWKEY_RE.search(block)
        viewkey = link.group(1) if link else (viewkey_m.group(1) if viewkey_m else None)
        if not viewkey or viewkey in seen:
            continue
        title = _html.unescape(link.group(2)).strip() if link else ""
        if not title:
            continue
        duration_m = _SEARCH_DURATION_RE.search(block)
        views_m = _SEARCH_VIEWS_RE.search(block)
        thumb_m = _SEARCH_THUMB_RE.search(block)
        seen.add(viewkey)
        results.append(
            {
                "viewkey": viewkey,
                "title": title,
                "url": canonical_view_url(
                    f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
                ),
                "duration": duration_m.group(1).strip() if duration_m else None,
                "views": views_m.group(1).strip() if views_m else None,
                "thumbnail": _html.unescape(thumb_m.group(1)) if thumb_m else None,
            }
        )
        if len(results) >= SEARCH_PAGE_SIZE:
            break
    return results


def _search_sync(query: str, page: int = 1) -> list[dict]:
    """Run one search page against every host route until one answers."""
    query = (query or "").strip()
    if not query:
        return []

    last_error: Exception | None = None
    for index, target in enumerate(search_targets(query, page)):
        apex = _apex_of(urlsplit(target).hostname or "")
        cookies_file = _warm_cookies_for_apex(apex)
        try:
            with _curl_session() as session:
                resp = session.get(target, headers=_PH_PAGE_HEADERS, timeout=30)
            if resp.status_code != 200:
                last_error = RuntimeError(
                    f"search HTTP {resp.status_code} via {target.split('/')[2]}"
                )
                continue
            results = parse_search_html(resp.text)
            if results:
                logger.info(
                    "PornHub search %r page %d -> %d results via %s",
                    query, page, len(results), target.split("/")[2],
                )
                return results
            last_error = RuntimeError("no result cards in search page")
        except Exception as exc:  # network / impersonation / proxy errors
            last_error = exc
            logger.warning(
                "search attempt %d via %s failed: %s",
                index + 1, target.split("/")[2], str(exc)[:160],
            )
        finally:
            if cookies_file:
                # Cookies are cached inside the module; nothing to clean here.
                pass

    if last_error is not None:
        logger.warning("search failed on every host: %s", last_error)
    raise PornHubBlockedError(
        "PornHub search was blocked or returned nothing on every host. "
        "The server's egress IP may be flagged; set PH_PROXY or redeploy."
    ) from last_error


async def search_videos(query: str, page: int = 1) -> list[dict]:
    """Search PornHub, returning result cards (title, viewkey, duration…)."""
    return await asyncio.to_thread(_search_sync, query, page)


# --------------------------------------------------------------------------
# Producers (channels / pornstars / models)
# --------------------------------------------------------------------------
#
# Same scrape-the-HTML approach as video search. PornHub has no public JSON
# API for people/channels, so we hit:
#   /channels/search?channelSearch=Q
#   /pornstars/search?search=Q
# then the profile page + the most-viewed listing
#   /channels/<slug>/videos?o=vi     (Most Viewed on a channel)
#   /pornstar/<slug>/videos?o=mv
#   /model/<slug>/videos?o=mv
# and reuse parse_search_html on the listing *section* — the rest of the page
# carries recommended videos in the nav dropdown that must not leak in.

PRODUCER_TOP_N = 10
_PRODUCER_MAX_SEARCH = 24
_RESERVED_PRODUCER_SLUGS = frozenset({"search", "discover"})

_PRODUCER_URL_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*(?:pornhub\.(?:com|net|org))/"
    r"(?P<kind>channels|pornstar|model)/(?P<slug>[A-Za-z0-9_-]+)"
    r"(?:/videos)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)

#: kind stored on our cards -> URL path segment
_KIND_PATH = {
    "channel": "channels",
    "pornstar": "pornstar",
    "model": "model",
}


class ProducerNotFoundError(ValueError):
    """The producer slug does not resolve to a profile we can parse."""


def parse_producer_url(text: str) -> tuple[str, str] | None:
    """Return ``(kind, slug)`` for a channel/pornstar/model URL, else None.

    ``kind`` is one of ``channel``, ``pornstar``, ``model``.
    """
    m = _PRODUCER_URL_RE.match((text or "").strip())
    if not m:
        return None
    slug = m.group("slug")
    if slug.lower() in _RESERVED_PRODUCER_SLUGS:
        return None
    kind = m.group("kind").lower()
    if kind == "channels":
        kind = "channel"
    return kind, slug


def format_count(value) -> str | None:
    """Pretty-print a PornHub count (``8,463,389``, ``8463389``, ``1.7B``)."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return f"{value:,}"
    s = str(value).strip()
    s = re.sub(r"\s*views?\s*$", "", s, flags=re.IGNORECASE).strip()
    digits = s.replace(",", "").replace(" ", "")
    if digits.isdigit():
        return f"{int(digits):,}"
    return s


def compact_count(value) -> str | None:
    """Short form for lists: ``8.5M``, ``10.6B``, ``1.7B``."""
    formatted = format_count(value)
    if not formatted:
        return None
    digits = formatted.replace(",", "")
    if digits.isdigit():
        n = int(digits)
        if n >= 1_000_000_000:
            v = n / 1_000_000_000
            return f"{v:.1f}B".replace(".0B", "B")
        if n >= 1_000_000:
            v = n / 1_000_000
            return f"{v:.1f}M".replace(".0M", "M")
        if n >= 10_000:
            v = n / 1_000
            return f"{v:.1f}K".replace(".0K", "K")
        return f"{n:,}"
    return formatted


def _get_ph_html(path: str) -> str:
    """GET a PornHub path with host rotation. Returns HTML or raises."""
    if not path.startswith("/"):
        path = "/" + path
    last_error: Exception | None = None
    dummy = "https://www.pornhub.com/x"
    for index, host in enumerate(host_candidates(dummy)):
        url = f"https://{host}{path}"
        apex = _apex_of(host)
        _warm_cookies_for_apex(apex)
        try:
            with _curl_session() as session:
                resp = session.get(url, headers=_PH_PAGE_HEADERS, timeout=30)
            if resp.status_code != 200:
                last_error = RuntimeError(f"HTTP {resp.status_code} via {host}")
                continue
            text = resp.text or ""
            if len(text) < 4000:
                last_error = RuntimeError(f"short body via {host}")
                continue
            final_path = urlsplit(str(resp.url)).path or "/"
            wanted = path.split("?", 1)[0].rstrip("/")
            if final_path.rstrip("/") in ("", "/") and wanted not in ("", "/"):
                last_error = RuntimeError(f"bounced to homepage via {host}")
                continue
            return text
        except Exception as exc:
            last_error = exc
            logger.warning(
                "html fetch %d via %s failed: %s",
                index + 1, host, str(exc)[:160],
            )
    raise PornHubBlockedError(
        "PornHub blocked every host for this page. "
        "The server's egress IP may be flagged; set PH_PROXY or redeploy."
    ) from last_error


def _section_html(html: str, section_id: str) -> str:
    """Slice the HTML starting at ``id="section_id"`` (the real element, not JS)."""
    m = re.search(rf'\bid=["\']{re.escape(section_id)}["\']', html or "")
    if not m:
        return ""
    return html[m.start() : m.start() + 220_000]


def parse_listing_videos(
    html: str, section_ids: list[str], limit: int = PRODUCER_TOP_N
) -> list[dict]:
    """Video cards from a listing section (never the whole page — nav leak)."""
    for sid in section_ids:
        chunk = _section_html(html, sid)
        if not chunk:
            continue
        items = parse_search_html(chunk)
        if items:
            return items[:limit]
    return []


def parse_channel_search(html: str) -> list[dict]:
    """Cards from ``/channels/search?channelSearch=…``."""
    m = re.search(r'id="searchChannelsSection"', html or "")
    section = html[m.start() : m.start() + 250_000] if m else (html or "")
    results: list[dict] = []
    seen: set[str] = set()
    # Split per card so stats from the previous channel cannot leak in.
    for block in re.split(r'<div class="channelsWrapper', section, flags=re.I)[1:]:
        match = re.search(
            r'<a href="/(channels)/([A-Za-z0-9_-]+)" class="usernameLink">\s*([^<]+)\s*</a>',
            block,
            re.IGNORECASE,
        )
        if not match:
            continue
        slug = match.group(2)
        name = _html.unescape(match.group(3)).strip()
        if not name or slug.lower() in seen or slug.lower() in _RESERVED_PRODUCER_SLUGS:
            continue
        seen.add(slug.lower())
        avatar = None
        am = re.search(
            r'<img[^>]+src="(https://[^"]+)"[^>]*alt="[^"]*avatar', block, re.IGNORECASE
        )
        if not am:
            am = re.search(r'<img[^>]+src="(https://ei\.phncdn\.com[^"]+)"', block, re.I)
        if am:
            avatar = _html.unescape(am.group(1))
        subs_m = re.search(r"<span>([\d,]+)</span>\s*Subscribers", block, re.I)
        views_m = re.search(r"<span>([\d,]+)</span>\s*Videos\s*Views", block, re.I)
        vids_m = re.search(r"<span>([\d,]+)</span>\s*Videos(?!\s*Views)", block, re.I)
        results.append(
            {
                "kind": "channel",
                "slug": slug,
                "name": name,
                "url": f"https://www.pornhub.com/channels/{slug}",
                "avatar": avatar,
                "subscribers": subs_m.group(1) if subs_m else None,
                "video_count": vids_m.group(1) if vids_m else None,
                "views": views_m.group(1) if views_m else None,
                "rank": None,
            }
        )
        if len(results) >= _PRODUCER_MAX_SEARCH:
            break
    return results


def parse_pornstar_search(html: str) -> list[dict]:
    """Cards from ``/pornstars/search?search=…`` (pornstars + models)."""
    m = re.search(r'id="pornstarsSearchResult"', html or "")
    section = html[m.start() : m.start() + 250_000] if m else (html or "")
    results: list[dict] = []
    seen: set[str] = set()
    for block in re.split(r'<div class="wrap">', section)[1:]:
        match = re.search(
            r'<a href="/(pornstar|model)/([A-Za-z0-9_-]+)"[^>]*class="title"[^>]*>\s*([^<]+)\s*</a>',
            block,
            re.IGNORECASE,
        )
        if not match:
            continue
        kind = match.group(1).lower()
        slug = match.group(2)
        name = _html.unescape(match.group(3)).strip()
        key = f"{kind}/{slug.lower()}"
        if not name or key in seen:
            continue
        seen.add(key)
        avatar = None
        am = re.search(r'<img[^>]+src="(https://[^"]+)"', block, re.I)
        if am:
            avatar = _html.unescape(am.group(1))
        vids_m = re.search(r'class="videosNumber">\s*([\d,]+)', block)
        views_m = re.search(r'class="pstarViews">\s*([^<]+)', block)
        rank_m = re.search(r'class="rank_number">\s*([\d,]+)', block)
        views = None
        if views_m:
            views = re.sub(
                r"\s*views?\s*$", "", views_m.group(1).strip(), flags=re.I
            ).strip()
        results.append(
            {
                "kind": kind,
                "slug": slug,
                "name": name,
                "url": f"https://www.pornhub.com/{kind}/{slug}",
                "avatar": avatar,
                "subscribers": None,
                "video_count": vids_m.group(1) if vids_m else None,
                "views": views or None,
                "rank": rank_m.group(1).strip() if rank_m else None,
            }
        )
        if len(results) >= _PRODUCER_MAX_SEARCH:
            break
    return results


def parse_channel_profile(html: str) -> dict:
    """Name / avatar / stats from a ``/channels/<slug>`` page."""
    name = None
    m = re.search(r"<h1[^>]*>\s*([^<]+)", html or "", re.I)
    if m:
        name = _html.unescape(m.group(1)).strip()
    avatar = None
    m = re.search(r'id="getAvatar"[^>]*src="([^"]+)"', html or "", re.I)
    if m:
        avatar = _html.unescape(m.group(1))
    stats: dict[str, str | None] = {
        "views": None, "subscribers": None, "video_count": None, "rank": None,
    }
    for sm in re.finditer(
        r'class="info[^"]*"\s*>\s*([\d,]+)\s*<br\s*/?>\s*<span>\s*([^<]+)',
        html or "",
        re.I,
    ):
        label = sm.group(2).strip().upper()
        val = sm.group(1).strip()
        if "VIEW" in label:
            stats["views"] = val
        elif "SUBSCRIB" in label:
            stats["subscribers"] = val
        elif "VIDEO" in label:
            stats["video_count"] = val
        elif "RANK" in label:
            stats["rank"] = val
    bio = None
    m = re.search(
        r'class="cdescriptions"[^>]*>\s*<p class="joined">([^<]+)', html or "", re.I
    )
    if m:
        bio = _html.unescape(m.group(1)).strip() or None
    joined = None
    m = re.search(
        r'channelInfoHeadlines">\s*JOINED\s*</span>\s*<span>([^<]+)', html or "", re.I
    )
    if m:
        joined = _html.unescape(m.group(1)).strip() or None
    return {
        "name": name,
        "avatar": avatar,
        "bio": bio,
        "joined": joined,
        **stats,
    }


def parse_person_profile(html: str) -> dict:
    """Name / avatar / stats from a pornstar or model page."""
    name = None
    m = re.search(r'<h1[^>]*itemprop="name"[^>]*>\s*([^<]+)', html or "", re.I)
    if not m:
        m = re.search(r"<h1[^>]*>\s*([^<]+)", html or "", re.I)
    if m:
        name = _html.unescape(m.group(1)).strip()
    avatar = None
    m = re.search(r'id="getAvatar"[^>]*src="([^"]+)"', html or "", re.I)
    if m:
        avatar = _html.unescape(m.group(1))
    views = None
    m = re.search(r'data-title="Video views:\s*([^"]+)"', html or "", re.I)
    if m:
        views = m.group(1).strip()
    subscribers = None
    m = re.search(r'data-title="Subscribers:\s*([^"]+)"', html or "", re.I)
    if m:
        subscribers = m.group(1).strip()
    rank = None
    m = re.search(
        r'class="infoBox"[^>]*>\s*<span class="big">([\s\S]*?)</span>\s*'
        r'<div class="title">\s*Model Rank',
        html or "",
        re.I,
    )
    if m:
        num = re.search(r"([\d,]+)", m.group(1))
        if num:
            rank = num.group(1)
    bio = None
    m = re.search(r'itemprop="description"[^>]*>([^<]+)', html or "", re.I)
    if m:
        bio = _html.unescape(m.group(1)).strip() or None
    # Do NOT use showingCounter — on a pornstar home page it is a subsection
    # ("Showing 1-12 of 120") not the real catalogue size. Search cards carry
    # the accurate count and the handler merges it in.
    return {
        "name": name,
        "avatar": avatar,
        "bio": bio,
        "joined": None,
        "views": views,
        "subscribers": subscribers,
        "video_count": None,
        "rank": rank,
    }


def _rank_producer(item: dict, query: str) -> tuple:
    name = (item.get("name") or "").lower()
    ql = query.lower().strip()
    if name == ql:
        exact = 0
    elif name.startswith(ql):
        exact = 1
    elif ql in name:
        exact = 2
    else:
        exact = 3
    kind_order = 0 if item.get("kind") == "channel" else 1
    return (exact, kind_order)


def _search_producers_sync(query: str) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    channels: list[dict] = []
    people: list[dict] = []
    failed = 0
    last_error: Exception | None = None
    try:
        ch_path = "/channels/search?" + urlencode({"channelSearch": query})
        channels = parse_channel_search(_get_ph_html(ch_path))
    except Exception as exc:
        failed += 1
        last_error = exc
        logger.warning("channel search failed for %r: %s", query, exc)
    try:
        ps_path = "/pornstars/search?" + urlencode({"search": query})
        people = parse_pornstar_search(_get_ph_html(ps_path))
    except Exception as exc:
        failed += 1
        last_error = exc
        logger.warning("pornstar search failed for %r: %s", query, exc)

    if not channels and not people:
        if failed == 2:
            raise PornHubBlockedError(
                "PornHub producer search was blocked on every host."
            ) from last_error
        return []

    merged = channels + people
    merged.sort(key=lambda item: _rank_producer(item, query))
    seen: set[str] = set()
    out: list[dict] = []
    for item in merged:
        key = f"{item.get('kind')}/{item.get('slug', '').lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= _PRODUCER_MAX_SEARCH:
            break
    logger.info(
        "producer search %r -> %d channels + %d people = %d",
        query, len(channels), len(people), len(out),
    )
    return out


def _fetch_producer_sync(kind: str, slug: str) -> dict:
    kind = (kind or "").lower().strip()
    if kind == "channels":
        kind = "channel"
    slug = (slug or "").strip()
    if kind not in _KIND_PATH or not slug:
        raise ProducerNotFoundError(f"bad producer {kind}/{slug}")
    path_kind = _KIND_PATH[kind]
    profile_path = f"/{path_kind}/{slug}"
    html = _get_ph_html(profile_path)
    if kind == "channel":
        profile = parse_channel_profile(html)
        videos_path = f"/{path_kind}/{slug}/videos?o=vi"
        section_ids = ["showAllChanelVideos", "moreData"]
    else:
        profile = parse_person_profile(html)
        videos_path = f"/{path_kind}/{slug}/videos?o=mv"
        section_ids = ["mostRecentVideosSection", "uploadedVideosSection"]
    profile["kind"] = kind
    profile["slug"] = slug
    profile["url"] = f"https://www.pornhub.com{profile_path}"
    if not (profile.get("name") or "").strip():
        raise ProducerNotFoundError(f"no profile at {profile_path}")
    videos: list[dict] = []
    try:
        vhtml = _get_ph_html(videos_path)
        videos = parse_listing_videos(vhtml, section_ids, PRODUCER_TOP_N)
    except Exception as exc:
        logger.warning("producer videos fetch failed for %s/%s: %s", kind, slug, exc)
    if not videos:
        videos = parse_listing_videos(html, section_ids, PRODUCER_TOP_N)
    profile["videos"] = videos[:PRODUCER_TOP_N]
    logger.info(
        "producer %s/%s -> %s, %d videos",
        kind, slug, profile.get("name"), len(profile["videos"]),
    )
    return profile


async def search_producers(query: str) -> list[dict]:
    """Search channels + pornstars/models. Returns producer cards."""
    return await asyncio.to_thread(_search_producers_sync, query)


async def fetch_producer(kind: str, slug: str) -> dict:
    """Full profile + up to ``PRODUCER_TOP_N`` most-viewed videos."""
    return await asyncio.to_thread(_fetch_producer_sync, kind, slug)


# --------------------------------------------------------------------------
# Quality / summary helpers (unchanged behaviour)
# --------------------------------------------------------------------------

#: Rough per-quality average bitrate (kbps) used ONLY when PornHub gives no
#: tbr for a height (rare — HLS variants normally carry tbr). Calibrated so a
#: ~10:29 1080p video lands near 183 MB (the user's observed real-world size),
#: and 360 (always synthetic) near a measured 480p->360p encode (~530 kbps).
_QUALITY_REF_KBPS = {360: 600, 480: 1400, 720: 2600, 1080: 2450}


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


def format_duration(seconds) -> str | None:
    """Seconds -> "M:SS" / "H:MM:SS" (None when unknown)."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


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
    # --- Synthetic 360p ---------------------------------------------------
    # PornHub only publishes 480p/720p/1080p. Users still get a 360p button:
    # we download the 480p stream and downscale it locally (see
    # bot/services/transcode.py). The entry reuses the 480p format selector,
    # so the downloader fetches 480p and the handler transcodes it.
    if 360 not in sizes and sizes:
        source = sizes.get(480) or sizes[min(sizes)]
        size = estimate_bytes(duration, None, 360)  # reference bitrate
        size_str = None if size is None else "~" + fmt_size(size)
        sizes[360] = {
            "size": size,
            "size_approx": True,
            "size_str": size_str,
            "format_id": "360p",
            "format_spec": source["format_spec"],  # resolves to the 480p stream
            "protocol": source.get("protocol"),
            "tbr": _QUALITY_REF_KBPS.get(360),
            "synthetic": True,
            "source_height": source.get("source_height") or 480,
        }

    uploader = (
        info.get("uploader")
        or info.get("channel")
        or info.get("creator")
        or info.get("uploader_id")
    )

    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "description": info.get("description"),
        "view_count": info.get("view_count"),
        "duration": info.get("duration"),
        "duration_str": format_duration(info.get("duration")),
        "uploader": uploader,
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or info.get("original_url"),
        "qualities": sizes,
    }
