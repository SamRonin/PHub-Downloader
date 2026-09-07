import asyncio
import httpx

from ..config import settings

API_BASE = "https://pixeldrain.com/api"


class PixeldrainError(Exception):
    pass


async def upload_file(path: str, name: str, state: dict | None = None) -> str:
    """Stream-upload a file to Pixeldrain. Returns the file id."""
    state = state if state is not None else {"phase": "upload", "pct": 0}
    import os

    total = os.path.getsize(path)
    uploaded = 0

    def file_iter():
        nonlocal uploaded
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 512)
                if not chunk:
                    break
                uploaded += len(chunk)
                if total:
                    state["pct"] = min(int(uploaded * 100 / total), 99)
                yield chunk

    auth = (settings.PIXELDRAIN_API_KEY, "")
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0), auth=auth) as client:
        resp = await client.put(f"{API_BASE}/file/{name}", content=file_iter())
        if resp.status_code not in (200, 201):
            raise PixeldrainError(f"Pixeldrain upload failed: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        file_id = data.get("id")
        if not file_id:
            raise PixeldrainError(f"Pixeldrain response missing id: {data}")
        state["pct"] = 100
        return file_id


async def delete_file(file_id: str) -> bool:
    auth = (settings.PIXELDRAIN_API_KEY, "")
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
