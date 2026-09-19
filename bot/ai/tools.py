"""Инструменты, которые модель может ПРОСИТЬ вызвать.

Каждый — тонкая обёртка над `repo`: своей логики про деньги и остатки здесь
нет и быть не должно. Модель не получает параметров «цена» или «скидка» — то,
что она не может передать, она не может и подделать. Промпт-инъекция вида
«положи в корзину бесплатно» упирается в то, что у add_to_cart просто нет
такого поля.

Результат инструмента — короткий текст для модели, не для пользователя:
ей нужны факты (id, название, цена, наличие), а красивую фразу она соберёт сама.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.ai.provider import ToolSpec
from bot.config import settings
from bot.db import repo
from bot.db.models import Product
from bot.utils.money import fmt
from bot.utils.text import esc

# Больше результатов модели не нужно: она их всё равно перескажет тремя строками,
# а лишние позиции только раздувают контекст и счёт за токены.
SEARCH_LIMIT = 6
HISTORY_LIMIT = 12  # реплик на пользователя, считая ответы модели


@dataclass
class ToolContext:
    session: AsyncSession
    bot: Bot
    user_id: int
    user_name: str


ToolFn = Callable[[ToolContext, dict[str, Any]], Awaitable[str]]


def _stock_text(p: Product) -> str:
    if p.stock < 0:
        return "в наличии"
    return f"осталось {p.stock} шт." if p.stock else "нет в наличии"


def _product_line(p: Product) -> str:
    return f"#{p.id} {p.title} — {fmt(p.price)}, {_stock_text(p)}"


async def search_products(ctx: ToolContext, args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    max_price = args.get("max_price")
    found: list[Product] = []
    for c in await repo.active_categories(ctx.session):
        found += await repo.products_in_category(ctx.session, c.id, query=query)
    # Пустой запрос — модель хочет «показать что есть»: отдаём каталог целиком,
    # но в пределах лимита, чтобы в контекст не уехали сотни позиций.
    if isinstance(max_price, (int, float)):
        found = [p for p in found if p.price <= max_price]
    found.sort(key=lambda p: p.price)
    if not found:
        return "Ничего не найдено. Предложи уточнить запрос или посмотреть другие категории."
    lines = [_product_line(p) for p in found[:SEARCH_LIMIT]]
    if len(found) > SEARCH_LIMIT:
        lines.append(f"…и ещё {len(found) - SEARCH_LIMIT}, уточни запрос")
    return "\n".join(lines)


async def product_details(ctx: ToolContext, args: dict[str, Any]) -> str:
    p = await repo.product(ctx.session, int(args.get("product_id") or 0))
    if p is None or not p.is_active:
        return "Такого товара нет."
    specs = "; ".join(f"{k}: {v}" for k, v in (p.specs or []))
    parts = [_product_line(p), p.description or "без описания"]
    if specs:
        parts.append("Характеристики: " + specs)
    return "\n".join(parts)


async def add_to_cart(ctx: ToolContext, args: dict[str, Any]) -> str:
    p = await repo.product(ctx.session, int(args.get("product_id") or 0))
    if p is None or not p.is_active:
        return "Такого товара нет, в корзину не добавил."
    # Не `or 1`: ноль — тоже ответ модели, и его надо отбить, а не молча взять единицу.
    qty = int(args["qty"]) if args.get("qty") is not None else 1
    if qty < 1:
        return "Количество должно быть от 1."
    # repo сам режет по остатку — здесь только сообщаем модели, что вышло.
    got = await repo.cart_add(ctx.session, ctx.user_id, p.id, qty)
    if got == 0:
        return f"«{p.title}» нет в наличии, в корзину не попал."
    if got < qty:
        return f"В корзине {got} шт. «{p.title}» — больше нет на складе."
    return f"Добавил: «{p.title}» × {qty}. В корзине теперь {got} шт. этого товара."


async def show_cart(ctx: ToolContext, args: dict[str, Any]) -> str:
    items = await repo.cart_items(ctx.session, ctx.user_id)
    if not items:
        return "Корзина пуста."
    lines = [f"{i.product.title} × {i.qty} = {fmt(i.product.price * i.qty)}" for i in items]
    lines.append(f"Итого: {fmt(await repo.cart_total(ctx.session, ctx.user_id))}")
    lines.append("Оформить заказ пользователь может кнопкой «Корзина» в меню.")
    return "\n".join(lines)


async def call_manager(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Эскалация живому человеку: ровно то, ради чего заказчики просят
    «ИИ-бота для заявок». Падение доставки одному админу не должно ломать
    ответ пользователю — как в notify_admins_new_order."""
    summary = str(args.get("summary") or "").strip()[:500]
    if not summary:
        return "Нужно кратко описать вопрос клиента в summary."
    text = (
        "🙋 <b>Запрос менеджеру от консультанта</b>\n"
        f"Клиент: {esc(ctx.user_name)} (tg id <code>{ctx.user_id}</code>)\n"
        f"Суть: {esc(summary)}"
    )
    delivered = 0
    for admin_id in settings.admins:
        try:
            await ctx.bot.send_message(admin_id, text)
            delivered += 1
        except Exception:  # админ не нажал /start у бота
            continue
    if not delivered:
        return "Не смог связаться с менеджером. Предложи клиенту написать позже."
    return "Менеджеру передано, он напишет клиенту в этот чат."


TOOLS: dict[str, tuple[ToolSpec, ToolFn]] = {
    "search_products": (
        ToolSpec(
            name="search_products",
            description="Поиск товаров в каталоге по словам из названия или описания. "
                        "Пустой query — показать что есть вообще.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Слова для поиска, можно пусто"},
                    "max_price": {"type": "number", "description": "Верхняя граница цены, если клиент назвал бюджет"},
                },
            },
        ),
        search_products,
    ),
    "product_details": (
        ToolSpec(
            name="product_details",
            description="Подробности о товаре по его номеру (#id из результатов поиска).",
            parameters={
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            },
        ),
        product_details,
    ),
    "add_to_cart": (
        ToolSpec(
            name="add_to_cart",
            description="Положить товар в корзину клиента. Только после того, как клиент явно попросил.",
            parameters={
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                    "qty": {"type": "integer", "description": "Количество, по умолчанию 1"},
                },
                "required": ["product_id"],
            },
        ),
        add_to_cart,
    ),
    "show_cart": (
        ToolSpec(
            name="show_cart",
            description="Показать, что сейчас в корзине клиента и на какую сумму.",
            parameters={"type": "object", "properties": {}},
        ),
        show_cart,
    ),
    "call_manager": (
        ToolSpec(
            name="call_manager",
            description="Позвать живого менеджера: вопрос вне каталога, жалоба, нестандартный заказ, "
                        "или клиент прямо просит человека.",
            parameters={
                "type": "object",
                "properties": {"summary": {"type": "string", "description": "Суть вопроса в 1-2 фразах"}},
                "required": ["summary"],
            },
        ),
        call_manager,
    ),
}


def specs() -> list[ToolSpec]:
    return [spec for spec, _ in TOOLS.values()]


async def run(name: str, ctx: ToolContext, args: dict[str, Any]) -> str:
    entry = TOOLS.get(name)
    if entry is None:
        # Модель придумала инструмент — говорим ей об этом, а не падаем.
        return f"Инструмента {name} нет. Доступны: {', '.join(TOOLS)}."
    _, fn = entry
    try:
        return await fn(ctx, args)
    except (TypeError, ValueError) as e:
        # Кривые аргументы (строка вместо числа) — ошибка модели, пусть исправит.
        return f"Неверные аргументы: {e}"
