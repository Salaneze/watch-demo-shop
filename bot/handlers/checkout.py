import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.keyboards.common import confirm_order_kb, main_menu
from bot.states import Checkout
from bot.utils.money import fmt
from bot.utils.notify import notify_admins_new_order, order_text
from bot.utils.text import esc

router = Router(name="checkout")
log = logging.getLogger(__name__)

# order_text переехал в utils/notify — тот же текст нужен витрине Mini App.
# Имя остаётся видимым отсюда: на него завязаны тесты и старые импорты.
__all__ = ["router", "order_text"]


@router.message(Checkout.name, F.text)
async def step_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(Checkout.phone)
    await message.answer("Телефон для связи?")


@router.message(Checkout.phone, F.text)
async def step_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()
    digits = sum(c.isdigit() for c in phone)
    if digits < 10:
        await message.answer("Похоже на неполный номер. Введи ещё раз, например +7 900 123-45-67")
        return
    await state.update_data(phone=phone)
    await state.set_state(Checkout.address)
    await message.answer("Адрес доставки?")


@router.message(Checkout.address, F.text)
async def step_address(message: Message, state: FSMContext) -> None:
    await state.update_data(address=message.text.strip())
    await state.set_state(Checkout.comment)
    await message.answer("Комментарий к заказу? Если не нужен — напиши «-»")


@router.message(Checkout.comment, F.text)
async def step_comment(message: Message, state: FSMContext, session: AsyncSession) -> None:
    comment = message.text.strip()
    await state.update_data(comment="" if comment == "-" else comment)
    data = await state.get_data()

    total = await repo.cart_total(session, message.from_user.id)
    items = await repo.cart_items(session, message.from_user.id)
    if not items:
        await state.clear()
        await message.answer("Корзина опустела, заказ отменён.", reply_markup=main_menu())
        return

    preview = ["<b>Проверь заказ</b>\n"]
    for i in items:
        preview.append(f"• {esc(i.product.title)} — {i.qty} × {fmt(i.product.price)}")
    preview.append(f"\n<b>Итого: {fmt(total)}</b>")
    preview.append(
        f"\n👤 {esc(data['name'])}\n📞 {esc(data['phone'])}\n🏠 {esc(data['address'])}"
    )
    if data["comment"]:
        preview.append(f"💬 {esc(data['comment'])}")

    # Сумму запоминаем: между показом и нажатием «Подтвердить» админ может
    # поменять цену, и списывать другую сумму молча нельзя.
    await state.update_data(shown_total=total)
    await state.set_state(Checkout.confirm)
    await message.answer("\n".join(preview), reply_markup=confirm_order_kb())


@router.callback_query(Checkout.confirm, F.data == "checkout:cancel")
async def confirm_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    await call.message.edit_text("Заказ отменён. Корзина сохранена.")


@router.callback_query(Checkout.confirm, F.data == "checkout:confirm")
async def confirm_ok(call: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    data = await state.get_data()
    try:
        order = await repo.create_order(
            session,
            user_id=call.from_user.id,
            name=data["name"],
            phone=data["phone"],
            address=data["address"],
            comment=data["comment"],
            expected_total=data.get("shown_total"),
        )
    except repo.CartChanged as exc:
        await state.clear()
        await call.answer("Корзина изменилась", show_alert=True)
        await call.message.answer(
            f"⚠️ {esc(str(exc))}\n\nЗаказ не оформлен — загляни в корзину и подтверди заново.",
            reply_markup=main_menu(call.from_user.id in settings.admins),
        )
        return
    except ValueError:
        await state.clear()
        await call.answer("Корзина пуста", show_alert=True)
        return

    await state.clear()
    await call.answer()
    await call.message.edit_text(order_text(order))
    await call.message.answer(
        f"✅ Заказ #{order.id} принят.\n\n"
        f"💳 <b>Оплата</b>\n{settings.payment_details}\n"
        f"Сумма: <b>{fmt(order.total)}</b>\n\n"
        "После перевода пришли сюда скриншот или фото чека — админ подтвердит оплату.",
        reply_markup=main_menu(call.from_user.id in settings.admins),
    )

    await notify_admins_new_order(bot, order)


@router.message(StateFilter(None), F.photo | F.document)
async def receipt(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Фото/документ вне какого-либо FSM считаем чеком и пересылаем админам.

    StateFilter(None) обязателен: без него этот хендлер перехватывал бы фото
    у админского /addproduct, который идёт следующим роутером.
    """
    mine = await repo.orders_by_user(session, message.from_user.id, limit=1)
    if not mine:
        return

    last = mine[0]
    await message.answer(f"Принял чек по заказу #{last.id}. Ждём подтверждения админа.")
    for admin_id in settings.admins:
        try:
            await message.forward(admin_id)
            await bot.send_message(
                admin_id,
                f"⬆️ Чек к заказу #{last.id} на {fmt(last.total)} от "
                f"<a href='tg://user?id={message.from_user.id}'>{esc(message.from_user.full_name)}</a>",
                reply_markup=admin_order_kb(last),
            )
        except Exception as e:
            log.warning("Не смог переслать чек админу %s: %s", admin_id, e)
