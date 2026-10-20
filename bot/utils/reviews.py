"""Отзывы вокруг бота: сообщение админу о новом отзыве и отложенная просьба оценить.

Сама проверка «можно ли» — в repo.add_review; здесь только доставка сообщений.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import Review, utcnow
from bot.keyboards.common import admin_review_kb, delivered_kb
from bot.utils.text import esc

log = logging.getLogger(__name__)

# Подпись к фото в Telegram — до 1024 символов, текст отзыва — до 1000, плюс шапка.
CAPTION_TEXT_MAX = 800
# Дальше этого окна просьба уже неуместна: «как вам покупка двухмесячной давности?»
ASK_WINDOW_DAYS = 14


def stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def admin_review_text(review: Review, product_title: str) -> str:
    head = (
        f"⭐ <b>Отзыв</b> {stars(review.rating)}\n"
        f"«{esc(product_title)}», заказ #{review.order_id}, {esc(review.author)}"
    )
    if review.is_hidden:
        head += "\n<i>скрыт с витрины</i>"
    body = esc(review.text[:CAPTION_TEXT_MAX]) if review.text else "<i>без текста</i>"
    return head + "\n\n" + body


async def notify_admins_review(bot: Bot, session: AsyncSession, review: Review) -> None:
    """Каждый отзыв с текстом или фото — админам, с кнопкой «Скрыть».

    Публикуем сразу, без премодерации: отзыв привязан к выданному заказу, а
    очередь «на проверке» в маленьком магазине разбирают раз в неделю.
    Админ видит всё и прячет мусор одной кнопкой.
    """
    product = await repo.product(session, review.product_id)
    text = admin_review_text(review, product.title if product else f"товар #{review.product_id}")
    for admin_id in settings.admins:
        try:
            if review.photo_file_id:
                await bot.send_photo(admin_id, review.photo_file_id, caption=text,
                                     reply_markup=admin_review_kb(review))
            else:
                await bot.send_message(admin_id, text, reply_markup=admin_review_kb(review))
        except Exception as e:
            log.warning("Не смог показать отзыв #%s админу %s: %s", review.id, admin_id, e)


async def ask_reviews_once(bot: Bot, session: AsyncSession, now=None) -> int:
    """Одна просьба оценить на выданный заказ без отзыва. → сколько отправлено."""
    if settings.review_ask_days <= 0:
        return 0
    now = now or utcnow()
    cutoff = now - timedelta(days=settings.review_ask_days)
    floor = cutoff - timedelta(days=ASK_WINDOW_DAYS)
    sent = 0
    for order in await repo.orders_to_ask_review(session, cutoff, floor):
        # Сначала отметка, потом отправка: при сбое сети человек не получит
        # просьбу вовсе, а наоборот получал бы её каждую минуту до утра.
        if not await repo.mark_review_asked(session, order.id):
            continue
        try:
            await bot.send_message(
                order.user_id,
                f"Как вам заказ #{order.id}? Пара слов или фото в отзыве поможет "
                "другим покупателям выбрать — а нам понять, что улучшить.",
                reply_markup=delivered_kb(order),
            )
            sent += 1
        except Exception as e:
            log.info("Просьба об отзыве по заказу #%s не дошла: %s", order.id, e)
    return sent
