"""Кнопки заказа в чатах админов следуют за статусом, откуда бы его ни сменили.

Живой баг 16.10: заказ отменили в веб-панели, а в чате под уведомлением так и
висели «Оплачен / Отменить». Проверяем все входы: панель, кнопка бота (своё
сообщение не трогаем), отмена покупателем, автоотмена, сломанная правка.

Запуск: .venv\\Scripts\\python.exe admin_sync_test.py
"""
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///admin_sync_test.db"
os.environ["SEED"] = "plain"
os.environ["ADMIN_IDS"] = "111,222"
os.environ["UNPAID_TTL_MINUTES"] = "30"

from sqlalchemy import update  # noqa: E402

from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import Order, Product  # noqa: E402
from bot.utils.admin_ops import admin_transition  # noqa: E402
from bot.utils.lifecycle import CUSTOMER_CANCEL_NOTE, apply_transition  # noqa: E402
from bot.utils.notify import notify_admins_new_order, notify_admins_order_cancelled  # noqa: E402
from bot.utils.sweeper import sweep_once  # noqa: E402

DB_FILE = Path("admin_sync_test.db")
USER = 424000888

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
    """send_message выдаёт номера сообщений, edit_message_reply_markup копит правки."""

    def __init__(self) -> None:
        self.next_id = 1000
        self.sent: list[tuple[int, int]] = []
        self.edits: dict[tuple[int, int], object] = {}
        self.broken: set[tuple[int, int]] = set()

    async def send_message(self, chat_id: int, text: str, **kw):
        self.next_id += 1
        self.sent.append((chat_id, self.next_id))
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup=None):
        if (chat_id, message_id) in self.broken:
            raise RuntimeError("Bad Request: message to edit not found")
        self.edits[(chat_id, message_id)] = reply_markup


def actions(markup) -> set[str]:
    if markup is None:
        return set()
    return {b.callback_data for row in markup.inline_keyboard for b in row}


async def make_product(s, stock: int = 50) -> Product:
    cat = await repo.add_category(s, "Тест")
    product = Product(title="Тестовые часы", price=100, stock=stock, category_id=cat.id)
    s.add(product)
    await s.commit()
    return product


async def new_order(s, bot: FakeBot) -> Order:
    product = await make_product(s)
    await repo.cart_add(s, USER, product.id, 1)
    order = await repo.create_order(s, USER, "Тест", "+70000000000", "адрес", "", delivery="post")
    await notify_admins_new_order(bot, order, session=s)
    return order


async def main() -> None:
    DB_FILE.unlink(missing_ok=True)
    await init_db()

    print("\n[1] Уведомление запоминается у каждого админа")
    bot = FakeBot()
    async with session_factory() as s:
        order = await new_order(s, bot)
        msgs = await repo.admin_messages(s, order.id)
        check("два сообщения записаны", len(msgs) == 2, str(len(msgs)))
        check("номера совпадают с отправленными",
              {(m.chat_id, m.message_id) for m in msgs} == set(bot.sent), str(bot.sent))
        msg1, msg2 = bot.sent

        print("\n[2] Смена статуса из панели обновляет все копии")
        order, _, _ = await admin_transition(s, bot, 111, order.id, str(order.status), "paid",
                                             source="веб-панель")
        check("правка у первого админа", msg1 in bot.edits)
        check("правка у второго админа", msg2 in bot.edits)
        acts = actions(bot.edits.get(msg1))
        check("кнопки уже для «оплачен»: есть «собран»", any("assembled" in a for a in acts), str(acts))
        # callback_data: aord:<id>:<куда>:<откуда>
        check("кнопки «оплачен» больше нет", all(a.split(":")[2] != "paid" for a in acts), str(acts))
        check("кнопки привязаны к новому статусу", all(a.split(":")[3] == "paid" for a in acts), str(acts))

        print("\n[3] Кнопка бота: своё сообщение не трогаем, второе правим")
        bot.edits.clear()
        order, _, _ = await admin_transition(s, bot, 111, order.id, "paid", "assembled", skip=msg1)
        check("нажатое сообщение пропущено", msg1 not in bot.edits)
        check("копия у второго админа обновлена", msg2 in bot.edits)

        print("\n[4] Сломанная правка не роняет переход")
        bot.edits.clear()
        bot.broken = {msg1}
        order, _, _ = await admin_transition(s, bot, 111, order.id, "assembled", "shipped", note="RA1")
        check("статус сменился", str(order.status) == "shipped", str(order.status))
        check("вторая копия всё равно обновлена", msg2 in bot.edits)
        bot.broken = set()

        print("\n[5] Финальный статус снимает кнопки и забывает сообщения")
        bot.edits.clear()
        order, _, _ = await admin_transition(s, bot, 222, order.id, "shipped", "cancelled", note="брак")
        check("у первого кнопок нет", msg1 in bot.edits and bot.edits[msg1] is None)
        check("у второго кнопок нет", msg2 in bot.edits and bot.edits[msg2] is None)
        check("записи удалены", await repo.admin_messages(s, order.id) == [])

    print("\n[6] Отмена покупателем")
    bot = FakeBot()
    async with session_factory() as s:
        order = await new_order(s, bot)
        order, _ = await apply_transition(s, order.id, str(order.status), "cancelled",
                                          actor="customer", actor_id=USER, note=CUSTOMER_CANCEL_NOTE)
        await notify_admins_order_cancelled(bot, order, source="чат", session=s)
        check("обе копии без кнопок",
              all(bot.edits.get(m, "нет") is None for m in bot.sent[:2]), str(bot.edits))

    print("\n[7] Автоотмена неоплаченного")
    bot = FakeBot()
    async with session_factory() as s:
        order = await new_order(s, bot)
        old = order.created_at - timedelta(hours=2)
        await s.execute(update(Order).where(Order.id == order.id).values(created_at=old))
        await s.commit()
    n = await sweep_once(bot)
    check("заказ отменён свипером", n >= 1, str(n))
    check("кнопки сняты в обеих копиях",
          all(bot.edits.get(m, "нет") is None for m in bot.sent[:2]), str(bot.edits))

    print("\n[8] Без сессии уведомление по-прежнему уходит")
    bot = FakeBot()
    async with session_factory() as s:
        product = await make_product(s, stock=5)
        await repo.cart_add(s, USER, product.id, 1)
        order = await repo.create_order(s, USER, "Тест", "+7", "адрес", "", delivery="post")
        await notify_admins_new_order(bot, order)
        check("оба админа получили", len(bot.sent) == 2, str(bot.sent))
        check("ничего не записано", await repo.admin_messages(s, order.id) == [])

    print("\n" + "=" * 46)
    print(f"OK: {ok}   FAIL: {fail}")
    print("=" * 46)
    DB_FILE.unlink(missing_ok=True)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
