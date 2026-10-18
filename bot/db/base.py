from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import String, event, text
from sqlalchemy.exc import DataError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from bot.config import settings


def engine_args(url: str) -> tuple[str, dict]:
    """Строка подключения из панели хостинга → то, что примет asyncpg.

    Neon, Render и Supabase выдают `postgresql://...?sslmode=require`. asyncpg
    не знает ни схемы без драйвера, ни параметра sslmode и падает на старте
    «unexpected keyword argument», поэтому схему подменяем, а SSL переносим
    в connect_args. channel_binding из той же строки asyncpg тоже не понимает.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if not url.startswith("postgresql"):
        return url, {}

    parts = urlsplit(url)
    scheme = "postgresql+asyncpg"
    query = dict(parse_qsl(parts.query))
    sslmode = query.pop("sslmode", "")
    query.pop("channel_binding", None)
    url = urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    connect_args: dict = {}
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = True
    # Бесплатный Postgres засыпает через несколько минут простоя и рвёт
    # соединения в пуле. Без pre_ping первый покупатель после паузы получает 500.
    return url, {"connect_args": connect_args, "pool_pre_ping": True, "pool_recycle": 300}


_url, _kw = engine_args(settings.db_url)
engine = create_async_engine(_url, echo=False, **_kw)

if engine.dialect.name == "sqlite":
    @event.listens_for(engine.sync_engine, "connect")
    def _unicode_lower(dbapi_conn, _record) -> None:
        # Встроенный lower() в SQLite знает только латиницу, поэтому поиск
        # «ремешок» не находил «Ремешок», а «анна» — «Анну». Подменяем на
        # питоновский; в Postgres lower() юникодный и так.
        dbapi_conn.create_function("lower", 1, lambda s: s.lower() if isinstance(s, str) else s,
                                   deterministic=True)

session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def is_bad_value(exc: Exception) -> bool:
    """Запрос упал из-за значения, которое не влезло в тип колонки.

    Одна и та же ошибка приходит в двух обёртках: сервер Postgres даёт DataError,
    а asyncpg отбивает число вне int4 ещё до отправки — и SQLAlchemy заворачивает
    это в общий DBAPIError. Проверено на Neon (pg_check.py, шаг 7).
    """
    if isinstance(exc, DataError):
        return True
    if isinstance(exc, DBAPIError):
        cause = getattr(exc.orig, "__cause__", None)
        return type(cause).__name__ == "DataError"
    return False


def _fit_strings(_mapper, _conn, target) -> None:
    # SQLite хранит строку любой длины, Postgres на String(n) роняет весь запрос.
    # Режем по длине колонки здесь, а не в каждом обработчике: входов много
    # (чат, витрина, админка, ИИ), и забытый один уронил бы оформление заказа.
    for col in target.__table__.columns:
        if isinstance(col.type, String) and col.type.length:
            value = getattr(target, col.key, None)
            if isinstance(value, str) and len(value) > col.type.length:
                setattr(target, col.key, value[: col.type.length])


event.listen(Base, "before_insert", _fit_strings, propagate=True)
event.listen(Base, "before_update", _fit_strings, propagate=True)


async def init_db(eng=None) -> None:
    # v1: простое создание таблиц. Для прода — Alembic.
    from bot.db import models  # noqa: F401

    async with (eng or engine).begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Заказы, созданные до появления истории статусов, получают одну
        # стартовую запись. Идемпотентно, стоит миллисекунды — Alembic ради
        # одной таблицы противоречил бы решению выше.
        # TRUE, а не 1: Postgres не приводит целое к boolean, SQLite понимает оба.
        await conn.execute(text(
            "INSERT INTO order_status_history "
            "(order_id, status, actor, actor_id, note, notified, created_at) "
            "SELECT o.id, o.status, 'system', NULL, 'статус на момент миграции', TRUE, o.created_at "
            "FROM orders o WHERE NOT EXISTS "
            "(SELECT 1 FROM order_status_history h WHERE h.order_id = o.id)"
        ))
