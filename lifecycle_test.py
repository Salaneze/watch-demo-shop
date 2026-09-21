"""Жизненный цикл заказа: матрица переходов, история, уведомления, автоотмена.

Главная проверка — не «кнопка работает», а что схема переходов непробиваема:
перебираются ВСЕ пары «из → в» для каждого способа получения, а гейт по оплате
проверяется обходом графа. Остальное (тексты, склад, гонки) — заодно.

Запуск: .venv\\Scripts\\python.exe lifecycle_test.py
"""
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///lifecycle_test.db"
os.environ["SEED"] = "plain"
os.environ["PICKUP_ADDRESS"] = "ул. Тестовая, 1"
os.environ["PICKUP_HOURS"] = "10-19 без выходных"

from aiogram.exceptions import TelegramForbiddenError  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402
from sqlalchemy import text, update  # noqa: E402

from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import Order, OrderStatus, Product, utcnow  # noqa: E402
from bot.keyboards.common import admin_order_kb, customer_order_kb  # noqa: E402
from bot.utils.lifecycle import (  # noqa: E402
    AUTO_CANCEL_FROM,
    CUSTOMER_CANCEL_NOTE,
    CUSTOMER_MAY_CANCEL_FROM,
    DELIVERY_ONLY,
    TRANSITIONS,
    UNPAID_NOTE,
    InvalidTransition,
    allowed_next,
    apply_transition,
    expire_unpaid,
    is_final,
    validate_note,
)
from bot.utils.notify import notify_customer_status, status_message  # noqa: E402

DB_FILE = Path("lifecycle_test.db")
STATUSES = [s.value for s in OrderStatus]
METHODS = ["pickup", "courier", "post"]
USER = 424000777

ok = 0
fail = 0


def check(label: str, condition: bool, extra: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label} — {extra}")


class FakeBot:
    """Копит send_message; в режиме blocked бросает то же, что Telegram при блокировке."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.blocked = False

    async def send_message(self, chat_id: int, text: str, **kw) -> None:
        if self.blocked:
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=chat_id, text=text), message="Forbidden: bot was blocked by the user",
            )
        self.sent.append((chat_id, text))


async def make_order(s, product_id: int, method: str = "pickup", qty: int = 1, user: int = USER) -> Order:
    await repo.cart_add(s, user, product_id, qty)
    return await repo.create_order(s, user, "Тест", "+70000000000", "адрес", "", delivery=method)


async def force_status(s, order_id: int, status: str) -> None:
    """Выставить статус в обход схемы — только для подготовки матрицы."""
    await s.execute(update(Order).where(Order.id == order_id).values(status=status))
    await s.commit()


def valid_note(to_status: str) -> str:
    return "тестовая причина" if to_status == "cancelled" else "RA123456789RU"


async def main() -> None:
    if DB_FILE.exists():
        DB_FILE.unlink()
    await init_db()

    async with session_factory() as s:
        cat = await repo.add_category(s, "Тест")
        # Только id: rollback внутри apply_transition сбрасывает загруженные
        # объекты, и доступ к prod из async упал бы на ленивой подгрузке.
        prod = (await repo.add_product(s, cat.id, "Товар", "", 100, None)).id
        limited = (await repo.add_product(s, cat.id, "Штучный", "", 100, None, stock=3)).id

        print("\n[1] Матрица переходов: 8 статусов x 8 x 3 способа получения")
        allowed = rejected = wrong = 0
        for method in METHODS:
            for frm in STATUSES:
                for to in STATUSES:
                    o = await make_order(s, prod, method)
                    await force_status(s, o.id, frm)
                    expected = to in TRANSITIONS[frm] and method in DELIVERY_ONLY.get(to, {method})
                    try:
                        await apply_transition(s, o.id, frm, to, actor="admin", note=valid_note(to))
                        got = True
                    except InvalidTransition:
                        got = False
                    if got != expected:
                        wrong += 1
                        print(f"       {method}: {frm} -> {to}: ожидалось {expected}, прошло {got}")
                    allowed += got
                    rejected += not got
                    if got:
                        fresh = await repo.order(s, o.id)
                        if str(fresh.status) != to:
                            wrong += 1
        print(f"       transitions: {len(STATUSES)} statuses x {len(METHODS)} delivery methods, "
              f"{allowed} allowed / {rejected} rejected")
        check("ни одной пары вне схемы", wrong == 0, f"расхождений: {wrong}")
        # По схеме: pickup 2+2+2+2+0+2+0+0=12, courier/post 2+2+2+2+2+0+0+0=12
        check("число допустимых совпадает со схемой", allowed == 36, str(allowed))

        print("\n[2] Гейт по оплате — обходом графа")
        for method in METHODS:
            seen, stack = set(), ["new", "awaiting_payment"]
            while stack:
                cur = stack.pop()
                for nxt in TRANSITIONS[cur]:
                    if nxt == "paid" or nxt in seen:
                        continue
                    if method in DELIVERY_ONLY.get(nxt, {method}):
                        seen.add(nxt)
                        stack.append(nxt)
            check(f"{method}: без paid недостижимы сборка/отправка/выдача",
                  not seen & {"assembled", "shipped", "ready_for_pickup", "delivered"}, str(seen))
        check("финальные — только delivered и cancelled",
              [st for st in STATUSES if is_final(st)] == ["delivered", "cancelled"])

        print("\n[3] Кнопки админа по способу получения")
        o = await make_order(s, prod, "pickup")
        await force_status(s, o.id, "assembled")
        o = await repo.order(s, o.id)
        labels = [b.text for row in admin_order_kb(o).inline_keyboard for b in row]
        check("самовывоз из «собран»: есть «Готов к выдаче», нет «Отправлен»",
              any("Готов" in l for l in labels) and not any("Отправлен" in l for l in labels), str(labels))
        o2 = await make_order(s, prod, "courier")
        await force_status(s, o2.id, "assembled")
        o2 = await repo.order(s, o2.id)
        labels = [b.text for row in admin_order_kb(o2).inline_keyboard for b in row]
        check("курьер из «собран»: есть «Отправлен», нет «Готов к выдаче»",
              any("Отправлен" in l for l in labels) and not any("Готов" in l for l in labels), str(labels))
        await force_status(s, o2.id, "delivered")
        o2 = await repo.order(s, o2.id)
        check("финальный заказ без кнопок", not admin_order_kb(o2).inline_keyboard)
        check("allowed_next: отмена последней", allowed_next(o)[-1] == "cancelled", str(allowed_next(o)))

        print("\n[4] Кнопка в старом сообщении")
        o = await make_order(s, prod, "courier")
        await apply_transition(s, o.id, "awaiting_payment", "paid", actor="admin", actor_id=1)
        try:
            await apply_transition(s, o.id, "awaiting_payment", "paid", actor="admin", actor_id=2)
            check("stale button rejected", False, "второй переход прошёл")
        except InvalidTransition as e:
            check("stale button rejected", e.current == "paid", e.current)
        fresh = await repo.order(s, o.id)
        check("статус не тронут", str(fresh.status) == "paid")
        hist = await repo.history_for(s, o.id)
        check("в истории ровно две записи (создание + оплата)", len(hist) == 2, str(len(hist)))

        print("\n[5] Валидация комментария")
        for label, args, should_fail in [
            ("отмена без причины отбита", ("cancelled", "courier", "   "), True),
            ("почта без трека отбита", ("shipped", "post", ""), True),
            ("курьер без трека проходит", ("shipped", "courier", ""), False),
        ]:
            try:
                validate_note(*args)
                check(label, not should_fail, "прошло")
            except ValueError:
                check(label, should_fail, "отбито")
        check("причина режется до 200", len(validate_note("cancelled", "pickup", "х" * 250)) == 200)
        check("трек режется до 64 и обрезается по краям",
              validate_note("shipped", "post", "  " + "A" * 70) == "A" * 64)

        print("\n[6] Уведомления покупателю")
        bot = FakeBot()
        o = await make_order(s, prod, "pickup")
        for frm, to in [("awaiting_payment", "paid"), ("paid", "assembled"),
                        ("assembled", "ready_for_pickup"), ("ready_for_pickup", "delivered")]:
            o, entry = await apply_transition(s, o.id, frm, to, actor="admin", actor_id=1)
            await notify_customer_status(bot, s, o, entry)
        check("по одному сообщению на переход", len(bot.sent) == 4, str(len(bot.sent)))
        ready = bot.sent[2][1]
        check("«готов к выдаче» содержит адрес и часы",
              "ул. Тестовая, 1" in ready and "10-19" in ready, ready)
        check("все сообщения — покупателю", all(cid == USER for cid, _ in bot.sent))

        o = await make_order(s, prod, "post")
        await force_status(s, o.id, "assembled")
        o, entry = await apply_transition(s, o.id, "assembled", "shipped", actor="admin", note="RA1 2345")
        check("трек в сообщении почты", "RA1 2345" in status_message(o, entry), status_message(o, entry))
        o, entry = await apply_transition(s, o.id, "shipped", "cancelled", actor="admin", note="брак")
        check("причина в сообщении об отмене", "брак" in status_message(o, entry))
        check("html в причине экранируется",
              "&lt;b&gt;x" in status_message(o, (await apply_transition(
                  s, (await make_order(s, prod)).id, "awaiting_payment", "cancelled",
                  actor="admin", note="<b>x</b>"))[1]))

        print("\n[7] Отказ доставки не блокирует переход")
        bot.blocked = True
        o = await make_order(s, prod, "courier")
        o, entry = await apply_transition(s, o.id, "awaiting_payment", "paid", actor="admin", actor_id=1)
        delivered = await notify_customer_status(bot, s, o, entry)
        fresh = await repo.order(s, o.id)
        last = (await repo.history_for(s, o.id))[-1]
        check("notify failure keeps transition", str(fresh.status) == "paid" and delivered is False)
        check("в истории notified=False", last.notified is False)
        bot.blocked = False

        print("\n[8] Самоотмена покупателем")
        o = await make_order(s, prod)
        kb = [b.text for row in customer_order_kb(o).inline_keyboard for b in row]
        check("у нового заказа есть кнопка отмены", any("Отменить" in l for l in kb), str(kb))
        o, entry = await apply_transition(s, o.id, str(o.status), "cancelled",
                                          actor="customer", actor_id=USER, note=CUSTOMER_CANCEL_NOTE)
        check("отменён покупателем с фиксированной причиной",
              entry.actor == "customer" and entry.note == CUSTOMER_CANCEL_NOTE)
        o = await make_order(s, prod)
        await force_status(s, o.id, "paid")
        o = await repo.order(s, o.id)
        kb = [b.text for row in customer_order_kb(o).inline_keyboard for b in row]
        check("у оплаченного кнопки отмены нет", not any("Отменить" in l for l in kb), str(kb))
        check("customer cancel from paid rejected", "paid" not in CUSTOMER_MAY_CANCEL_FROM)

        print("\n[9] Заказы до внедрения: history seeded")
        legacy = await make_order(s, prod)
        await s.execute(text("DELETE FROM order_status_history WHERE order_id = :id"), {"id": legacy.id})
        await s.commit()
    await init_db()
    async with session_factory() as s:
        hist = await repo.history_for(s, legacy.id)
        check("legacy orders: history seeded",
              len(hist) == 1 and hist[0].actor == "system" and "миграции" in hist[0].note,
              str([(h.actor, h.note) for h in hist]))
        await init_db()
        check("повторный init_db не дублирует", len(await repo.history_for(s, legacy.id)) == 1)

        print("\n[10] Автоотмена неоплаченных")
        bot = FakeBot()
        stale = (await make_order(s, limited, qty=2)).id
        fresh_o = (await make_order(s, prod)).id
        unlimited = (await make_order(s, prod)).id
        paid_o = (await make_order(s, prod)).id
        await force_status(s, paid_o, "paid")
        long_ago = utcnow() - timedelta(hours=2)
        for oid in (stale, unlimited, paid_o):
            await s.execute(update(Order).where(Order.id == oid).values(created_at=long_ago))
        await s.commit()
        check("остаток списан при заказе", (await s.get(Product, limited)).stock == 1)

        async def notify(order, entry):
            await notify_customer_status(bot, s, order, entry)

        check("ttl=0 disables", await expire_unpaid(s, 0, notify=notify) == [])
        done = await expire_unpaid(s, 60, notify=notify)
        ids = {o.id for o in done}
        check("stale cancelled", stale in ids and unlimited in ids, str(ids))
        check("fresh kept", fresh_o not in ids)
        check("paid untouched", paid_o not in ids and str((await repo.order(s, paid_o)).status) == "paid")
        s.expire_all()
        check("stock returned", (await s.get(Product, limited)).stock == 3,
              str((await s.get(Product, limited)).stock))
        check("unlimited stock stays -1", (await s.get(Product, prod)).stock == -1)
        last = (await repo.history_for(s, stale))[-1]
        check("инициатор система, причина «не оплачен в срок»",
              last.actor == "system" and last.note == UNPAID_NOTE, f"{last.actor} / {last.note}")
        check("покупатель уведомлён об автоотмене",
              any(UNPAID_NOTE in t for _, t in bot.sent), str(bot.sent))
        check("повторный проход ничего не находит", await expire_unpaid(s, 60) == [])
        check("AUTO_CANCEL_FROM — только неоплаченные", AUTO_CANCEL_FROM <= {"new", "awaiting_payment"})

    print(f"\n{'=' * 40}\nOK: {ok}   FAIL: {fail}\n{'=' * 40}")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
