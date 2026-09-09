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
from bot.utils.delivery import delivery_option
from bot.utils.money import fmt
from bot.utils.text import esc

log = logging.getLogger(__name__)


def totals_lines(
    items_total: int,
    discount: int,
    promo_code: str,
    delivery_title: str,
    delivery_cost: int,
    total: int,
) -> list[str]:
    """Разбивка суммы: товары, скидка, доставка, итог.

    Одна на превью в чате, уведомление админу и карточку заказа — иначе покупатель
    видит одни строки, а админ другие. Промокод и название способа идут через esc():
    код набирает человек, и первая же угловая скобка в нём уронит отправку
    (parse_mode=HTML), то есть админ просто не узнает о заказе.

    Скидка и платная доставка показываются только когда они есть: «Скидка: 0» в
    каждом заказе — шум, который перестают читать.
    """
    lines = [f"Товары: {fmt(items_total)}"]
    if discount:
        label = f"Скидка ({esc(promo_code)})" if promo_code else "Скидка"
        lines.append(f"{label}: -{fmt(discount)}")
    cost = "бесплатно" if delivery_cost == 0 else fmt(delivery_cost)
    lines.append(f"Доставка ({esc(delivery_title)}): {cost}")
    lines.append(f"<b>К оплате: {fmt(total)}</b>")
    return lines


def order_delivery_title(order: Order) -> str:
    """Человеческое название способа доставки. Заказ мог быть оформлен на коде,
    который из настроек уже убрали, — показываем сам код, а не падаем."""
    try:
        return delivery_option(order.delivery_method).title
    except ValueError:
        return order.delivery_method


def order_text(order: Order, for_admin: bool = False) -> str:
    lines = [f"<b>Заказ #{order.id}</b>"]
    for i in order.items:
        lines.append(f"• {esc(i.title)} — {i.qty} × {fmt(i.price)}")
    lines.append("")
    # У заказов, созданных до появления доставки, items_total нулевой — за сумму
    # товаров берём итог, иначе старая запись показывает «Товары: 0».
    lines += totals_lines(
        items_total=order.items_total or order.total,
        discount=order.discount,
        promo_code=order.promo_code,
        delivery_title=order_delivery_title(order),
        delivery_cost=order.delivery_cost,
        total=order.total,
    )
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
