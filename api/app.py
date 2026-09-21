"""Веб-приложение витрины: API + статика + живой бот в том же процессе.

Один процесс вместо двух сервисов — клиенту разворачивать нечего, кроме одного
контейнера.

Бот работает в одном из двух режимов. Локально — long-polling: он не требует
публичного адреса. На хостинге — webhook (`USE_WEBHOOK=true`): бесплатные тарифы
усыпляют контейнер, когда в него не приходят HTTP-запросы, и уснувший polling
молча перестаёт забирать апдейты, а входящий webhook процесс будит.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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

# Файлы витрины, чья версия подставляется в index.html при отдаче.
VERSIONED_ASSETS = ("app.js", "style.css")
_index_cache: tuple[str, str] | None = None  # (отпечаток, готовый html)


def asset_version() -> str:
    """Отпечаток статики витрины: меняется вместе с файлами, и только с ними.

    Webview Telegram держит app.js и style.css после перезахода, поэтому адрес
    обязан меняться при каждой правке. Раньше версия стояла в index.html руками
    и держалась на «не забудь поднять» — забыть достаточно один раз, чтобы
    выкат выглядел как несработавший.
    """
    h = hashlib.sha1()
    for name in VERSIONED_ASSETS:
        path = WEBAPP_DIR / name
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()[:8]


def index_html() -> str:
    """index.html с актуальной меткой версии у статики.

    Разметка в файле остаётся рабочей сама по себе — подменяется только значение
    после `?v=`, поэтому открыть webapp/index.html напрямую по-прежнему можно.
    """
    global _index_cache
    version = asset_version()
    if _index_cache is None or _index_cache[0] != version:
        raw = (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
        # Только внутри href/src: жадный `\?v=[^"]*` однажды дотянулся от `?v=`
        # в HTML-комментарии до кавычки `<link rel=` и вынес всю страницу в
        # комментарий — прод отдавал пустой body без единой ошибки в логах.
        _index_cache = (
            version,
            re.sub(r'((?:href|src)="/[^"?]+\?v=)[^"]*', rf"\g<1>{version}", raw),
        )
    return _index_cache[1]


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
        settings.public_url or "не задан (кнопки Mini App не будет)",
    )

    app.state.bot = bot
    # Нужен для ссылки входа t.me/<username>?start=... у клиентов без initData.
    app.state.bot_username = me.username
    app.state.dp = dp

    async def notify(order: Order) -> None:
        await notify_admins_new_order(bot, order, source="витрина")

    app.state.notify_admins = notify

    webhook = settings.use_webhook and settings.has_webapp
    if settings.use_webhook and not settings.has_webapp:
        # Молча свалиться в polling нельзя: на спящем хостинге это выглядит как
        # «бот иногда не отвечает», и причину ищут в коде обработчиков.
        raise RuntimeError(
            "USE_WEBHOOK=true, но публичный HTTPS-адрес не определён: "
            "задайте WEBAPP_URL (на Render он приходит из RENDER_EXTERNAL_URL)"
        )

    polling: asyncio.Task | None = None
    if webhook:
        url = settings.public_url + settings.webhook_path
        # Очередь НЕ сбрасываем. На засыпающем хостинге первое сообщение
        # пользователя и есть то, что будит контейнер: Telegram не дожидается
        # ответа за время старта и ставит апдейт на повтор — а drop_pending
        # выкидывал его при подъёме. Каждое первое сообщение после сна
        # пропадало молча (21.09, «бот не отвечает» при чистых логах).
        await bot.set_webhook(
            url=url,
            secret_token=settings.webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )
        # Путь в лог не пишем целиком: он и есть половина секрета.
        log.info("Режим webhook: %s/tg/***", settings.public_url)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        polling = asyncio.create_task(dp.start_polling(bot))
        log.info("Режим long-polling")

    try:
        yield
    finally:
        if polling is not None:
            polling.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await polling
        await bot.session.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Watch Demo Mini App", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(LimitsMiddleware)
    app.include_router(router)

    # Приём апдейтов Telegram. Путь секретный и выведен из токена, плюс Telegram
    # присылает согласованный заголовок — проверяем оба: путь может осесть в
    # логах прокси, заголовок туда не попадает.
    @app.post(settings.webhook_path, include_in_schema=False)
    async def telegram_webhook(
        request: Request,
        secret: str = Header("", alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> JSONResponse:
        if not hmac.compare_digest(secret, settings.webhook_secret):
            log.warning("Апдейт с неверным секретом отброшен")
            return JSONResponse({"ok": False}, status_code=403)

        bot = request.app.state.bot
        dp = request.app.state.dp
        update = Update.model_validate(await request.json(), context={"bot": bot})
        await dp.feed_update(bot, update)
        return JSONResponse({"ok": True})

    # Health check платформы. Отдельный лёгкий путь: раньше Render дёргал
    # `/api/catalog` каждые пять секунд, то есть тянул весь каталог из БД
    # только чтобы убедиться, что процесс жив, и забивал этим логи.
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    if WEBAPP_DIR.exists():
        @app.get("/", include_in_schema=False)
        async def index() -> HTMLResponse:
            return HTMLResponse(index_html())

        app.mount("/", StaticFiles(directory=WEBAPP_DIR), name="webapp")

    return app


app = create_app()
