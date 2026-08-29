from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import settings
from bot.db.models import CartItem, Category, Order, OrderStatus, Product
from bot.keyboards.callbacks import AdminOrderCB, CartCB, CategoryCB, NavCB, ProductCB
from bot.utils.money import fmt


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = []
    # Витрина первой строкой — основной способ покупки. Кнопки ниже остаются
    # запасным путём: старые клиенты и десктопы без Mini App.
    if settings.has_webapp:
        rows.append([KeyboardButton(
            text="🛍 Открыть витрину",
            web_app=WebAppInfo(url=settings.public_url),
        )])
    rows += [
        [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="🛒 Корзина")],
        [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="ℹ️ О магазине")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="⚙️ Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def categories_kb(categories: list[Category]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in categories:
        kb.button(text=c.title, callback_data=CategoryCB(id=c.id))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="🛒 Корзина", callback_data=CartCB(action="open").pack()))
    return kb.as_markup()


def products_kb(products: list[Product], category_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(
            # Текст кнопки Telegram показывает как есть, без разметки —
            # экранировать не нужно, но длину ограничиваем, иначе клавиатура рвётся.
            text=f"{p.title[:40]} — {fmt(p.price)}",
            callback_data=ProductCB(id=p.id, category_id=category_id),
        )
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="⬅️ Категории", callback_data=NavCB(to="catalog").pack()),
        InlineKeyboardButton(text="🛒 Корзина", callback_data=CartCB(action="open").pack()),
    )
    return kb.as_markup()


def product_kb(product: Product) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ В корзину", callback_data=CartCB(action="add", product_id=product.id))
    kb.button(text="⬅️ Назад", callback_data=CategoryCB(id=product.category_id))
    kb.button(text="🛒 Корзина", callback_data=CartCB(action="open"))
    kb.adjust(1, 2)
    return kb.as_markup()


def cart_kb(items: list[CartItem]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i in items:
        kb.row(
            InlineKeyboardButton(
                text=f"➖ {i.product.title[:20]}",
                callback_data=CartCB(action="dec", product_id=i.product_id).pack(),
            ),
            InlineKeyboardButton(
                text=f"{i.qty} шт",
                callback_data="noop",
            ),
            InlineKeyboardButton(
                text="➕",
                callback_data=CartCB(action="inc", product_id=i.product_id).pack(),
            ),
        )
    if items:
        kb.row(InlineKeyboardButton(text="✅ Оформить заказ", callback_data=CartCB(action="checkout").pack()))
        kb.row(InlineKeyboardButton(text="🗑 Очистить", callback_data=CartCB(action="clear").pack()))
    kb.row(InlineKeyboardButton(text="🛍 В каталог", callback_data=NavCB(to="catalog").pack()))
    return kb.as_markup()


def confirm_order_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="checkout:confirm")
    kb.button(text="❌ Отмена", callback_data="checkout:cancel")
    kb.adjust(2)
    return kb.as_markup()


# Какие действия доступны из каждого статуса. Терминальные статусы
# (отправлен, отменён) кнопок не дают — иначе отменённый заказ можно
# одним кликом вернуть в оплаченные и отгрузить.
# Ключи — строки, а не члены OrderStatus: из БД статус приходит обычным str,
# а Enum.__hash__ считается по имени члена (та же грабля, что была со STATUS_RU).
ORDER_ACTIONS: dict[str, list[tuple[str, str]]] = {
    OrderStatus.new.value: [("✅ Оплачен", "paid"), ("❌ Отменить", "cancel")],
    OrderStatus.awaiting_payment.value: [("✅ Оплачен", "paid"), ("❌ Отменить", "cancel")],
    OrderStatus.paid.value: [("📦 Отправлен", "shipped"), ("❌ Отменить", "cancel")],
    OrderStatus.shipped.value: [],
    OrderStatus.cancelled.value: [],
}


def admin_order_kb(order: Order) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, action in ORDER_ACTIONS.get(str(order.status), []):
        kb.button(text=label, callback_data=AdminOrderCB(order_id=order.id, action=action))
    kb.adjust(2)
    return kb.as_markup()
