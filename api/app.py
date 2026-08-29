"""Веб-приложение витрины: API + статика + живой бот в том же процессе.

Один процесс вместо двух сервисов — клиенту разворачивать нечего, кроме одного
контейнера. Бот здесь на polling: webhook появится вместе с боевым доменом.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.limits import LimitsMiddleware
from api.routes import router
from bot.config import settings
from bot.db.base import init_db, session_factory
from bot.db.models import Order
from bot.handlers import setup_routers
from bot.middlewares.db import DbSessionMiddleware
from bot.utils.notify import notify_admins_new_order
from bot.utils.seed import seed_if_empty

log = logging.getLogger(__name__)

WEBAPP_DIR = Path("webapp")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with session_factory() as session:
        if await seed_if_empty(session):
            log.info("Каталог был пуст — залил демо-товары")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(setup_routers())

    me = await bot.get_me()
    log.info(
        "Витрина поднята: @%s, http://%s:%s, публичный адрес: %s",
        me.username, settings.web_host, settings.web_port,
        settings.webapp_url or "не задан (кнопки Mini App не будет)",
    )

    app.state.bot = bot
    # Нужен для ссылки входа t.me/<username>?start=... у клиентов без initData.
    app.state.bot_username = me.username

    async def notify(order: Order) -> None:
        await notify_admins_new_order(bot, order, source="витрина")

    app.state.notify_admins = notify

    await bot.delete_webhook(drop_pending_updates=True)
    polling = asyncio.create_task(dp.start_polling(bot))
    try:
        yield
    finally:
        polling.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await polling
        await bot.session.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Watch Demo Mini App", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(LimitsMiddleware)
    app.include_router(router)

    if WEBAPP_DIR.exists():
        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEBAPP_DIR / "index.html")

        app.mount("/", StaticFiles(directory=WEBAPP_DIR), name="webapp")

    return app


app = create_app()
