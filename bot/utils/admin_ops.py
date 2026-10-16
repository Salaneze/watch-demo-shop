"""Действия админа над заказом, общие для кнопок бота и веб-панели.

Сам переход — apply_transition(); здесь то, что вокруг него обязано
случиться при любом входе: запись в журнал действий и уведомление
покупателя. Держать это в хендлере бота значило бы, что панель однажды
сменит статус молча и без следа в /audit.
"""
from __future__ import annotations

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Order, OrderStatusHistory
from bot.utils.lifecycle import apply_transition
from bot.utils.notify import notify_customer_status


async def admin_transition(
    session: AsyncSession, bot: Bot | None, admin_id: int,
    order_id: int, from_status: str, to_status: str, note: str = "",
    source: str = "",
) -> tuple[Order, OrderStatusHistory, bool]:
    """→ (заказ, запись истории, дошло ли уведомление покупателю).

    InvalidTransition и ValueError (кривой трек/причина) летят наружу —
    текст ошибки вызывающий показывает админу как есть.
    """
    order, entry = await apply_transition(
        session, order_id, from_status, to_status, actor="admin", actor_id=admin_id, note=note,
    )
    details = f"{from_status} -> {to_status}" + (f" ({source})" if source else "")
    await repo.log_action(session, admin_id, "order_status", f"order#{order.id}", details)
    notified = False
    if bot is not None:
        notified = await notify_customer_status(bot, session, order, entry)
    else:
        await repo.mark_not_notified(session, entry.id)
    return order, entry, notified
