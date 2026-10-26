import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import Order, OrderStatus, status_ru
from bot.keyboards.callbacks import AdminOrderCB, ReviewAdminCB
from bot.keyboards.common import admin_order_kb, admin_panel_kb, admin_review_kb
from bot.states import AddProduct, AdminOrderInput
from bot.utils.admin_ops import admin_transition
from bot.utils.ask_photo import parse_marker
from bot.utils.lifecycle import (
    CANCEL_REASON_MAX,
    InvalidTransition,
    is_final,
    note_required,
    validate_note,
)
from bot.utils.money import fmt, symbol
from bot.utils.notify import history_lines, order_text, send_admin_order
from bot.utils.reviews import admin_review_text
from bot.utils.share import product_link
from bot.utils.text import esc, local_time

router = Router(name="admin")
log = logging.getLogger(__name__)

# Весь роутер — только для админов.
router.message.filter(F.from_user.id.in_(settings.admins))
router.callback_query.filter(F.from_user.id.in_(settings.admins))


@router.message(F.text == "⚙️ Админка")
@router.message(Command("admin"))
async def admin_menu(message: Message) -> None:
    await message.answer(
        "⚙️ <b>Админка</b>\n\n"
        "/orders — последние заказы\n"
        "/neworders — ждущие оплаты\n"
        "/addcat Название — новая категория\n"
        "/addproduct — добавить товар (пошагово)\n"
        "/hide ID — скрыть/показать товар\n"
        "/audit — журнал действий администраторов",
        reply_markup=admin_panel_kb(),
    )


@router.message(Command("audit"))
async def show_audit(message: Message, session: AsyncSession) -> None:
    """Кто что менял. Нужен, как только администраторов становится больше одного."""
    entries = await repo.recent_actions(session, limit=20)
    if not entries:
        await message.answer("Журнал пуст — никто ничего не менял.")
        return

    lines = ["📋 <b>Последние действия</b>\n"]
    for e in entries:
        when = local_time(e.created_at)
        who = "ты" if e.admin_id == message.from_user.id else f"id{e.admin_id}"
        detail = f" — {esc(e.details)}" if e.details else ""
        lines.append(f"<code>{when}</code> {who}: {esc(e.action)} {esc(e.target)}{detail}")
    await message.answer("\n".join(lines))


async def _send_order_rows(message: Message, bot: Bot, session: AsyncSession, orders: list[Order]) -> None:
    for o in orders:
        await send_admin_order(
            bot, session, message.chat.id,
            f"<b>#{o.id}</b> — {fmt(o.total)} — {status_ru(o.status)}\n"
            f"👤 {esc(o.contact_name)}, 📞 {esc(o.contact_phone)}",
            o,
        )


@router.message(Command("orders"))
async def list_orders(message: Message, bot: Bot, session: AsyncSession) -> None:
    orders = await repo.orders_by_status(session, limit=15)
    if not orders:
        await message.answer("Заказов нет.")
        return
    await _send_order_rows(message, bot, session, orders)


@router.message(Command("neworders"))
async def list_new_orders(message: Message, bot: Bot, session: AsyncSession) -> None:
    orders = await repo.orders_by_status(session, OrderStatus.awaiting_payment, limit=15)
    if not orders:
        await message.answer("Нет заказов, ждущих оплаты.")
        return
    await _send_order_rows(message, bot, session, orders)


def _order_card(order, history) -> str:
    return order_text(order, for_admin=True) + "\n\n🕓 <b>История</b>\n" + "\n".join(history_lines(history))


async def _finish_transition(
    session: AsyncSession, bot: Bot, admin_id: int,
    order_id: int, from_status: str, to_status: str, note: str = "",
    skip: tuple[int, int] | None = None,
) -> tuple[str, Order, list]:
    """Общий хвост кнопки и текстового ввода: переход, журнал, уведомление.

    Возвращает текст для админа, заказ и историю. InvalidTransition наружу —
    вызывающий решает, alert это или обычный ответ.
    """
    order, _, notified = await admin_transition(
        session, bot, admin_id, order_id, from_status, to_status, note, skip=skip,
    )
    reply = f"Статус: {status_ru(to_status)}"
    if not notified:
        reply += "\n⚠ покупатель не уведомлён"
    return reply, order, await repo.history_for(session, order.id)


@router.callback_query(AdminOrderCB.filter())
async def order_action(
    call: CallbackQuery, callback_data: AdminOrderCB, session: AsyncSession,
    bot: Bot, state: FSMContext,
) -> None:
    order = await repo.order(session, callback_data.order_id)
    if order is None:
        await call.answer("Заказ не найден", show_alert=True)
        return
    to_status, from_status = callback_data.action, callback_data.from_status

    # Отмена и почтовый трек требуют текста — уходим в FSM, переход случится
    # после ввода. Проверка статуса там же: пока админ печатает, заказ может уйти.
    if note_required(to_status, order.delivery_method):
        await state.set_state(
            AdminOrderInput.cancel_reason if to_status == "cancelled" else AdminOrderInput.track
        )
        await state.update_data(order_id=order.id, from_status=from_status, to_status=to_status)
        prompt = (
            f"Причина отмены заказа #{order.id} (до {CANCEL_REASON_MAX} символов), покупатель её увидит"
            if to_status == "cancelled" else
            f"Трек-номер для заказа #{order.id}"
        )
        await call.answer()
        await call.message.answer(prompt + "\n\n/cancel — передумал")
        return

    try:
        reply, order, history = await _finish_transition(
            session, bot, call.from_user.id, order.id, from_status, to_status,
            skip=(call.message.chat.id, call.message.message_id),
        )
    except InvalidTransition as e:
        await call.answer(f"Заказ уже в статусе {status_ru(e.current)}", show_alert=True)
        # Перечитать: после отката сессии загруженный объект протух.
        order = await repo.order(session, order.id)
        await call.message.edit_reply_markup(reply_markup=admin_order_kb(order))
        return

    await call.answer(reply, show_alert="⚠" in reply)
    if is_final(order.status):
        await call.message.edit_text(_order_card(order, history), reply_markup=None)
    else:
        await call.message.edit_reply_markup(reply_markup=admin_order_kb(order))


@router.message(AdminOrderInput.track, F.text)
@router.message(AdminOrderInput.cancel_reason, F.text)
async def order_input(message: Message, session: AsyncSession, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    order = await repo.order(session, data["order_id"])
    if order is None:
        await state.clear()
        await message.answer("Заказ не найден")
        return
    try:
        note = validate_note(data["to_status"], order.delivery_method, message.text)
    except ValueError as e:
        await message.answer(f"{e}. Попробуй ещё раз или /cancel")
        return

    await state.clear()
    try:
        reply, order, history = await _finish_transition(
            session, bot, message.from_user.id,
            order.id, data["from_status"], data["to_status"], note,
        )
    except InvalidTransition as e:
        await message.answer(f"Заказ уже в статусе {status_ru(e.current)}, ничего не менял")
        return
    if is_final(order.status):
        await message.answer(reply + "\n\n" + _order_card(order, history))
    else:
        await send_admin_order(bot, session, message.chat.id, reply + "\n\n" + _order_card(order, history), order)


@router.message(Command("addcat"))
async def add_category(message: Message, session: AsyncSession) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: <code>/addcat Название категории</code>")
        return
    cat = await repo.add_category(session, parts[1].strip())
    await repo.log_action(session, message.from_user.id, "category_add",
                          f"category#{cat.id}", cat.title)
    await message.answer(f"Категория «{esc(cat.title)}» создана, id={cat.id}")


@router.message(Command("hide"))
async def hide_product(message: Message, session: AsyncSession) -> None:
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/hide 12</code>")
        return
    p = await repo.toggle_product(session, int(parts[1]))
    if p is not None:
        await repo.log_action(session, message.from_user.id, "product_toggle",
                              f"product#{p.id}",
                              "показан" if p.is_active else "скрыт")
    if p is None:
        await message.answer("Товар не найден.")
        return
    await message.answer(f"«{esc(p.title)}» теперь {'виден' if p.is_active else 'скрыт'}.")


# ---------- пошаговое добавление товара ----------

@router.message(Command("addproduct"))
async def add_product_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    cats = await repo.active_categories(session)
    if not cats:
        await message.answer("Сначала создай категорию: /addcat Название")
        return
    listing = "\n".join(f"{c.id} — {esc(c.title)}" for c in cats)
    await state.set_state(AddProduct.category)
    await message.answer(f"В какую категорию? Пришли id:\n\n{listing}\n\n/cancel — отмена")


@router.message(AddProduct.category, F.text)
async def add_product_category(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.text.strip().isdigit():
        await message.answer("Нужен числовой id категории.")
        return
    cat = await repo.category(session, int(message.text.strip()))
    if cat is None:
        await message.answer("Категория не найдена, попробуй ещё раз.")
        return
    await state.update_data(category_id=cat.id)
    await state.set_state(AddProduct.title)
    await message.answer("Название товара?")


@router.message(AddProduct.title, F.text)
async def add_product_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip()[:128])
    await state.set_state(AddProduct.description)
    await message.answer("Описание? («-» чтобы пропустить)")


@router.message(AddProduct.description, F.text)
async def add_product_description(message: Message, state: FSMContext) -> None:
    desc = message.text.strip()
    await state.update_data(description="" if desc == "-" else desc)
    await state.set_state(AddProduct.price)
    await message.answer(f"Цена целым числом, в {symbol()}?")


@router.message(AddProduct.price, F.text)
async def add_product_price(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(" ", "")
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Нужно положительное целое число.")
        return
    await state.update_data(price=int(raw))
    await state.set_state(AddProduct.photo)
    await message.answer("Пришли фото товара или напиши «-», чтобы без фото.")


@router.message(AddProduct.photo, F.photo)
async def add_product_photo(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await _finish_product(message, state, session, message.photo[-1].file_id)


@router.message(AddProduct.photo, F.text == "-")
async def add_product_no_photo(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await _finish_product(message, state, session, None)


async def _finish_product(
    message: Message, state: FSMContext, session: AsyncSession, file_id: str | None
) -> None:
    data = await state.get_data()
    p = await repo.add_product(
        session,
        category_id=data["category_id"],
        title=data["title"],
        description=data["description"],
        price=data["price"],
        photo_file_id=file_id,
    )
    await repo.log_action(session, message.from_user.id, "product_add",
                          f"product#{p.id}", f"{p.title} — {fmt(p.price)}")
    await state.clear()
    await message.answer(f"✅ Товар «{esc(p.title)}» добавлен (id={p.id}, {fmt(p.price)}).")


# Фото к уже существующему товару — для тех, что заведены в веб-панели. Там
# картинку не загрузить: photo_file_id выдаёт Telegram, а не наш сервер.
@router.message(Command("photo"), F.photo)
async def set_product_photo(message: Message, session: AsyncSession) -> None:
    parts = (message.caption or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Подпись к фото: <code>/photo 12</code>, где 12 — номер товара из панели.")
        return
    p = await repo.product(session, int(parts[1]))
    if p is None:
        await message.answer("Товар не найден.")
        return
    p.photo_file_id = message.photo[-1].file_id
    await session.commit()
    await repo.log_action(session, message.from_user.id, "product_photo", f"product#{p.id}", p.title)
    await message.answer(f"🖼 Фото у «{esc(p.title)}» обновлено.")


@router.message(Command("photo"))
async def set_product_photo_hint(message: Message) -> None:
    # Команда пришла текстом, без снимка — иначе её молча съел бы поиск по каталогу.
    await message.answer("Пришлите фото с подписью <code>/photo 12</code>, где 12 — номер товара.")


@router.callback_query(ReviewAdminCB.filter())
async def review_visibility(call: CallbackQuery, callback_data: ReviewAdminCB, session: AsyncSession) -> None:
    review = await repo.set_review_hidden(session, callback_data.review_id, callback_data.hide)
    if review is None:
        await call.answer("Отзыв не найден", show_alert=True)
        return
    await repo.log_action(
        session, call.from_user.id, "review_hide" if review.is_hidden else "review_show",
        f"review#{review.id}", f"product#{review.product_id}",
    )
    await call.answer("Скрыт с витрины" if review.is_hidden else "Снова на витрине")
    product = await repo.product(session, review.product_id)
    text = admin_review_text(review, product.title if product else f"товар #{review.product_id}")
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=text, reply_markup=admin_review_kb(review))
        else:
            await call.message.edit_text(text, reply_markup=admin_review_kb(review))
    except Exception as e:
        # Второй админ уже нажал ту же кнопку — Telegram отвечает «message is
        # not modified». Само действие от этого не отменяется.
        log.info("Сообщение об отзыве #%s не обновлено: %s", review.id, e)


@router.message(F.reply_to_message, F.photo | F.video)
async def answer_photo_request(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Ответ фото на «Просят живое фото» уходит покупателю от имени бота.

    Метку читаем только из сообщения самого бота: иначе админ, ответивший на
    пересланный кем-то текст с подставной меткой, отправил бы фото чужому.
    """
    replied = message.reply_to_message
    target = parse_marker(replied.text) if replied.from_user and replied.from_user.id == bot.id else None
    if target is None:
        return
    user_id, product_id = target
    product = await repo.product(session, product_id)
    title = product.title if product else f"товар #{product_id}"
    caption = f"📷 Живое фото по вашей просьбе: <b>{esc(title)}</b>"
    if message.caption:
        caption += f"\n\n{esc(message.caption)}"
    me = await bot.me()
    kb = None
    if product and product.is_active and me.username:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть товар", url=product_link(me.username, product.id)),
        ]])
    try:
        await bot.copy_message(user_id, message.chat.id, message.message_id,
                               caption=caption[:1024], reply_markup=kb)
    except Exception as e:
        # Покупатель заблокировал бота или пришёл из витрины, ни разу не нажав
        # /start. Админу остаётся написать ему напрямую по ссылке из просьбы.
        log.info("Живое фото покупателю %s не доставлено: %s", user_id, e)
        await message.reply("Не доставлено: покупатель закрыл личку с ботом. "
                            "Напишите ему по ссылке в просьбе.")
        return
    await repo.log_action(session, message.from_user.id, "live_photo",
                          f"product#{product_id}", f"user#{user_id}")
    await message.reply("Отправлено покупателю ✅")
