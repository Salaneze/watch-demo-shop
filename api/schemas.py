"""Что API отдаёт наружу.

Отдельные схемы, а не модели БД: наружу уходит только то, что нужно витрине.
Иначе любое новое поле в таблице (себестоимость, заметка админа) автоматически
уезжает покупателю.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from bot.utils.delivery import DEFAULT_DELIVERY, DELIVERY_OPTIONS


class ProductOut(BaseModel):
    id: int
    title: str
    description: str
    price: int
    price_text: str
    image: str | None
    # Все кадры галереи, включая первый. Пустой список — картинок нет вовсе.
    images: list[str]
    category_id: int
    # -1 = продаём без учёта остатка, тогда витрина про наличие ничего не пишет.
    stock: int
    specs: list[list[str]]
    is_favorite: bool = False


class CategoryOut(BaseModel):
    id: int
    title: str
    products: list[ProductOut]


class CatalogOut(BaseModel):
    currency: str
    categories: list[CategoryOut]


class DeliveryOut(BaseModel):
    code: str
    title: str
    note: str
    cost: int
    cost_text: str


class PromoOut(BaseModel):
    """Проверка промокода до оформления, чтобы сумму человек видел заранее."""

    valid: bool
    code: str
    percent: int
    discount: int
    discount_text: str
    message: str


class FavoriteOut(BaseModel):
    product_id: int
    is_favorite: bool


class FavoritePatch(BaseModel):
    product_id: int


class PromoIn(BaseModel):
    code: str = Field(max_length=32)


class AiStatus(BaseModel):
    enabled: bool


class AiIn(BaseModel):
    # Потолок по длине — не про UX, а про счёт: каждый символ уходит модели,
    # и без лимита один клиент может слать в неё по абзацу текста.
    message: str = Field(min_length=1, max_length=500)

    @field_validator("message")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Пустое сообщение")
        return v


class AiOut(BaseModel):
    reply: str
    # Корзина в ответе всегда: модель могла положить туда товар, и витрине
    # дешевле обновить бейдж из этого же ответа, чем делать второй запрос.
    cart: CartOut


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
    # Только КОД способа доставки и ТЕКСТ промокода. Цену доставки и размер
    # скидки считает сервер: пришли их клиент — курьер стоил бы столько,
    # сколько покупатель захочет.
    delivery: str = Field(default=DEFAULT_DELIVERY, max_length=32)
    promo_code: str = Field(default="", max_length=32)

    @field_validator("delivery")
    @classmethod
    def known_delivery(cls, v: str) -> str:
        if v not in {o.code for o in DELIVERY_OPTIONS}:
            raise ValueError("неизвестный способ доставки")
        return v

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
    items_total: int
    items_total_text: str
    discount: int
    discount_text: str
    promo_code: str
    delivery_title: str
    delivery_cost: int
    delivery_cost_text: str
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
