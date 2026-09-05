"""Способы доставки и промокоды — всё, что меняет сумму заказа.

Ключевое правило модуля: цену доставки и размер скидки считает сервер. Клиент
присылает только выбранный способ и текст промокода. Пришли он ещё и цену —
достаточно было бы подменить её в запросе, чтобы получить курьера за ноль
(этой ровно граблей мы уже касались в `expected_total`).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Promo


@dataclass(frozen=True)
class DeliveryOption:
    code: str
    title: str
    note: str
    cost: int


# Порядок важен: в таком виде варианты и показываются на экране оформления.
# Цены берутся из настроек, а не зашиты здесь: они зависят от валюты магазина.
DELIVERY_OPTIONS: tuple[DeliveryOption, ...] = (
    DeliveryOption("pickup", "Самовывоз", "из магазина, в день заказа", 0),
    DeliveryOption("courier", "Курьер", "по городу, на следующий день",
                   settings.delivery_courier),
    DeliveryOption("post", "Почта", "по стране, 3–7 дней", settings.delivery_post),
)

DEFAULT_DELIVERY = DELIVERY_OPTIONS[0].code

_BY_CODE = {o.code: o for o in DELIVERY_OPTIONS}


def delivery_option(code: str) -> DeliveryOption:
    """Вариант доставки по коду. Неизвестный код — ошибка, а не тихий самовывоз.

    Молчаливый откат на бесплатный вариант выглядит безобидно ровно до первого
    заказа, где клиент ждёт курьера, а магазин видит «самовывоз».
    """
    option = _BY_CODE.get(code)
    if option is None:
        raise ValueError(f"неизвестный способ доставки: {code!r}")
    return option


async def resolve_promo(session: AsyncSession, code: str) -> Promo | None:
    """Живой промокод по тексту, введённому покупателем, или None.

    Регистр и пробелы не важны: человек набирает код с телефона, а не копирует.
    """
    code = code.strip().upper()
    if not code:
        return None
    promo = (await session.execute(select(Promo).where(Promo.code == code))).scalar_one_or_none()
    if promo is None or not promo.is_active:
        return None
    if promo.max_uses >= 0 and promo.used >= promo.max_uses:
        return None
    return promo


def discount_for(items_total: int, promo: Promo | None) -> int:
    """Скидка в деньгах. Округление вниз — в пользу магазина, не покупателя."""
    if promo is None:
        return 0
    return items_total * promo.percent // 100
