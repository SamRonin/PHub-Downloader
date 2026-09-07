import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .config import settings
from .db import db
from .handlers import start, info, download, admin
from .utils.cleanup import pixeldrain_expiry_loop, temp_cleanup_loop


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    os.makedirs(settings.TEMP_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)

    await db.init()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # order matters: commands first, then link/text, then admin FSM handlers
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(download.router)
    dp.include_router(info.router)

    asyncio.create_task(pixeldrain_expiry_loop())
    asyncio.create_task(temp_cleanup_loop())

    logging.info("PH Downloader bot starting (admins: %s)", settings.ADMIN_IDS)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
