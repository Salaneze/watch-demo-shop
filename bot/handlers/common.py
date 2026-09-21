from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from api.session import code_from_start
from api.session import store as login_store
from bot.config import settings
from bot.db import repo
from bot.db.models import status_ru
from bot.keyboards.callbacks import CustomerOrderCB
from bot.keyboards.common import customer_order_kb, main_menu
from bot.utils.lifecycle import CUSTOMER_CANCEL_NOTE, InvalidTransition, apply_transition
from bot.utils.money import fmt
from bot.utils.notify import history_lines, notify_admins_order_cancelled
from bot.utils.text import esc

router = Router(name="common")


@router.message(CommandStart(deep_link=True))
async def cmd_start_login(message: Message, command: CommandObject, session: AsyncSession,
                          state: FSMContext) -> None:
    """`/start wa_<код>` — подтверждение входа в витрину.

    Сюда приходят только клиенты, у которых Mini App не отдаёт подпись Telegram.
    Апдейт пришёл через серверы Telegram, значит отправитель — настоящий владелец
    аккаунта, и доказывать личность как-то ещё не нужно.
    """
    code = code_from_start(command.args or "")
    if code is None:
        await cmd_start(message, session, state)
        return

    await state.clear()
    await repo.upsert_user(
        session, message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    confirmed = login_store.confirm(
        code, message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    await message.answer(
        "✅ Витрина авторизована — вернись в неё, каталог уже открыт."
        if confirmed else
        "⌛ Код входа устарел. Открой витрину заново и нажми «Войти» ещё раз.",
        reply_markup=main_menu(message.from_user.id in settings.admins),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await repo.upsert_user(
        session,
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await message.answer(
        f"Привет, {esc(message.from_user.first_name)}! 👋\n\n"
        "⌚ <b>Watch Demo</b> — витрина магазина часов.\n"
        "Загляни в каталог, собери корзину и оформи заказ — весь путь покупателя работает по-настоящему.\n\n"
        "<i>Демонстрационный бот: товары вымышленные, оплата не списывается.</i>",
        reply_markup=main_menu(message.from_user.id in settings.admins),
    )


@router.message(Command("cancel"))
@router.message(F.text.casefold() == "отмена")
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменил.", reply_markup=main_menu(message.from_user.id in settings.admins))


@router.message(F.text == "ℹ️ О магазине")
async def about(message: Message) -> None:
    await message.answer(
        "⌚ <b>Watch Demo</b>\n\n"
        "Витрина магазина часов: механика, кварц, ремешки, подарочные наборы.\n\n"
        "Как это работает: каталог → корзина → оформление заказа → оплата по реквизитам "
        "с подтверждением администратором.\n\n"
        "<i>Это демонстрационный бот. Товары вымышленные, заказы реально не отгружаются.</i>"
    )


@router.message(F.text == "📦 Мои заказы")
async def my_orders(message: Message, session: AsyncSession) -> None:
    mine = await repo.orders_by_user(session, message.from_user.id, limit=10)
    if not mine:
        await message.answer("У тебя пока нет заказов.")
        return
    # По сообщению на заказ: у каждого свои кнопки (история, отмена пока не оплачен).
    for o in mine:
        await message.answer(
            f"<b>Заказ #{o.id}</b> — {fmt(o.total)} — {status_ru(o.status)}",
            reply_markup=customer_order_kb(o),
        )


async def _own_order(call: CallbackQuery, session: AsyncSession, order_id: int):
    """Заказ по кнопке, только свой. Чужой и несуществующий неотличимы —
    номер заказа угадывается перебором, существование не раскрываем."""
    order = await repo.order(session, order_id)
    if order is None or order.user_id != call.from_user.id:
        await call.answer("Заказ не найден", show_alert=True)
        return None
    return order


@router.callback_query(CustomerOrderCB.filter(F.action == "history"))
async def order_history(call: CallbackQuery, callback_data: CustomerOrderCB, session: AsyncSession) -> None:
    order = await _own_order(call, session, callback_data.order_id)
    if order is None:
        return
    history = await repo.history_for(session, order.id)
    await call.answer()
    await call.message.answer(
        f"🕓 <b>Заказ #{order.id}</b>\n" + "\n".join(history_lines(history))
    )


@router.callback_query(CustomerOrderCB.filter(F.action == "cancel"))
async def order_cancel(
    call: CallbackQuery, callback_data: CustomerOrderCB, session: AsyncSession, bot: Bot,
) -> None:
    order = await _own_order(call, session, callback_data.order_id)
    if order is None:
        return
    try:
        order, _ = await apply_transition(
            session, order.id, str(order.status), "cancelled",
            actor="customer", actor_id=call.from_user.id, note=CUSTOMER_CANCEL_NOTE,
        )
    except InvalidTransition as e:
        await call.answer(
            f"Заказ уже в статусе {status_ru(e.current)} — для отмены свяжитесь с магазином",
            show_alert=True,
        )
        return
    await call.answer("Заказ отменён")
    await call.message.edit_text(
        f"<b>Заказ #{order.id}</b> — {fmt(order.total)} — {status_ru(order.status)}",
        reply_markup=customer_order_kb(order),
    )
    await notify_admins_order_cancelled(bot, order, source="чат")


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery) -> None:
    await call.answer()
