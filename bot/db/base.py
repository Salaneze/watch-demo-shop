from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from bot.config import settings

engine = create_async_engine(settings.db_url, echo=False)
session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    # v1: простое создание таблиц. Для прода — Alembic.
    from bot.db import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Заказы, созданные до появления истории статусов, получают одну
        # стартовую запись. Идемпотентно, стоит миллисекунды — Alembic ради
        # одной таблицы противоречил бы решению выше.
        await conn.execute(text(
            "INSERT INTO order_status_history "
            "(order_id, status, actor, actor_id, note, notified, created_at) "
            "SELECT o.id, o.status, 'system', NULL, 'статус на момент миграции', 1, o.created_at "
            "FROM orders o WHERE NOT EXISTS "
            "(SELECT 1 FROM order_status_history h WHERE h.order_id = o.id)"
        ))
