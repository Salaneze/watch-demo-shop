from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
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


@router.callback_query(ProductCB.filter())
async def open_product(call: CallbackQuery, callback_data: ProductCB, session: AsyncSession) -> None:
    await call.answer()
    p = await repo.product(session, callback_data.id)
    if p is None or not p.is_active:
        await call.message.answer("Товар недоступен.")
        return

    caption = (
        f"<b>{esc(p.title)}</b>\n\n"
        f"{esc(p.description)}\n\n"
        f"💰 <b>{fmt(p.price)}</b>"
    )
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
