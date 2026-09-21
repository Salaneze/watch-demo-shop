"""Чат и витрина продают на одних условиях.

Повод для файла: модель заказа обросла доставкой и промокодом, а довели до неё
только Mini App. Заказ из чата молча уходил самовывозом без скидки, и никакой
из 205 зелёных тестов этого не видел — они проверяли витрину.

Поэтому здесь FSM оформления гоняется целиком, через настоящие хендлеры с
подставными Message и CallbackQuery, а итог сверяется с тем, что посчитала бы
витрина на той же корзине.

Запуск: .venv\\Scripts\\python.exe chat_parity_test.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///chat_parity_test.db"
os.environ["SEED"] = "plain"
os.environ["CURRENCY"] = "USD"
os.environ["DELIVERY_COURIER"] = "9"
os.environ["DELIVERY_POST"] = "15"

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from api.routes import _order_out  # noqa: E402
from bot.handlers import checkout, common  # noqa: E402
from bot.keyboards.callbacks import CustomerOrderCB  # noqa: E402
from bot.utils.lifecycle import apply_transition  # noqa: E402
from bot.handlers.catalog import CAPTION_LIMIT, product_caption  # noqa: E402
from bot.states import Checkout  # noqa: E402
from bot.utils.notify import order_text  # noqa: E402

DB_FILE = Path("chat_parity_test.db")

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


class FakeUser:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.full_name = "Test Buyer"
        self.first_name = "Test"


class FakeMessage:
    """Столько от aiogram.Message, сколько трогают хендлеры оформления."""

    def __init__(self, text: str = "", uid: int = 700100200) -> None:
        self.text = text
        self.from_user = FakeUser(uid)
        self.sent: list[str] = []

    async def answer(self, text: str, reply_markup=None) -> "FakeMessage":
        self.sent.append(text)
        return self

    async def edit_text(self, text: str, reply_markup=None) -> "FakeMessage":
        self.sent.append(text)
        return self

    @property
    def last(self) -> str:
        return self.sent[-1] if self.sent else ""

    @property
    def all_text(self) -> str:
        return "\n".join(self.sent)


class FakeBot:
    """Ловит то, что ушло бы админам: уведомление о заказе проверяется целиком."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        self.messages.append(text)


class FakeCall:
    def __init__(self, data: str, message: FakeMessage) -> None:
        self.data = data
        self.message = message
        self.from_user = message.from_user
        self.alerts: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        if text:
            self.alerts.append(text)


def make_state(storage: MemoryStorage, uid: int) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=0, chat_id=uid, user_id=uid))


async def fill_cart(s, uid: int, product_id: int, qty: int) -> None:
    await repo.cart_clear(s, uid)
    await repo.cart_add(s, uid, product_id, qty)


async def walk_checkout(s, state: FSMContext, uid: int, delivery: str, promo: str,
                        msg: FakeMessage) -> None:
    """Проходит форму до превью: имя, телефон, адрес, доставка, промокод, комментарий."""
    await checkout.step_name(FakeMessage("Иван", uid), state)
    await checkout.step_phone(FakeMessage("+7 900 123 45 67", uid), state)
    await checkout.step_address(FakeMessage("Ленина 1", uid), state)
    await checkout.step_delivery(FakeCall(f"checkout:dlv:{delivery}", msg), state)
    if promo:
        await checkout.step_promo(FakeMessage(promo, uid), state)
    else:
        await checkout.skip_promo(FakeCall("checkout:nopromo", msg), state)
    await checkout.step_comment(msg_with(msg, "-"), state, s)


def msg_with(msg: FakeMessage, text: str) -> FakeMessage:
    """Тот же собиратель ответов, но с новым текстом от покупателя."""
    msg.text = text
    return msg


async def run() -> None:
    if DB_FILE.exists():
        DB_FILE.unlink()
    await init_db()
    async with session_factory() as s:
        from bot.utils.seed import seed_if_empty

        await seed_if_empty(s)

    storage = MemoryStorage()

    async with session_factory() as s:
        product = (await repo.products_in_category(s, 1))[0]
        price = product.price

        print("\n[1] Заказ из чата: курьер и промокод доезжают до заказа")
        uid = 700100200
        await fill_cart(s, uid, product.id, 2)
        state = make_state(storage, uid)
        msg = FakeMessage(uid=uid)
        await walk_checkout(s, state, uid, "courier", "WELCOME10", msg)

        check("превью показывает разбивку", "К оплате" in msg.all_text and "Товары" in msg.all_text,
              msg.last)
        check("превью назвало доставку", "Курьер" in msg.all_text, msg.all_text)

        data = await state.get_data()
        expected = price * 2 - (price * 2) * 10 // 100 + 9
        check("превью посчитало итог с доставкой и скидкой", data["shown_total"] == expected,
              f"{data['shown_total']} != {expected}")

        bot = FakeBot()
        await checkout.confirm_ok(FakeCall("checkout:confirm", msg), state, s, bot)
        order = (await repo.orders_by_user(s, uid, limit=1))[0]
        admin_text = "\n".join(bot.messages)
        check("админ получил уведомление", bool(bot.messages))
        check("админу видна разбивка", "Курьер" in admin_text and "Скидка" in admin_text,
              admin_text)
        check("способ доставки уехал в заказ", order.delivery_method == "courier",
              order.delivery_method)
        check("доставка оплачена", order.delivery_cost == 9, str(order.delivery_cost))
        check("скидка применена", order.discount == (price * 2) * 10 // 100, str(order.discount))
        check("промокод записан", order.promo_code == "WELCOME10", order.promo_code)
        check("итог сходится", order.total == expected, f"{order.total} != {expected}")
        # Регресс-маркер: ровно так выглядел заказ из чата до правки.
        check("самовывоз без скидки больше не подставляется",
              not (order.delivery_method == "pickup" and order.discount == 0))

        print("\n[2] Чат и витрина считают одинаково")
        uid2 = 700100201
        await fill_cart(s, uid2, product.id, 2)
        quote = await repo.cart_quote(s, uid2, delivery="courier", promo_code="WELCOME10")
        check("превью чата равно расчёту витрины", quote.total == expected,
              f"{quote.total} != {expected}")
        webapp_order = await repo.create_order(
            s, user_id=uid2, name="Пётр", phone="+79001234567", address="Мира 2",
            comment="", expected_total=quote.total, delivery="courier", promo_code="WELCOME10",
        )
        check("заказ из витрины совпадает с заказом из чата",
              (webapp_order.total, webapp_order.discount, webapp_order.delivery_cost)
              == (order.total, order.discount, order.delivery_cost))

        print("\n[3] Уведомление админу несёт разбивку и переживает злой ввод")
        text = order_text(order, for_admin=True)
        check("в тексте есть сумма товаров", "Товары:" in text, text)
        check("в тексте есть скидка", "Скидка" in text, text)
        check("в тексте есть доставка", "Доставка" in text, text)

        await repo.add_promo(s, "<b>EVIL</b>", 50)
        uid3 = 700100202
        await fill_cart(s, uid3, product.id, 1)
        q3 = await repo.cart_quote(s, uid3, delivery="post", promo_code="<b>EVIL</b>")
        evil_order = await repo.create_order(
            s, user_id=uid3, name="Злой", phone="+79001234567", address="Тьмы 3",
            comment="", expected_total=q3.total, delivery="post", promo_code="<b>EVIL</b>",
        )
        # Код хранится в верхнем регистре — resolve_promo нормализует ввод.
        evil_text = order_text(evil_order, for_admin=True)
        check("промокод с разметкой экранирован", "&lt;B&gt;EVIL" in evil_text, evil_text)
        check("сырого тега в уведомлении нет", "<B>EVIL</B>" not in evil_text, evil_text)

        print("\n[4] Испорченный выбор доставки не создаёт заказ")
        uid4 = 700100203
        await fill_cart(s, uid4, product.id, 1)
        state4 = make_state(storage, uid4)
        msg4 = FakeMessage(uid=uid4)
        await checkout.step_name(FakeMessage("Кто-то", uid4), state4)
        await checkout.step_phone(FakeMessage("+7 900 123 45 67", uid4), state4)
        await checkout.step_address(FakeMessage("Адрес", uid4), state4)
        call4 = FakeCall("checkout:dlv:teleport", msg4)
        await checkout.step_delivery(call4, state4)
        check("неизвестный способ отбит", bool(call4.alerts), str(call4.alerts))
        check("покупатель остался на выборе доставки",
              await state4.get_state() == Checkout.delivery.state,
              str(await state4.get_state()))

        print("\n[5] Промокод не ломает базу длиной")
        uid5 = 700100204
        state5 = make_state(storage, uid5)
        await state5.set_state(Checkout.promo)
        await checkout.step_promo(FakeMessage("X" * 200, uid5), state5)
        saved = (await state5.get_data())["promo_code"]
        check("длинный промокод обрезан по колонке", len(saved) <= checkout.PROMO_MAX,
              str(len(saved)))

        print("\n[6] Карточка товара в чате")
        cap = product_caption(product)
        check("цена на месте", "$" in cap, cap)
        specs = [r for r in (product.specs or []) if isinstance(r, (list, tuple)) and len(r) == 2]
        if specs:
            check("характеристики показаны", str(specs[0][1]) in cap, cap)
        product.stock = 3
        check("остаток показан", "Осталось 3" in product_caption(product), product_caption(product))
        product.stock = 0
        check("нулевой остаток назван", "Нет в наличии" in product_caption(product))
        product.stock = -1
        check("безлимитный остаток не упоминается", "Осталось" not in product_caption(product))

        product.description = "Ох & <ужас> " * 300
        long_cap = product_caption(product)
        check("длинная подпись влезает в лимит", len(long_cap) <= CAPTION_LIMIT, str(len(long_cap)))
        check("обрезка не рассекла сущность", "&am" not in long_cap.replace("&amp;", ""),
              long_cap[-40:])
        check("угловые скобки описания экранированы", "<ужас>" not in long_cap)

        product.specs = [["Материал", "сталь"], "мусор", ["Диаметр"], ["Вода", "100 м"]]
        product.description = "коротко"
        broken = product_caption(product)
        check("кривая характеристика не роняет карточку", "Материал" in broken and "Вода" in broken,
              broken)

        print("\n[7] Регресс: чек об оплате доходит до админа")
        check("admin_order_kb импортирован в checkout",
              getattr(checkout, "admin_order_kb", None) is not None)

        print("\n[8] Заказы: чат и витрина показывают один статус и одну историю")
        # Заказ из [1] переводим админом, потом смотрим глазами покупателя в обоих входах.
        moved, _ = await apply_transition(s, order.id, str(order.status), "paid",
                                          actor="admin", actor_id=1)
        chat_msg = FakeMessage("📦 Мои заказы", uid)
        await common.my_orders(chat_msg, s)
        api_view = _order_out(moved, await repo.history_for(s, moved.id))
        check("статус в чате = status_text витрины", api_view.status_text in chat_msg.all_text,
              chat_msg.all_text)
        hist_call = FakeCall("cord:history", chat_msg)
        await common.order_history(hist_call, CustomerOrderCB(order_id=moved.id, action="history"), s)
        chat_hist = chat_msg.last
        check("история в чате той же длины, что в API",
              chat_hist.count("\n") == len(api_view.history), chat_hist)
        check("последняя запись совпадает",
              api_view.history[-1].status_text in chat_hist.splitlines()[-1], chat_hist)
        foreign = FakeCall("cord:history", FakeMessage("", 700100299))
        await common.order_history(foreign, CustomerOrderCB(order_id=moved.id, action="history"), s)
        check("чужая история из чата не раскрывается", "не найден" in " ".join(foreign.alerts),
              str(foreign.alerts))

    print("\n" + "=" * 46)
    print(f"OK: {ok}   FAIL: {fail}")
    print("=" * 46)
    if DB_FILE.exists():
        DB_FILE.unlink()
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(run())
