"""Текст заказа и рассылка админам.

Вынесено из обработчика кнопки: заказ теперь приходит из двух мест — из бота и из
витрины Mini App, — а уведомление админу должно быть одно и то же.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import Order, OrderStatusHistory, status_ru
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


async def notify_admins_order_cancelled(bot: Bot, order: Order, source: str = "") -> None:
    """Покупатель отменил заказ сам — админам знать, чтобы не собирать его."""
    head = f"↩️ <b>Заказ #{order.id} отменён покупателем</b>"
    if source:
        head += f" ({esc(source)})"
    for admin_id in settings.admins:
        try:
            await bot.send_message(admin_id, head + "\n\n" + order_text(order, for_admin=True))
        except Exception as e:
            log.warning("Не смог уведомить админа %s: %s", admin_id, e)


def history_lines(entries: list[OrderStatusHistory]) -> list[str]:
    """История статусов для карточки — одна строка на запись, note только если есть."""
    lines = []
    for h in entries:
        when = h.created_at.strftime("%d.%m %H:%M")
        line = f"<code>{when}</code> {status_ru(h.status)}"
        if h.note:
            line += f" — {esc(h.note)}"
        lines.append(line)
    return lines


def status_message(order: Order, entry: OrderStatusHistory) -> str:
    """Что получает покупатель при смене статуса. Текст зависит и от статуса,
    и от способа получения: «готов к выдаче» без адреса — пустой звук."""
    head = f"Заказ #{order.id}: <b>{status_ru(entry.status)}</b>"
    st, method, note = str(entry.status), order.delivery_method, esc(entry.note)
    if st == "paid":
        body = "Оплата подтверждена, собираем заказ."
    elif st == "assembled":
        body = "Заказ собран, " + ("готовим к выдаче." if method == "pickup" else "скоро отправим.")
    elif st == "shipped":
        if method == "post":
            body = f"Отправлен почтой. Трек-номер: <code>{note}</code>"
        else:
            body = "Передан курьеру." + (f" Номер: <code>{note}</code>" if note else "")
    elif st == "ready_for_pickup":
        body = (
            f"Можно забирать!\n🏠 {esc(settings.pickup_address)}\n"
            f"🕓 {esc(settings.pickup_hours)}"
        )
    elif st == "delivered":
        body = "Спасибо за покупку!"
    elif st == "cancelled":
        body = f"Причина: {note}" if note else "Заказ отменён."
    else:
        body = ""
    return head + ("\n" + body if body else "")


async def notify_customer_status(
    bot: Bot, session: AsyncSession, order: Order, entry: OrderStatusHistory,
) -> bool:
    """Уведомить покупателя о переходе. False — не дошло, флаг записан в историю.

    Причины не различаем (заблокировал бота, удалил чат, сеть): действие админа
    одно и то же — связаться другим способом.
    TODO: повторной отправки нет. Сетевой сбой на секунду помечает запись как
    «не уведомлён» навсегда; если такое начнёт случаться, нужна очередь с backoff.
    """
    try:
        await bot.send_message(order.user_id, status_message(order, entry))
        return True
    except Exception as e:
        log.warning("Не смог уведомить покупателя %s о заказе #%s: %s", order.user_id, order.id, e)
        await repo.mark_not_notified(session, entry.id)
        return False
