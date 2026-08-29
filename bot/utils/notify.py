"""Текст заказа и рассылка админам.

Вынесено из обработчика кнопки: заказ теперь приходит из двух мест — из бота и из
витрины Mini App, — а уведомление админу должно быть одно и то же.
"""
from __future__ import annotations

import logging

from aiogram import Bot

from bot.config import settings
from bot.db.models import Order
from bot.keyboards.common import admin_order_kb
from bot.utils.money import fmt
from bot.utils.text import esc

log = logging.getLogger(__name__)


def order_text(order: Order, for_admin: bool = False) -> str:
    lines = [f"<b>Заказ #{order.id}</b>"]
    for i in order.items:
        lines.append(f"• {esc(i.title)} — {i.qty} × {fmt(i.price)}")
    lines.append(f"\n<b>Итого: {fmt(order.total)}</b>")
    lines.append(
        f"\n👤 {esc(order.contact_name)}\n📞 {esc(order.contact_phone)}\n🏠 {esc(order.address)}"
    )
    if order.comment:
        lines.append(f"💬 {esc(order.comment)}")
    if for_admin:
        lines.append(f"\ntg id: <code>{order.user_id}</code>")
    return "\n".join(lines)


async def notify_admins_new_order(bot: Bot, order: Order, source: str = "") -> None:
    """Шлёт заказ всем админам. Падение доставки одному не должно ронять заказ."""
    head = "🔔 <b>Новый заказ</b>"
    if source:
        head += f" ({esc(source)})"
    for admin_id in settings.admins:
        try:
            await bot.send_message(
                admin_id,
                head + "\n\n" + order_text(order, for_admin=True),
                reply_markup=admin_order_kb(order),
            )
        except Exception as e:  # админ мог не нажать /start у бота
            log.warning("Не смог уведомить админа %s: %s", admin_id, e)
