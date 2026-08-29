"""Прогон бизнес-логики без Telegram: каталог -> корзина -> заказ -> смена статуса.

Запуск: .venv\\Scripts\\python.exe smoke_test.py
Использует отдельную БД smoke.db, чтобы не трогать рабочую.
"""
import asyncio
import os
import pathlib
import sys

# Консоль Windows по умолчанию cp1251 и роняет печать на ₽/€/₸.
# Это точка входа, а не импортируемый модуль — здесь так можно.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ["DB_URL"] = "sqlite+aiosqlite:///smoke.db"
os.environ["SEED"] = "shop"
os.environ["CURRENCY"] = "USD"

DB_FILE = pathlib.Path("smoke.db")
if DB_FILE.exists():
    DB_FILE.unlink()

from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import OrderStatus, status_ru  # noqa: E402
from bot.handlers.checkout import order_text  # noqa: E402
from bot.keyboards.common import admin_order_kb, cart_kb, categories_kb, products_kb  # noqa: E402
from bot.utils.seed import CATALOGS, active_catalog, seed_if_empty  # noqa: E402

DEMO = active_catalog()

USER = 111222333
ok = 0
fail = 0


def check(label: str, condition: bool, extra: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label} {extra}")


async def main() -> None:
    await init_db()

    async with session_factory() as s:
        print("\n[1] Сид демо-каталога")
        check("сид отработал", await seed_if_empty(s))
        check("повторный сид не дублирует", not await seed_if_empty(s))

        # Сверяемся с самим DEMO, а не с зашитыми числами — иначе тест
        # приходится править каждый раз при смене ассортимента витрины.
        cats = await repo.active_categories(s)
        check("категории созданы", len(cats) == len(DEMO), f"{len(cats)} != {len(DEMO)}")
        check("названия категорий совпали", [c.title for c in cats] == list(DEMO))
        categories_kb(cats)  # клавиатура собирается без ошибок

        expected_first = len(DEMO[cats[0].title])
        prods = await repo.products_in_category(s, cats[0].id)
        check("товары в первой категории", len(prods) == expected_first, f"got {len(prods)}")
        products_kb(prods, cats[0].id)

        print("\n[2] Корзина")
        p1, p2 = prods[0], prods[1]
        await repo.cart_add(s, USER, p1.id, 1)
        await repo.cart_add(s, USER, p1.id, 1)  # тот же товар -> qty 2
        await repo.cart_add(s, USER, p2.id, 1)
        items = await repo.cart_items(s, USER)
        check("две позиции в корзине", len(items) == 2, f"got {len(items)}")
        check("qty первого товара = 2", items[0].qty == 2, f"got {items[0].qty}")

        expected = p1.price * 2 + p2.price
        total = await repo.cart_total(s, USER)
        check("сумма корзины", total == expected, f"{total} != {expected}")
        cart_kb(items)

        qty = await repo.cart_add(s, USER, p2.id, -1)  # уходит в 0 -> позиция удаляется
        check("позиция удалена при qty<=0", qty == 0)
        check("осталась одна позиция", len(await repo.cart_items(s, USER)) == 1)

        print("\n[3] Оформление заказа")
        order = await repo.create_order(
            s, USER, "Тест Тестов", "+7 900 000-00-00", "г. Тест, ул. Тестовая, 1", "домофон 42"
        )
        check("статус awaiting_payment", order.status == OrderStatus.awaiting_payment, order.status)
        check("сумма заказа", order.total == p1.price * 2, f"{order.total}")
        check("снимок позиций", len(order.items) == 1 and order.items[0].title == p1.title)
        check("корзина очищена после заказа", not await repo.cart_items(s, USER))
        admin_order_kb(order)
        text = order_text(order, for_admin=True)
        check("текст заказа содержит адрес", "Тестовая" in text)
        check("текст заказа содержит tg id", str(USER) in text)

        print("\n[4] Пустая корзина не даёт заказ")
        try:
            await repo.create_order(s, USER, "x", "y", "z", "")
            check("ValueError на пустой корзине", False, "исключения не было")
        except ValueError:
            check("ValueError на пустой корзине", True)

        print("\n[5] Смена статуса админом")
        upd = await repo.set_order_status(s, order.id, OrderStatus.paid)
        check("статус paid", upd.status == OrderStatus.paid)
        check("status_ru для str из БД", status_ru(upd.status) == "✅ оплачен", status_ru(upd.status))
        reloaded = await repo.order(s, order.id)
        check("статус сохранился в БД", str(reloaded.status) == "paid", str(reloaded.status))
        check("фильтр по статусу", len(await repo.orders_by_status(s, OrderStatus.paid)) == 1)
        check("нет ждущих оплаты", not await repo.orders_by_status(s, OrderStatus.awaiting_payment))
        check("несуществующий заказ -> None", await repo.set_order_status(s, 9999, OrderStatus.paid) is None)

        print("\n[6] Админ: категория, товар, скрытие")
        cat = await repo.add_category(s, "Тест-категория")
        new_p = await repo.add_product(s, cat.id, "Тест-товар", "описание", 100, None)
        check("товар создан активным", new_p.is_active)
        check("виден в категории", len(await repo.products_in_category(s, cat.id)) == 1)
        toggled = await repo.toggle_product(s, new_p.id)
        check("скрыт после toggle", not toggled.is_active)
        check("не виден в каталоге", not await repo.products_in_category(s, cat.id))

        print("\n[7] Пользователи")
        await repo.upsert_user(s, USER, "tester", "Тест Тестов")
        await repo.upsert_user(s, USER, "tester2", "Тест Тестов")  # апдейт, не дубль
        check("upsert не падает на повторе", True)

        print("\n[8] Обе витрины валидны")
        check("каталоги shop и plain на месте", set(CATALOGS) == {"shop", "plain"}, list(CATALOGS))
        check(
            "витрины совпадают по структуре",
            [(k, len(v)) for k, v in CATALOGS["shop"].items()]
            == [(k, len(v)) for k, v in CATALOGS["plain"].items()],
            "shop и plain разошлись — при подмене клиент увидит другой ассортимент",
        )
        for name, catalog in CATALOGS.items():
            titles = [t for prods in catalog.values() for t, _, _ in prods]
            prices = [p for prods in catalog.values() for _, _, p in prods]
            descs = [d for prods in catalog.values() for _, d, _ in prods]
            check(f"{name}: есть категории", len(catalog) > 0)
            check(f"{name}: нет пустых категорий", all(catalog.values()))
            check(f"{name}: названия товаров уникальны", len(titles) == len(set(titles)))
            check(f"{name}: названия влезают в БД (128)", all(len(t) <= 128 for t in titles))
            check(f"{name}: цены — положительные int", all(isinstance(p, int) and p > 0 for p in prices))
            check(f"{name}: описания непустые", all(d.strip() for d in descs))
            # caption у фото в Telegram ограничен 1024 символами
            check(f"{name}: описания влезают в caption", all(len(d) < 900 for d in descs))

        print("\n[9] Кривой SEED падает, а не подсовывает не ту витрину")
        from bot.config import settings as cfg  # noqa: E402

        original = cfg.seed
        try:
            cfg.seed = "shopp"  # опечатка
            try:
                active_catalog()
                check("ValueError на неизвестном SEED", False, "исключения не было")
            except ValueError:
                check("ValueError на неизвестном SEED", True)
            cfg.seed = "  PLAIN  "  # регистр и пробелы должны прощаться
            check("SEED нормализуется", active_catalog() is CATALOGS["plain"])
        finally:
            cfg.seed = original

        print("\n[10] Валюта")
        from bot.config import settings as cfg2  # noqa: E402
        from bot.utils.money import CURRENCIES, fmt, symbol  # noqa: E402

        original_cur = cfg2.currency
        try:
            cfg2.currency = "USD"
            check("USD: символ перед суммой", fmt(2490) == "$2,490", fmt(2490))
            check("USD: symbol()", symbol() == "$")
            cfg2.currency = "RUB"
            check("RUB: символ после суммы", fmt(2490) == "2 490 ₽", repr(fmt(2490)))
            check("RUB: пробелы неразрывные", " " not in fmt(2490), repr(fmt(2490)))
            cfg2.currency = "eur"  # регистр прощается
            check("EUR: нормализация регистра", fmt(100) == "€100", fmt(100))
            cfg2.currency = "BTC"
            try:
                fmt(1)
                check("ValueError на неизвестной валюте", False, "исключения не было")
            except ValueError:
                check("ValueError на неизвестной валюте", True)
            cfg2.currency = "USD"
            check("все валюты форматируются", all(
                isinstance((setattr(cfg2, "currency", c), fmt(1234))[1], str) for c in CURRENCIES
            ))
        finally:
            cfg2.currency = original_cur

    print(f"\n{'=' * 40}\nOK: {ok}   FAIL: {fail}\n{'=' * 40}")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
