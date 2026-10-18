"""Проверка на настоящем Postgres перед переездом с SQLite.

Остальные тесты гоняют SQLite, а он прощает то, на чём Postgres падает:
длинные строки в String(n), 1 вместо TRUE, число больше int4. Здесь те же
пути проходят по боевому драйверу.

Строку подключения берёт из .env.pg (DB_URL=postgresql://...), в вывод её
не печатает. Работает в одноразовой схеме pgcheck_<случайное>, в конце её
удаляет — таблицы магазина в той же базе не трогает.

Запуск: .venv\\Scripts\\python.exe pg_check.py
"""
import asyncio
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
# Модульный engine приложения не нужен, подсовываем пустой SQLite в памяти.
os.environ["DB_URL"] = "sqlite+aiosqlite://"

from sqlalchemy import event, text, update  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from bot.db import repo  # noqa: E402
from bot.db.base import engine_args, init_db, is_bad_value  # noqa: E402
from bot.db.models import Order, Product  # noqa: E402
from bot.utils.lifecycle import apply_transition, expire_unpaid  # noqa: E402

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


def read_url() -> str:
    env = Path(".env.pg")
    if not env.exists():
        sys.exit("Нет .env.pg — положи туда строку DB_URL=postgresql://... из Neon")
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("DB_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("В .env.pg нет строки DB_URL=")


async def main() -> None:
    raw = read_url()
    url, kw = engine_args(raw)
    check("строка превращена в asyncpg", url.startswith("postgresql+asyncpg://"), url.split("://")[0])
    schema = "pgcheck_" + secrets.token_hex(4)
    eng = create_async_engine(url, **kw)

    # search_path через server_settings прокси Neon молча выбрасывает — первый
    # прогон так и насоздавал таблиц в public. Ставим его SQL-командой на каждом
    # новом соединении пула.
    @event.listens_for(eng.sync_engine, "connect")
    def _search_path(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute(f'SET search_path TO "{schema}"')
        cur.close()

    async with eng.begin() as c:
        await c.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        where = (await c.execute(text("SELECT current_schema()"))).scalar()
    check("работаем в своей схеме, не в public", where == schema, str(where))
    if where != schema:
        await eng.dispose()
        sys.exit("Схема не переключилась — дальше не иду, иначе таблицы лягут в public")
    print(f"схема {schema}")
    try:
        await run(eng)
    finally:
        async with eng.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await eng.dispose()
        print(f"схема {schema} удалена")

    print("\n" + "=" * 46)
    print(f"OK: {ok}   FAIL: {fail}")
    print("=" * 46)
    sys.exit(1 if fail else 0)


async def run(eng) -> None:
    sf = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    print("\n[1] Таблицы и стартовая миграция истории")
    await init_db(eng)
    await init_db(eng)
    check("init_db дважды подряд без ошибок", True)

    async with sf() as s:
        print("\n[2] Длинные строки режутся, а не роняют запрос")
        cat = await repo.add_category(s, "Ремешки " + "я" * 300)
        check("категория 300+ символов сохранилась", len(cat.title) == 128, str(len(cat.title)))
        p = Product(title="Тест " * 60, description="Кожаный РЕМЕШОК для часов", price=100,
                    stock=5, category_id=cat.id)
        s.add(p)
        await s.commit()
        check("название товара обрезано по колонке", len(p.title) == 128, str(len(p.title)))
        pid = p.id

        print("\n[3] Заказ с телеграм-id больше int4")
        user = 7_123_456_789
        await repo.cart_add(s, user, p.id, 2)
        order = await repo.create_order(s, user, "Имя " * 100, "+7 (916) 555-12-34", "адрес " * 200,
                                        "", delivery="post")
        check("заказ создан", order.id > 0)
        check("имя обрезано", len(order.contact_name) <= 255, str(len(order.contact_name)))

        print("\n[4] Переходы и история")
        order, _ = await apply_transition(s, order.id, str(order.status), "paid", actor="admin", actor_id=user)
        order, _ = await apply_transition(s, order.id, "paid", "assembled", actor="admin", actor_id=user)
        hist = await repo.history_for(s, order.id)
        check("история из трёх записей", len(hist) == 3, str([h.status for h in hist]))
        check("notified булево", hist[-1].notified is True, repr(hist[-1].notified))

        print("\n[5] Сообщения админов с id супергруппы")
        await repo.remember_admin_message(s, order.id, -1001234567890, 42)
        msgs = await repo.admin_messages(s, order.id)
        check("chat_id -100... сохранился", msgs and msgs[0].chat_id == -1001234567890)

        print("\n[6] Поиск без регистра по кириллице")
        found = await repo.products_in_category(s, cat.id, query="ремешок")
        check("«ремешок» находит «РЕМЕШОК»", [x.id for x in found] == [p.id])
        by_phone = await repo.search_orders(s, query="9165551234")
        check("поиск по телефону без разделителей", [o.id for o in by_phone] == [order.id])
        by_long = await repo.search_orders(s, query="79165551234")
        check("длинный номер не роняет запрос", isinstance(by_long, list))
        counts = await repo.order_status_counts(s)
        check("счётчики по статусам", counts.get("assembled") == 1, str(counts))

        print("\n[7] Число вне int4 в id")
        try:
            await repo.order(s, 99_999_999_999)
            check("огромный id опознан как кривой ввод", False, "ошибки не было")
        except DBAPIError as exc:
            await s.rollback()
            check("огромный id опознан как кривой ввод (обработчик API даст 400)", is_bad_value(exc),
                  type(exc).__name__)

        print("\n[8] Автоотмена сравнивает время с поясом")
        # rollback выше сбросил загруженные объекты: p.id полез бы в базу синхронно.
        p = await repo.product(s, pid)
        await repo.cart_add(s, user, p.id, 1)
        stale = await repo.create_order(s, user, "Тест", "+7", "адрес", "", delivery="pickup")
        await s.execute(update(Order).where(Order.id == stale.id)
                        .values(created_at=stale.created_at - timedelta(hours=3)))
        await s.commit()
        gone = await expire_unpaid(s, 60)
        check("просроченный отменён", [o.id for o in gone] == [stale.id], str([o.id for o in gone]))
        check("остаток вернулся", (await repo.product(s, p.id)).stock == 3,
              str((await repo.product(s, p.id)).stock))


if __name__ == "__main__":
    asyncio.run(main())
