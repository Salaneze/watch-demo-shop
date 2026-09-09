import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.keyboards.common import (
    admin_order_kb,
    confirm_order_kb,
    delivery_kb,
    main_menu,
    promo_kb,
)
from bot.states import Checkout
from bot.utils.delivery import DEFAULT_DELIVERY, delivery_option
from bot.utils.money import fmt
from bot.utils.notify import notify_admins_new_order, order_text, totals_lines
from bot.utils.text import esc

# Промокод уезжает в Order.promo_code — колонка на 32 символа. Режем на входе,
# иначе длинный ввод дойдёт до вставки в базу и уронит уже оформленный заказ.
PROMO_MAX = 32

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
    await state.set_state(Checkout.delivery)
    await message.answer("Как доставить заказ?", reply_markup=delivery_kb())


@router.callback_query(Checkout.delivery, F.data.startswith("checkout:dlv:"))
async def step_delivery(call: CallbackQuery, state: FSMContext) -> None:
    code = call.data.removeprefix("checkout:dlv:")
    try:
        option = delivery_option(code)
    except ValueError:
        # Способ мог исчезнуть из настроек, пока сообщение висело в чате.
        await call.answer("Способ больше недоступен, выбери другой", show_alert=True)
        return

    await state.update_data(delivery=option.code)
    await state.set_state(Checkout.promo)
    await call.answer()
    await call.message.edit_text(f"Доставка: <b>{esc(option.title)}</b> — {esc(option.note)}")
    await call.message.answer(
        "Есть промокод? Пришли его сообщением.", reply_markup=promo_kb()
    )


@router.message(Checkout.promo, F.text)
async def step_promo(message: Message, state: FSMContext) -> None:
    await state.update_data(promo_code=message.text.strip()[:PROMO_MAX])
    await ask_comment(message, state)


@router.callback_query(Checkout.promo, F.data == "checkout:nopromo")
async def skip_promo(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(promo_code="")
    await call.answer()
    await call.message.edit_text("Без промокода.")
    await ask_comment(call.message, state)


async def ask_comment(message: Message, state: FSMContext) -> None:
    await state.set_state(Checkout.comment)
    await message.answer("Комментарий к заказу? Если не нужен — напиши «-»")


@router.message(Checkout.comment, F.text)
async def step_comment(message: Message, state: FSMContext, session: AsyncSession) -> None:
    comment = message.text.strip()
    await state.update_data(comment="" if comment == "-" else comment)
    data = await state.get_data()

    items = await repo.cart_items(session, message.from_user.id)
    if not items:
        await state.clear()
        await message.answer("Корзина опустела, заказ отменён.", reply_markup=main_menu())
        return

    promo_code = data.get("promo_code", "")
    totals = await repo.cart_quote(
        session,
        message.from_user.id,
        delivery=data.get("delivery", DEFAULT_DELIVERY),
        promo_code=promo_code,
    )

    preview = ["<b>Проверь заказ</b>\n"]
    for i in items:
        preview.append(f"• {esc(i.product.title)} — {i.qty} × {fmt(i.product.price)}")
    preview.append("")
    preview += totals_lines(
        items_total=totals.items_total,
        discount=totals.discount,
        promo_code=totals.promo_code,
        delivery_title=totals.option.title,
        delivery_cost=totals.option.cost,
        total=totals.total,
    )
    # Про непринятый промокод говорим сразу: молча оформленный заказ без скидки
    # покупатель заметит уже после оплаты, и это будет разговор с админом.
    if promo_code and totals.promo is None:
        preview.append(f"\n⚠️ Промокод «{esc(promo_code)}» не действует.")
    preview.append(
        f"\n👤 {esc(data['name'])}\n📞 {esc(data['phone'])}\n🏠 {esc(data['address'])}"
    )
    if data["comment"]:
        preview.append(f"💬 {esc(data['comment'])}")

    # Сумму запоминаем: между показом и нажатием «Подтвердить» админ может
    # поменять цену, и списывать другую сумму молча нельзя.
    await state.update_data(shown_total=totals.total)
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
            delivery=data.get("delivery", DEFAULT_DELIVERY),
            promo_code=data.get("promo_code", ""),
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
