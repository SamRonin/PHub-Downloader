import html
import re

PHUB_PATTERN = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*(?:pornhub\.com|ph\.ncm)/\S+$", re.IGNORECASE
)


def is_phub_url(text: str) -> bool:
    text = (text or "").strip()
    return bool(PHUB_PATTERN.match(text))


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def row_get(row, key: str, default=None):
    """Read ``key`` from a DB row that may be a ``sqlite3.Row``/``aiosqlite.Row``
    or a plain dict.

    Rows do not have a ``.get()`` method (that caused an AttributeError in
    /start), so all row reads that need a fallback go through here.
    """
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value



def fmt_size(num_bytes) -> str:
    if not num_bytes:
        return None
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} PB"
