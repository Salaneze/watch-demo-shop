"""Проверка сборки бота без обращения к Telegram: роутеры, фильтры, порядок хендлеров."""
import os

os.environ.setdefault("BOT_TOKEN", "123456:AAHtest-token-placeholder_0000000000")
os.environ["ADMIN_IDS"] = "111,222"
os.environ["DB_URL"] = "sqlite+aiosqlite:///wiring.db"

from aiogram import Dispatcher  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from bot.config import settings  # noqa: E402
from bot.handlers import setup_routers  # noqa: E402
from bot.middlewares.db import DbSessionMiddleware  # noqa: E402

ok, fail = 0, 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label} {extra}")


print("\n[1] Конфиг")
check("парсинг ADMIN_IDS", settings.admins == {111, 222}, settings.admins)

import logging  # noqa: E402

from bot.utils.notify import check_admins  # noqa: E402


def errors_of(bot_id: int, admins: set[int]) -> list[str]:
    """Что скажет проверка админов в лог при таком наборе."""
    saved = settings.admin_ids
    settings.admin_ids = ",".join(str(a) for a in admins)
    logger = logging.getLogger("bot.utils.notify")
    caught: list[str] = []

    class Catch(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                caught.append(record.getMessage())

    h = Catch()
    logger.addHandler(h)
    try:
        check_admins(bot_id)
    finally:
        logger.removeHandler(h)
        settings.admin_ids = saved
    return caught


print("\n[1.1] Проверка ADMIN_IDS при старте")
check("id бота в админах — ошибка в лог", len(errors_of(777, {777})) == 1)
check("пустой ADMIN_IDS — ошибка в лог", len(errors_of(777, set())) == 1)
check("нормальный админ — молчит", errors_of(777, {111}) == [])

print("\n[2] Сборка диспетчера")
dp = Dispatcher(storage=MemoryStorage())
dp.update.middleware(DbSessionMiddleware())
root = setup_routers()
dp.include_router(root)

names = [r.name for r in root.sub_routers]
# ai — последним намеренно: в диалоге с консультантом кнопки меню должны
# перехватываться роутерами выше.
check("все роутеры подключены", names == ["common", "catalog", "cart", "checkout", "admin", "ai"], names)

counts = {r.name: (len(r.message.handlers), len(r.callback_query.handlers)) for r in root.sub_routers}
for name, (m, c) in counts.items():
    print(f"       {name}: message={m}, callback={c}")
check("в каждом роутере есть хендлеры", all(m + c > 0 for m, c in counts.values()), counts)

print("\n[3] Приоритет: чек не перехватывает админское фото")
checkout_router = next(r for r in root.sub_routers if r.name == "checkout")
receipt = checkout_router.message.handlers[-1]
has_state_filter = any(
    "StateFilter" in type(f.callback).__name__ for f in receipt.filters
)
check("на receipt висит StateFilter", has_state_filter, [type(f.callback).__name__ for f in receipt.filters])

print("\n[4] Админский роутер закрыт фильтром")
import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

admin_router = next(r for r in root.sub_routers if r.name == "admin")


async def passes(observer, user_id: int) -> bool:
    fake = SimpleNamespace(from_user=SimpleNamespace(id=user_id), text="/orders")
    # check_root_filters возвращает (passed, data) — bool(кортежа) всегда True,
    # поэтому берём первый элемент, а не сам результат.
    passed, _ = await observer.check_root_filters(fake)
    return passed


check("админ (111) проходит message", asyncio.run(passes(admin_router.message, 111)))
check("чужой (999) не проходит message", not asyncio.run(passes(admin_router.message, 999)))
check("админ (222) проходит callback", asyncio.run(passes(admin_router.callback_query, 222)))
check("чужой (999) не проходит callback", not asyncio.run(passes(admin_router.callback_query, 999)))

print("\n[5] Callback-фабрики паковка/распаковка")
from bot.keyboards.callbacks import AdminOrderCB, CartCB, CategoryCB, ProductCB  # noqa: E402

packed = CartCB(action="inc", product_id=7).pack()
check("CartCB round-trip", CartCB.unpack(packed).product_id == 7, packed)
check("CategoryCB round-trip", CategoryCB.unpack(CategoryCB(id=3).pack()).id == 3)
check("ProductCB round-trip", ProductCB.unpack(ProductCB(id=5, category_id=2).pack()).category_id == 2)
cb = AdminOrderCB(order_id=9, action="paid", from_status="awaiting_payment")
check("AdminOrderCB round-trip", AdminOrderCB.unpack(cb.pack()).from_status == "awaiting_payment")
check("длина callback_data < 64 байт", len(packed.encode()) < 64, len(packed.encode()))
# Самая длинная пара статусов с шестизначным номером заказа — предел Telegram 64 байта.
longest = AdminOrderCB(order_id=999999, action="ready_for_pickup", from_status="awaiting_payment").pack()
check("самый длинный AdminOrderCB влезает в 64 байта", len(longest.encode()) <= 64, len(longest.encode()))

print(f"\n{'=' * 40}\nOK: {ok}   FAIL: {fail}\n{'=' * 40}")
raise SystemExit(1 if fail else 0)
