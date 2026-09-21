"""Жизненный цикл заказа: одна схема переходов на все входы.

Кнопки админа, ввод трека и причины, самоотмена из чата и витрины, фоновая
автоотмена — всё идёт через apply_transition(). Правила лежат в словарях,
а не в хендлерах: иначе клавиатура, бот и API держали бы три копии и
расходились бы при первой правке.

Ключи — строки статусов, не члены OrderStatus: из SQLite статус приходит
как str, а Enum.__hash__ считается по имени члена (см. STATUS_RU).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Order, OrderStatusHistory, utcnow

# Гейт по оплате — свойство графа: у assembled/shipped/ready_for_pickup/
# delivered нет входящих рёбер в обход paid. Регресс-тест проверяет это
# обходом, а не отдельным if в коде.
TRANSITIONS: dict[str, set[str]] = {
    "new": {"paid", "cancelled"},
    "awaiting_payment": {"paid", "cancelled"},
    "paid": {"assembled", "cancelled"},
    "assembled": {"shipped", "ready_for_pickup", "cancelled"},
    "shipped": {"delivered", "cancelled"},
    "ready_for_pickup": {"delivered", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}

# Порядок кнопок админа: рабочий переход первым, отмена последней.
_ORDER = ["paid", "assembled", "shipped", "ready_for_pickup", "delivered", "cancelled"]

DELIVERY_ONLY: dict[str, set[str]] = {
    "shipped": {"courier", "post"},
    "ready_for_pickup": {"pickup"},
}

FINAL = {"delivered", "cancelled"}
CUSTOMER_MAY_CANCEL_FROM = {"new", "awaiting_payment"}
AUTO_CANCEL_FROM = {"new", "awaiting_payment"}

CANCEL_REASON_MAX = 200
TRACK_MAX = 64
CUSTOMER_CANCEL_NOTE = "отменён покупателем"
UNPAID_NOTE = "не оплачен в срок"


class InvalidTransition(Exception):
    """Переход не по схеме или заказ уже ушёл дальше. `current` — что в БД сейчас."""

    def __init__(self, current: str) -> None:
        super().__init__(current)
        self.current = current


def is_final(status: str) -> bool:
    return str(status) in FINAL


def allowed_next(order: Order) -> list[str]:
    nxt = TRANSITIONS.get(str(order.status), set())
    return [
        st for st in _ORDER
        if st in nxt and order.delivery_method in DELIVERY_ONLY.get(st, {order.delivery_method})
    ]


def note_required(to_status: str, delivery_method: str) -> bool:
    if to_status == "cancelled":
        return True
    # Курьеру трек не обязателен: у половины служб его просто нет.
    return to_status == "shipped" and delivery_method == "post"


def validate_note(to_status: str, delivery_method: str, note: str) -> str:
    """Обрезает и проверяет комментарий к переходу. ValueError — с текстом для админа."""
    note = (note or "").strip()
    if to_status == "cancelled":
        if not note:
            raise ValueError("Нужна причина отмены — покупатель её увидит")
        return note[:CANCEL_REASON_MAX]
    if to_status == "shipped":
        if delivery_method == "post" and not note:
            raise ValueError("Для почты нужен трек-номер")
        # Формат не проверяем: у почт мира он разный, а кириллица в треке — реальность.
        return note[:TRACK_MAX]
    return note


async def apply_transition(
    session: AsyncSession,
    order_id: int,
    from_status: str,
    to_status: str,
    actor: str,
    actor_id: int | None = None,
    note: str = "",
) -> tuple[Order, OrderStatusHistory]:
    """Единственный способ сменить статус заказа.

    from_status — то, что вызывающий видел (кнопка, callback, снимок перед
    сном sweeper-а). Если в БД уже другое, условный UPDATE не тронет строку,
    и вызывающий получит InvalidTransition с актуальным статусом. Кто проиграл
    гонку — тот и получает исключение; выиграть обе стороны не могут.
    """
    from_status, to_status = str(from_status), str(to_status)
    order = await repo.order(session, order_id)
    if order is None:
        raise InvalidTransition("")
    if to_status not in TRANSITIONS.get(from_status, set()):
        raise InvalidTransition(str(order.status))
    if order.delivery_method not in DELIVERY_ONLY.get(to_status, {order.delivery_method}):
        raise InvalidTransition(str(order.status))
    note = validate_note(to_status, order.delivery_method, note)

    if not await repo.set_order_status(session, order_id, expected=from_status, new=to_status):
        await session.rollback()
        current = await repo.order(session, order_id)
        raise InvalidTransition(str(current.status) if current else "")

    entry = await repo.add_status_entry(session, order_id, to_status, actor, actor_id, note)
    await session.commit()
    order = await repo.order(session, order_id)
    return order, entry


Notifier = Callable[[Order, OrderStatusHistory], Awaitable[object]]


async def expire_unpaid(
    session: AsyncSession,
    ttl_minutes: int,
    now: datetime | None = None,
    notify: Notifier | None = None,
) -> list[Order]:
    """Отменить заказы, не оплаченные за ttl_minutes, и вернуть товар на склад.

    Идёт через apply_transition с from_status из выборки: если админ подтвердил
    оплату между SELECT и UPDATE, условный UPDATE не пройдёт и заказ пропускается.
    """
    if ttl_minutes <= 0:
        return []
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(minutes=ttl_minutes)

    cancelled: list[Order] = []
    for stale in await repo.unpaid_older_than(session, AUTO_CANCEL_FROM, cutoff):
        try:
            order, entry = await apply_transition(
                session, stale.id, str(stale.status), "cancelled",
                actor="system", note=UNPAID_NOTE,
            )
        except InvalidTransition:
            continue
        await repo.return_stock(session, order)
        await session.commit()
        cancelled.append(order)
        if notify is not None:
            await notify(order, entry)
    return cancelled
