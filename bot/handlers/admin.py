import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import OrderStatus, status_ru
from bot.keyboards.callbacks import AdminOrderCB
from bot.keyboards.common import admin_order_kb
from bot.states import AddProduct
from bot.utils.money import fmt, symbol
from bot.utils.text import esc

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
        "/audit — журнал действий администраторов"
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
        when = e.created_at.strftime("%d.%m %H:%M")
        who = "ты" if e.admin_id == message.from_user.id else f"id{e.admin_id}"
        detail = f" — {esc(e.details)}" if e.details else ""
        lines.append(f"<code>{when}</code> {who}: {esc(e.action)} {esc(e.target)}{detail}")
    await message.answer("\n".join(lines))


@router.message(Command("orders"))
async def list_orders(message: Message, session: AsyncSession) -> None:
    orders = await repo.orders_by_status(session, limit=15)
    if not orders:
        await message.answer("Заказов нет.")
        return
    for o in orders:
        await message.answer(
            f"<b>#{o.id}</b> — {fmt(o.total)} — {status_ru(o.status)}\n"
            f"👤 {esc(o.contact_name)}, 📞 {esc(o.contact_phone)}",
            reply_markup=admin_order_kb(o),
        )


@router.message(Command("neworders"))
async def list_new_orders(message: Message, session: AsyncSession) -> None:
    orders = await repo.orders_by_status(session, OrderStatus.awaiting_payment, limit=15)
    if not orders:
        await message.answer("Нет заказов, ждущих оплаты.")
        return
    for o in orders:
        await message.answer(
            f"<b>#{o.id}</b> — {fmt(o.total)} — {status_ru(o.status)}\n"
            f"👤 {esc(o.contact_name)}, 📞 {esc(o.contact_phone)}",
            reply_markup=admin_order_kb(o),
        )


@router.callback_query(AdminOrderCB.filter())
async def order_action(
    call: CallbackQuery, callback_data: AdminOrderCB, session: AsyncSession, bot: Bot
) -> None:
    mapping = {
        "paid": OrderStatus.paid,
        "shipped": OrderStatus.shipped,
        "cancel": OrderStatus.cancelled,
    }
    new_status = mapping.get(callback_data.action)
    if new_status is None:
        await call.answer("Неизвестное действие", show_alert=True)
        return

    order = await repo.set_order_status(session, callback_data.order_id, new_status)
    if order is None:
        await call.answer("Заказ не найден", show_alert=True)
        return

    await repo.log_action(
        session, call.from_user.id, "order_status",
        f"order#{order.id}", f"-> {new_status.value}",
    )

    await call.answer(f"Статус: {status_ru(new_status)}")
    await call.message.edit_reply_markup(reply_markup=admin_order_kb(order))

    try:
        await bot.send_message(
            order.user_id,
            f"Заказ #{order.id}: статус изменён — <b>{status_ru(new_status)}</b>",
        )
    except Exception as e:
        log.warning("Не смог уведомить покупателя %s: %s", order.user_id, e)


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
