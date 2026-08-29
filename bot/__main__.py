import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings
from bot.db.base import init_db, session_factory
from bot.handlers import setup_routers
from bot.middlewares.db import DbSessionMiddleware
from bot.utils.seed import seed_if_empty

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    await init_db()
    async with session_factory() as session:
        if await seed_if_empty(session):
            log.info("Каталог был пуст — залил демо-товары")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # v1: MemoryStorage. В проде — RedisStorage, иначе состояния теряются на рестарте.
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(setup_routers())

    me = await bot.get_me()
    log.info("Запускаю @%s (id=%s), админы: %s", me.username, me.id, settings.admins or "не заданы")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен")
