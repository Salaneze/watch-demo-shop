"""Напоминание о брошенной корзине: одно, вовремя, не ночью и не тем, кто отказался.

Запуск: .venv\\Scripts\\python.exe cart_nudge_test.py
"""
import asyncio
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///cart_nudge_test.db"
os.environ["SEED"] = "shop"
os.environ["SHOP_TZ"] = "Europe/Moscow"
os.environ["CART_REMIND_MINUTES"] = "45"
os.environ["WEBAPP_URL"] = "https://shop.example"

from sqlalchemy import select, update  # noqa: E402

from bot.config import settings  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import CartNudge, Product  # noqa: E402
from bot.keyboards.callbacks import CartCB  # noqa: E402
from bot.keyboards.common import cart_nudge_kb  # noqa: E402
from bot.utils.cart_nudge import nudge_carts_once  # noqa: E402
from bot.utils.seed import seed_if_empty  # noqa: E402

DB_FILE = pathlib.Path("cart_nudge_test.db")
ok = fail = 0
# 12:00 по Москве — день, напоминать можно; 23:30 — ночь
NOON = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 10, 26, 20, 30, tzinfo=timezone.utc)


def check(label: str, cond: bool, extra: object = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label} — {extra}")


class SpyBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.blocked: set[int] = set()

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text, reply_markup))


async def touched(s, user_id: int, at: datetime) -> None:
    await s.execute(update(CartNudge).where(CartNudge.user_id == user_id).values(touched_at=at))
    await s.commit()


async def run(bot: SpyBot, now: datetime) -> list[int]:
    before = len(bot.sent)
    async with session_factory() as s:
        await nudge_carts_once(bot, s, now=now)
    return [chat for chat, _, _ in bot.sent[before:]]


async def main() -> None:
    DB_FILE.unlink(missing_ok=True)
    await init_db()
    bot = SpyBot()
    async with session_factory() as s:
        await seed_if_empty(s)
        pids = list(await s.scalars(select(Product.id).where(Product.is_active).order_by(Product.id)))

    print("\n[1] Основной путь")
    async with session_factory() as s:
        await repo.cart_add(s, 1, pids[0], 2)
        nudge = await s.get(CartNudge, 1)
        check("корзина отметила время", nudge is not None and not nudge.reminded)
        await touched(s, 1, NOON - timedelta(minutes=30))
    check("30 мин тишины — рано", await run(bot, NOON) == [])
    async with session_factory() as s:
        await touched(s, 1, NOON - timedelta(minutes=50))
    check("50 мин — напомнили", await run(bot, NOON) == [1], bot.sent)
    text, kb = bot.sent[-1][1], bot.sent[-1][2]
    check("в тексте товар и итог", "× 2" in text and "Итого" in text, text)
    check("без скидок и срочности", not any(w in text.lower() for w in ("скидк", "успей", "осталось")), text)
    check("второй раз — нет", await run(bot, NOON + timedelta(hours=1)) == [])

    print("\n[2] Одно напоминание на корзину")
    async with session_factory() as s:
        await repo.cart_add(s, 1, pids[1], 1)
        await touched(s, 1, NOON)
    check("докинул товар после напоминания — не напоминаем снова",
          await run(bot, NOON + timedelta(hours=2)) == [])
    async with session_factory() as s:
        await repo.create_order(s, 1, "Тест", "+70000000000", "ул. Тестовая, 1", "")
        await repo.cart_add(s, 1, pids[2], 1)
        await touched(s, 1, NOON)
    check("после заказа новая корзина — снова одно", await run(bot, NOON + timedelta(hours=1)) == [1])
    async with session_factory() as s:
        await repo.cart_clear(s, 1)
        await repo.cart_add(s, 1, pids[0], 1)
        await touched(s, 1, NOON)
    check("после очистки — тоже", await run(bot, NOON + timedelta(hours=1)) == [1])
    async with session_factory() as s:
        await repo.cart_add(s, 1, pids[0], -1)  # убрал последний товар кнопкой «➖»
        await repo.cart_add(s, 1, pids[1], 1)
        await touched(s, 1, NOON)
    check("опустошил «➖» и собрал заново — тоже", await run(bot, NOON + timedelta(hours=1)) == [1])

    print("\n[3] Когда молчим")
    async with session_factory() as s:
        await repo.cart_add(s, 2, pids[0], 1)
        await touched(s, 2, NIGHT - timedelta(hours=1))
    check("ночью — молчим", await run(bot, NIGHT) == [])
    check("утром — догоняем", await run(bot, NIGHT + timedelta(hours=10)) == [2])

    async with session_factory() as s:
        await repo.cart_add(s, 3, pids[0], 1)
        await touched(s, 3, NOON - timedelta(hours=30))
    check("корзина старше суток — молчим", await run(bot, NOON) == [])

    async with session_factory() as s:
        await repo.cart_add(s, 4, pids[0], 1)
        await repo.cart_nudge_opt_out(s, 4)
        await touched(s, 4, NOON - timedelta(hours=1))
    check("нажал «Не напоминать» — молчим", await run(bot, NOON) == [])
    async with session_factory() as s:
        await repo.cart_clear(s, 4)
        await repo.cart_add(s, 4, pids[0], 1)
        await touched(s, 4, NOON - timedelta(hours=1))
    check("отказ переживает новую корзину", await run(bot, NOON) == [])

    async with session_factory() as s:
        await repo.cart_add(s, 5, pids[3], 1)
        await touched(s, 5, NOON - timedelta(hours=1))
        await s.execute(update(Product).where(Product.id == pids[3]).values(is_active=False))
        await s.commit()
    check("всё в корзине снято с продажи — молчим", await run(bot, NOON) == [])
    async with session_factory() as s:
        await s.execute(update(Product).where(Product.id == pids[3]).values(is_active=True))
        await s.commit()
    check("и не возвращаемся к ней каждую минуту", await run(bot, NOON + timedelta(minutes=1)) == [])

    print("\n[4] Сбой отправки")
    async with session_factory() as s:
        await repo.cart_add(s, 6, pids[0], 1)
        await touched(s, 6, NOON - timedelta(hours=1))
    bot.blocked.add(6)
    check("заблокировал бота — цикл не падает", await run(bot, NOON) == [])
    bot.blocked.clear()
    check("и не долбим повторно", await run(bot, NOON + timedelta(minutes=1)) == [])

    print("\n[5] Выключатель и кнопки")
    async with session_factory() as s:
        await repo.cart_add(s, 7, pids[0], 1)
        await touched(s, 7, NOON - timedelta(hours=1))
    settings.cart_remind_minutes = 0
    check("CART_REMIND_MINUTES=0 — молчим", await run(bot, NOON) == [])
    settings.cart_remind_minutes = 45
    check("включили обратно — пришло", await run(bot, NOON) == [7])

    rows = [[b for b in r] for r in cart_nudge_kb().inline_keyboard]
    flat = [b for r in rows for b in r]
    check("есть витрина как Mini App", any(b.web_app and b.web_app.url == "https://shop.example" for b in flat), rows)
    check("есть отписка", any(b.callback_data == CartCB(action="mute").pack() for b in flat), rows)

    DB_FILE.unlink(missing_ok=True)
    print(f"\n{'=' * 40}\nOK: {ok}   FAIL: {fail}")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
