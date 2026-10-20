"""Отзыв из чата: товар → звёзды → текст или фото.

Звёзды сохраняются сразу по кнопке. Дальше человек может не написать ничего
— и оценка всё равно учтётся, а не потеряется вместе с брошенным диалогом.
"""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.keyboards.callbacks import ReviewCB
from bot.keyboards.common import review_pick_kb, review_skip_kb, review_stars_kb
from bot.states import ReviewForm
from bot.utils.reviews import notify_admins_review, stars
from bot.utils.text import esc

router = Router(name="reviews")


async def _ask_stars(call: CallbackQuery, order_id: int, item_title: str, product_id: int) -> None:
    await call.message.answer(
        f"Оцените «{esc(item_title)}»",
        reply_markup=review_stars_kb(order_id, product_id),
    )


@router.callback_query(ReviewCB.filter(F.action == "start"))
async def review_start(call: CallbackQuery, callback_data: ReviewCB, session: AsyncSession) -> None:
    try:
        items = await repo.reviewable_items(session, call.from_user.id, callback_data.order_id)
    except repo.ReviewDenied as e:
        await call.answer(str(e), show_alert=True)
        return
    if not items:
        await call.answer("Товаров из этого заказа больше нет в магазине", show_alert=True)
        return
    await call.answer()
    if len(items) == 1:
        await _ask_stars(call, callback_data.order_id, items[0].title, items[0].product_id)
        return
    rated = await repo.user_ratings(session, call.from_user.id)
    await call.message.answer(
        "Какой товар оценим?",
        reply_markup=review_pick_kb(callback_data.order_id, items, rated),
    )


@router.callback_query(ReviewCB.filter(F.action == "pick"))
async def review_pick(call: CallbackQuery, callback_data: ReviewCB, session: AsyncSession) -> None:
    try:
        items = await repo.reviewable_items(session, call.from_user.id, callback_data.order_id)
    except repo.ReviewDenied as e:
        await call.answer(str(e), show_alert=True)
        return
    item = next((i for i in items if i.product_id == callback_data.product_id), None)
    if item is None:
        await call.answer("Этого товара нет в заказе", show_alert=True)
        return
    await call.answer()
    await _ask_stars(call, callback_data.order_id, item.title, item.product_id)


@router.callback_query(ReviewCB.filter(F.action == "rate"))
async def review_rate(call: CallbackQuery, callback_data: ReviewCB, session: AsyncSession,
                      state: FSMContext) -> None:
    try:
        review = await repo.add_review(
            session, call.from_user.id, callback_data.order_id, callback_data.product_id,
            callback_data.rating, author=repo.public_name(call.from_user.full_name),
        )
    except repo.ReviewDenied as e:
        await call.answer(str(e), show_alert=True)
        return
    await state.set_state(ReviewForm.text)
    await state.update_data(order_id=callback_data.order_id, product_id=callback_data.product_id)
    await call.answer("Оценка сохранена")
    await call.message.edit_text(
        f"Оценка {stars(review.rating)} сохранена.\n\n"
        "Напишите пару слов о товаре или пришлите фото с подписью — "
        "это видят другие покупатели в карточке.",
        reply_markup=review_skip_kb(callback_data.order_id, callback_data.product_id),
    )


@router.callback_query(ReviewCB.filter(F.action == "skip"))
async def review_skip(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await call.message.edit_text("Спасибо за оценку!")


@router.message(ReviewForm.text)
async def review_text(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    photo = message.photo[-1].file_id if message.photo else None
    text = message.text or message.caption or ""
    if not text.strip() and not photo:
        await message.answer("Пришлите текст или фото. Передумали — /cancel, оценка останется.")
        return
    data = await state.get_data()
    await state.clear()
    try:
        review = await repo.add_review(
            session, message.from_user.id, data["order_id"], data["product_id"],
            # Оценку берём ту, что уже сохранена: этот шаг меняет только текст.
            (await _current_rating(session, message.from_user.id, data["product_id"])),
            text=text, author=repo.public_name(message.from_user.full_name),
            photo_file_id=photo,
        )
    except repo.ReviewDenied as e:
        await message.answer(f"Отзыв не сохранён: {esc(str(e))}")
        return
    await message.answer("Спасибо! Отзыв появился в карточке товара.")
    await notify_admins_review(bot, session, review)


async def _current_rating(session: AsyncSession, user_id: int, product_id: int) -> int:
    return (await repo.user_ratings(session, user_id)).get(product_id, 0)
