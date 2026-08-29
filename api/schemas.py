"""Что API отдаёт наружу.

Отдельные схемы, а не модели БД: наружу уходит только то, что нужно витрине.
Иначе любое новое поле в таблице (себестоимость, заметка админа) автоматически
уезжает покупателю.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class ProductOut(BaseModel):
    id: int
    title: str
    description: str
    price: int
    price_text: str
    image: str | None
    category_id: int


class CategoryOut(BaseModel):
    id: int
    title: str
    products: list[ProductOut]


class CatalogOut(BaseModel):
    currency: str
    categories: list[CategoryOut]


class CartLine(BaseModel):
    product_id: int
    title: str
    price: int
    qty: int
    sum: int
    sum_text: str
    image: str | None


class CartOut(BaseModel):
    lines: list[CartLine]
    total: int
    total_text: str
    # Названия товаров, которые сняли с продажи, пока они лежали в корзине.
    removed: list[str] = Field(default_factory=list)


class CartPatch(BaseModel):
    product_id: int
    delta: int

    @field_validator("delta")
    @classmethod
    def sane_delta(cls, v: int) -> int:
        # Защита от «добавить 10^9 штук» — total улетает в бессмыслицу.
        if not -99 <= v <= 99 or v == 0:
            raise ValueError("delta должна быть в пределах ±99 и не ноль")
        return v


# Минимум значащих символов после обрезки пробелов.
MIN_AFTER_STRIP = {"name": 2, "phone": 5, "address": 5}


class OrderIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=5, max_length=32)
    address: str = Field(min_length=5, max_length=300)
    comment: str = Field(default="", max_length=500)
    # Сумма, которую покупатель видел на экране оформления. Разошлась с реальной —
    # заказ не проходит: цена могла измениться, пока заполнялась форма.
    # Поле обязательное: будь оно необязательным, защиту снимал бы любой клиент,
    # просто не прислав его — включая старую версию витрины из кэша webview.
    expected_total: int = Field(ge=0)

    @field_validator("name", "phone", "address", "comment")
    @classmethod
    def strip_spaces(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def enough_after_strip(self) -> "OrderIn":
        # min_length меряет строку ДО обрезки, поэтому "   " и "  a  " проходят
        # ограничение поля, а админ получает заказ без имени.
        for field, minimum in MIN_AFTER_STRIP.items():
            if len(getattr(self, field)) < minimum:
                raise ValueError(f"{field}: нужно минимум {minimum} символов")
        return self


class OrderItemOut(BaseModel):
    title: str
    price: int
    qty: int


class OrderOut(BaseModel):
    id: int
    status: str
    status_text: str
    total: int
    total_text: str
    created_at: str
    items: list[OrderItemOut]


class LoginStart(BaseModel):
    """Одноразовый код и ссылка на бота для клиентов без initData."""
    code: str
    link: str


class LoginPoll(BaseModel):
    ready: bool
    token: str | None
