from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.keyboards.callbacks import CartCB
from bot.keyboards.common import cart_kb
from bot.states import Checkout
from bot.utils.money import fmt
from bot.utils.text import esc

router = Router(name="cart")


async def render_cart(session: AsyncSession, user_id: int) -> tuple[str, object]:
    # Товар мог быть снят с продажи, пока корзина лежала — убираем и говорим об этом,
    # иначе позиция просто молча исчезнет и покупатель решит, что бот съел заказ.
    removed = await repo.drop_inactive_from_cart(session, user_id)
    notice = ""
    if removed:
        names = ", ".join(esc(t) for t in removed)
        notice = f"⚠️ Снято с продажи и убрано из корзины: {names}\n\n"

    items = await repo.cart_items(session, user_id)
    if not items:
        return notice + "🛒 Корзина пуста.", cart_kb([])

    lines = [notice + "🛒 <b>Твоя корзина</b>\n"]
    total = 0
    for i in items:
        line_sum = i.product.price * i.qty
        total += line_sum
        lines.append(
            f"• {esc(i.product.title)} — {i.qty} × {fmt(i.product.price)} = <b>{fmt(line_sum)}</b>"
        )
    lines.append(f"\n<b>Итого: {fmt(total)}</b>")
    return "\n".join(lines), cart_kb(items)


@router.message(F.text == "🛒 Корзина")
async def cart_cmd(message: Message, session: AsyncSession) -> None:
    text, kb = await render_cart(session, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CartCB.filter(F.action == "add"))
async def cart_add(call: CallbackQuery, callback_data: CartCB, session: AsyncSession) -> None:
    p = await repo.product(session, callback_data.product_id)
    if p is None or not p.is_active:
        await call.answer("Товар недоступен", show_alert=True)
        return
    qty = await repo.cart_add(session, call.from_user.id, p.id, 1)
    await call.answer(f"Добавлено. В корзине: {qty} шт.")


@router.callback_query(CartCB.filter(F.action.in_({"inc", "dec"})))
async def cart_change(call: CallbackQuery, callback_data: CartCB, session: AsyncSession) -> None:
    delta = 1 if callback_data.action == "inc" else -1
    await repo.cart_add(session, call.from_user.id, callback_data.product_id, delta)
    text, kb = await render_cart(session, call.from_user.id)
    await call.answer()
    await call.message.edit_text(text, reply_markup=kb)


@router.callback_query(CartCB.filter(F.action == "clear"))
async def cart_clear(call: CallbackQuery, session: AsyncSession) -> None:
    await repo.cart_clear(session, call.from_user.id)
    text, kb = await render_cart(session, call.from_user.id)
    await call.answer("Корзина очищена")
    await call.message.edit_text(text, reply_markup=kb)


@router.callback_query(CartCB.filter(F.action == "open"))
async def cart_open(call: CallbackQuery, session: AsyncSession) -> None:
    text, kb = await render_cart(session, call.from_user.id)
    await call.answer()
    await call.message.answer(text, reply_markup=kb)


@router.callback_query(CartCB.filter(F.action == "checkout"))
async def cart_checkout(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    items = await repo.cart_items(session, call.from_user.id)
    if not items:
        await call.answer("Корзина пуста", show_alert=True)
        return
    await call.answer()
    await state.set_state(Checkout.name)
    await call.message.answer(
        "Оформляем заказ. Как тебя зовут?\n\n<i>В любой момент — /cancel</i>"
    )
