import secrets
import time


def detect_lang(language_code: str | None) -> str:
    return "fa" if (language_code or "").lower().startswith("fa") else "en"


class InfoCache:
    """In-memory metadata cache with TTL (message -> quality buttons)."""

    def __init__(self, ttl: int = 1800, max_items: int = 8000):
        self.ttl = ttl
        self.max_items = max_items
        self._data: dict[str, tuple[float, dict]] = {}

    def put(self, info: dict) -> str:
        cid = secrets.token_hex(4)
        self._purge()
        self._data[cid] = (time.time(), info)
        return cid

    def get(self, cid: str) -> dict | None:
        item = self._data.get(cid)
        if not item:
            return None
        ts, info = item
        if time.time() - ts > self.ttl:
            self._data.pop(cid, None)
            return None
        return info

    def _purge(self):
        now = time.time()
        expired = [k for k, (ts, _) in self._data.items() if now - ts > self.ttl]
        for k in expired:
            self._data.pop(k, None)
        while len(self._data) > self.max_items:
            self._data.pop(next(iter(self._data)))


info_cache = InfoCache()

#: Search result pages live long enough for a user to browse/paginate them.
search_cache = InfoCache(ttl=900, max_items=2000)
