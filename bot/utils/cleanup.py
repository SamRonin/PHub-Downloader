import asyncio
import logging
import shutil
import time
from pathlib import Path

from ..config import settings
from ..db import db
from ..services import pixeldrain

logger = logging.getLogger(__name__)


def delete_path(path: str):
    """Best-effort guaranteed local deletion."""
    try:
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("Failed to delete %s: %s", path, e)


async def delete_pixeldrain_file(file_id: str) -> bool:
    ok = await pixeldrain.delete_file(file_id)
    if ok:
        await db.mark_upload_deleted(file_id)
    else:
        logger.warning("Failed to delete Pixeldrain file %s (will retry)", file_id)
    return ok


async def pixeldrain_expiry_loop():
    """Delete expired Pixeldrain files every 60s (survives restarts via DB)."""
    while True:
        try:
            minutes = int(await db.get_setting("px_delete_minutes"))
            expired = await db.pending_expired_uploads(minutes * 60)
            for row in expired:
                await delete_pixeldrain_file(row["file_id"])
        except Exception as e:
            logger.error("pixeldrain_expiry_loop error: %s", e)
        await asyncio.sleep(60)


async def temp_cleanup_loop():
    """Safety net: delete any temp file older than 2 hours."""
    tmp = Path(settings.TEMP_DIR)
    while True:
        try:
            if tmp.exists():
                cutoff = time.time() - 2 * 3600
                for p in tmp.rglob("*"):
                    try:
                        if p.is_file() and p.stat().st_mtime < cutoff:
                            p.unlink(missing_ok=True)
                        elif p.is_dir() and p.stat().st_mtime < cutoff and not any(p.iterdir()):
                            p.rmdir()
                    except Exception:
                        pass
        except Exception as e:
            logger.error("temp_cleanup_loop error: %s", e)
        await asyncio.sleep(600)
