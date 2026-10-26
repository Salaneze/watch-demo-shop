"""Одно напоминание о брошенной корзине.

Без скидок и «осталось 2 часа»: человек просто отвлёкся, ему напоминают, что
корзина на месте. Давление и фальшивая срочность в маленьком магазине
окупаются отписками и блокировкой бота.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import utcnow
from bot.keyboards.common import cart_nudge_kb
from bot.utils.money import fmt
from bot.utils.text import esc

log = logging.getLogger(__name__)

# Позже суток напоминание уже не про «отвлёкся», а про спам по старой базе.
WINDOW_HOURS = 24
# Ночью не пишем: отложенное утром дойдёт само, окно в сутки это покрывает.
QUIET_FROM, QUIET_TO = 22, 9
SHOWN_LINES = 5


def is_quiet(now: datetime) -> bool:
    hour = now.astimezone(ZoneInfo(settings.shop_tz)).hour
    return hour >= QUIET_FROM or hour < QUIET_TO


async def nudge_text(session: AsyncSession, user_id: int) -> str | None:
    """Текст напоминания или None, если напоминать не о чем (всё снято с продажи)."""
    items = [i for i in await repo.cart_items(session, user_id) if i.product.is_active]
    if not items:
        return None
    lines = [f"• {esc(i.product.title)} × {i.qty}" for i in items[:SHOWN_LINES]]
    if len(items) > SHOWN_LINES:
        lines.append(f"…и ещё {len(items) - SHOWN_LINES}")
    total = sum(i.product.price * i.qty for i in items)
    return (
        "🛒 В корзине остались товары:\n"
        + "\n".join(lines)
        + f"\n\nИтого: <b>{fmt(total)}</b>\n"
        "Корзина сохранена — оформить можно, когда удобно."
    )


async def nudge_carts_once(bot: Bot, session: AsyncSession, now: datetime | None = None) -> int:
    """Разослать напоминания тем, кому пора. → сколько ушло."""
    if settings.cart_remind_minutes <= 0:
        return 0
    now = now or utcnow()
    if is_quiet(now):
        return 0
    cutoff = now - timedelta(minutes=settings.cart_remind_minutes)
    floor = now - timedelta(hours=WINDOW_HOURS)
    sent = 0
    for user_id in await repo.carts_to_nudge(session, cutoff, floor):
        text = await nudge_text(session, user_id)
        # Отметка до отправки, как у просьбы об отзыве: сбой сети съест одно
        # напоминание, а не превратит его в ежеминутное.
        if not await repo.mark_cart_nudged(session, user_id) or text is None:
            continue
        try:
            await bot.send_message(user_id, text, reply_markup=cart_nudge_kb())
            sent += 1
        except Exception as e:
            log.info("Напоминание о корзине %s не дошло: %s", user_id, e)
    return sent
