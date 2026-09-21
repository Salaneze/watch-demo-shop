"""Фоновая автоотмена неоплаченных заказов.

Один процесс, одна БД — планировщик поверх asyncio.sleep тут лишний.
Первая проверка идёт сразу при старте: на засыпающем хостинге сервер мог
пролежать дольше порога, и просроченные заказы догоняются при пробуждении,
а не ждут ещё один интервал.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial

from aiogram import Bot

from bot.config import settings
from bot.db.base import session_factory
from bot.utils.lifecycle import expire_unpaid
from bot.utils.notify import notify_customer_status

log = logging.getLogger(__name__)


async def sweep_once(bot: Bot) -> int:
    async with session_factory() as session:
        cancelled = await expire_unpaid(
            session, settings.unpaid_ttl_minutes,
            notify=partial(notify_customer_status, bot, session),
        )
    if cancelled:
        log.info("Автоотмена: %s", ", ".join(f"#{o.id}" for o in cancelled))
    return len(cancelled)


async def run_sweeper(bot: Bot) -> None:
    while True:
        try:
            await sweep_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Одна упавшая итерация (БД занята, сеть) не должна убивать цикл до рестарта.
            log.exception("Автоотмена упала, повторю через %s с", settings.unpaid_sweep_seconds)
        await asyncio.sleep(settings.unpaid_sweep_seconds)


def start_sweeper(bot: Bot) -> asyncio.Task | None:
    """Поднять задачу или честно сказать в лог, что автоотмена выключена."""
    if settings.unpaid_ttl_minutes <= 0:
        log.info("Автоотмена неоплаченных выключена (UNPAID_TTL_MINUTES=0)")
        return None
    log.info(
        "Автоотмена неоплаченных: порог %s мин, проверка каждые %s с",
        settings.unpaid_ttl_minutes, settings.unpaid_sweep_seconds,
    )
    return asyncio.create_task(run_sweeper(bot), name="unpaid-sweeper")
