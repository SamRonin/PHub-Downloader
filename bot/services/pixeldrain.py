import asyncio
import os

import httpx

from bot.config import settings

API_BASE = "https://pixeldrain.com/api"


class PixeldrainError(Exception):
    pass


class _StreamingFile:
    """Binary file wrapper that counts the bytes it hands out, so the upload
    loop can report progress. Keeps ``fileno()`` so httpx detects a real file
    and sends a proper ``Content-Length`` (exactly like ``curl -T``)."""

    def __init__(self, path: str, on_bytes):
        self._fp = open(path, "rb")
        self._on_bytes = on_bytes

    def read(self, size: int = -1) -> bytes:
        chunk = self._fp.read(size)
        if chunk:
            self._on_bytes(len(chunk))
        return chunk

    # httpx detects iterables via the ``Iterable`` ABC, which requires
    # ``__iter__`` to be defined (httpx itself will stream through ``read``).
    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        chunk = self.read(64 * 1024)
        if not chunk:
            raise StopIteration
        return chunk

    def fileno(self) -> int:
        return self._fp.fileno()

    def close(self) -> None:
        self._fp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


async def upload_file(path: str, name: str, state: dict | None = None) -> str:
    """Stream-upload a file to Pixeldrain. Returns the file id."""
    state = state if state is not None else {"phase": "upload", "pct": 0}
    total = os.path.getsize(path)

    def _run() -> str:
        uploaded = 0

        def bump(n: int) -> None:
            nonlocal uploaded
            uploaded += n
            if total:
                state["pct"] = min(int(uploaded * 100 / total), 99)

        # Pixeldrain expects the API key in the *password* field of HTTP Basic
        # auth (the username is ignored). The request body here is a sync
        # (blocking file) stream, so it must go through a sync httpx.Client --
        # an AsyncClient would reject it with
        # "RuntimeError: Attempted to send an sync request with an AsyncClient
        # instance." Running it in a worker thread keeps the event loop free.
        auth = ("", settings.PIXELDRAIN_API_KEY)
        with httpx.Client(timeout=httpx.Timeout(600.0), auth=auth) as client:
            with _StreamingFile(path, bump) as stream:
                resp = client.put(f"{API_BASE}/file/{name}", content=stream)

            if resp.status_code not in (200, 201):
                raise PixeldrainError(
                    f"Pixeldrain upload failed: HTTP {resp.status_code} {resp.text[:200]}"
                )
            data = resp.json()
            file_id = data.get("id")
            if not file_id:
                raise PixeldrainError(f"Pixeldrain response missing id: {data}")
            state["pct"] = 100
            return file_id

    return await asyncio.to_thread(_run)


async def delete_file(file_id: str) -> bool:
    # API key goes in the password field of HTTP Basic auth (see upload_file).
    auth = ("", settings.PIXELDRAIN_API_KEY)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), auth=auth) as client:
            resp = await client.delete(f"{API_BASE}/file/{file_id}")
            return resp.status_code in (200, 204)
    except httpx.HTTPError:
        return False


def file_page_url(file_id: str) -> str:
    return f"https://pixeldrain.com/u/{file_id}"


def file_direct_url(file_id: str) -> str:
    return f"{API_BASE}/file/{file_id}?download"
