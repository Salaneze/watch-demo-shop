"""Фоновые дела магазина: автоотмена неоплаченных, просьбы об отзыве и
напоминание о брошенной корзине.

Один процесс, одна БД — планировщик поверх asyncio.sleep тут лишний.
Первая проверка идёт сразу при старте: на засыпающем хостинге сервер мог
пролежать дольше порога, и просроченные заказы догоняются при пробуждении,
а не ждут ещё один интервал.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from bot.config import settings
from bot.db.base import session_factory
from bot.utils.cart_nudge import nudge_carts_once
from bot.utils.lifecycle import expire_unpaid
from bot.utils.notify import notify_customer_status, sync_admin_keyboards
from bot.utils.reviews import ask_reviews_once

log = logging.getLogger(__name__)


async def sweep_once(bot: Bot) -> int:
    async with session_factory() as session:
        async def notify(order, entry) -> None:
            await notify_customer_status(bot, session, order, entry)
            await sync_admin_keyboards(bot, session, order)

        cancelled = await expire_unpaid(session, settings.unpaid_ttl_minutes, notify=notify)
    if cancelled:
        log.info("Автоотмена: %s", ", ".join(f"#{o.id}" for o in cancelled))
    return len(cancelled)


async def ask_once(bot: Bot) -> int:
    async with session_factory() as session:
        sent = await ask_reviews_once(bot, session)
    if sent:
        log.info("Просьб об отзыве отправлено: %s", sent)
    return sent


async def nudge_once(bot: Bot) -> int:
    async with session_factory() as session:
        sent = await nudge_carts_once(bot, session)
    if sent:
        log.info("Напоминаний о корзине отправлено: %s", sent)
    return sent


async def run_sweeper(bot: Bot) -> None:
    while True:
        try:
            await sweep_once(bot)
            await ask_once(bot)
            await nudge_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Одна упавшая итерация (БД занята, сеть) не должна убивать цикл до рестарта.
            log.exception("Фоновая проверка упала, повторю через %s с", settings.unpaid_sweep_seconds)
        await asyncio.sleep(settings.unpaid_sweep_seconds)


def start_sweeper(bot: Bot) -> asyncio.Task | None:
    """Поднять задачу или честно сказать в лог, что делать ей нечего."""
    if settings.unpaid_ttl_minutes <= 0:
        log.info("Автоотмена неоплаченных выключена (UNPAID_TTL_MINUTES=0)")
    else:
        log.info(
            "Автоотмена неоплаченных: порог %s мин, проверка каждые %s с",
            settings.unpaid_ttl_minutes, settings.unpaid_sweep_seconds,
        )
    if settings.review_ask_days <= 0:
        log.info("Напоминание об отзыве выключено (REVIEW_ASK_DAYS=0)")
    else:
        log.info("Напоминание об отзыве: через %s дн. после выдачи", settings.review_ask_days)
    if settings.cart_remind_minutes <= 0:
        log.info("Напоминание о корзине выключено (CART_REMIND_MINUTES=0)")
    else:
        log.info("Напоминание о корзине: через %s мин тишины", settings.cart_remind_minutes)
    if (settings.unpaid_ttl_minutes <= 0 and settings.review_ask_days <= 0
            and settings.cart_remind_minutes <= 0):
        return None
    return asyncio.create_task(run_sweeper(bot), name="unpaid-sweeper")
