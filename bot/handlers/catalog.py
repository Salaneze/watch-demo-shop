from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Product
from bot.keyboards.callbacks import CategoryCB, NavCB, ProductCB
from bot.keyboards.common import categories_kb, product_kb, products_kb
from bot.utils.money import fmt
from bot.utils.seed import seed_art_path
from bot.utils.text import esc

router = Router(name="catalog")


async def show_categories(target: Message | CallbackQuery, session: AsyncSession) -> None:
    categories = await repo.active_categories(session)
    if not categories:
        text = "Каталог пока пуст."
        if isinstance(target, Message):
            await target.answer(text)
        else:
            await target.message.answer(text)
        return

    text = "🛍 <b>Каталог</b>\nВыбери категорию:"
    kb = categories_kb(categories)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb)
    else:
        # У карточки товара может быть фото — тогда редактировать текст нельзя.
        await target.message.answer(text, reply_markup=kb)


@router.message(F.text == "🛍 Каталог")
async def catalog_cmd(message: Message, session: AsyncSession) -> None:
    await show_categories(message, session)


@router.callback_query(NavCB.filter(F.to == "catalog"))
async def catalog_nav(call: CallbackQuery, session: AsyncSession) -> None:
    await call.answer()
    await show_categories(call, session)


@router.callback_query(CategoryCB.filter())
async def open_category(call: CallbackQuery, callback_data: CategoryCB, session: AsyncSession) -> None:
    await call.answer()
    cat = await repo.category(session, callback_data.id)
    if cat is None:
        await call.message.answer("Категория не найдена.")
        return

    products = await repo.products_in_category(session, cat.id)
    if not products:
        await call.message.answer(f"В категории «{esc(cat.title)}» пока нет товаров.")
        return

    await call.message.answer(
        f"📂 <b>{esc(cat.title)}</b>\nВыбери товар:",
        reply_markup=products_kb(products, cat.id),
    )


# Подпись у фото ограничена 1024 символами, и превышение — не обрезка на стороне
# Telegram, а отказ отправить сообщение. Характеристики ограничиваем по числу
# строк, описание — по длине.
CAPTION_LIMIT = 1024
MAX_SPECS = 8


def product_caption(p: Product) -> str:
    """Карточка товара для чата: описание, характеристики, остаток, цена.

    Держится вровень с витриной Mini App — до этого характеристики и остаток
    были только в ней, и покупатель из чата выбирал товар вслепую.
    """
    head = f"<b>{esc(p.title)}</b>"

    # specs лежат в JSON парами [название, значение]. Формат задаёт админ, и
    # кривая строка не повод не показать карточку — пропускаем такие молча.
    rows = [r for r in (p.specs or []) if isinstance(r, (list, tuple)) and len(r) == 2]
    tail = [""] + [f"• {esc(k)}: {esc(v)}" for k, v in rows[:MAX_SPECS]] if rows else []
    tail += ["", f"💰 <b>{fmt(p.price)}</b>"]
    if p.stock == 0:
        tail.append("❌ Нет в наличии")
    elif p.stock > 0:
        tail.append(f"📦 Осталось {p.stock} шт.")

    # Режем сырое описание, а не собранную подпись: обрезка по готовой строке
    # рассекает тег или сущность вроде &amp;, и Telegram отклоняет всё сообщение.
    fixed = len(head) + len("\n\n") + len("\n".join(tail))
    budget = CAPTION_LIMIT - fixed
    desc = esc(p.description)
    if len(desc) > budget:
        raw = p.description[: max(budget, 0)]
        while raw and len(esc(raw)) > budget - 1:
            raw = raw[:-16]
        desc = esc(raw) + "…"

    return "\n".join([head, "", desc] + tail)


@router.callback_query(ProductCB.filter())
async def open_product(call: CallbackQuery, callback_data: ProductCB, session: AsyncSession) -> None:
    await call.answer()
    p = await repo.product(session, callback_data.id)
    if p is None or not p.is_active:
        await call.message.answer("Товар недоступен.")
        return

    caption = product_caption(p)
    kb = product_kb(p)
    # У демо-товара картинка лежит файлом в репозитории, у настоящего — в
    # Telegram. file_id используем как есть: заливать одно и то же повторно
    # незачем, а файл с диска поднимаем только для демо.
    art = seed_art_path(p.photo_file_id)
    photo = FSInputFile(art) if art is not None else p.photo_file_id
    if photo:
        await call.message.answer_photo(photo, caption=caption, reply_markup=kb)
    else:
        await call.message.answer(caption, reply_markup=kb)
