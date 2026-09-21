"""Охота на баги, которые smoke/wiring не ловят.

Проверяет граничные случаи: пользовательский ввод в HTML, выборка заказов
при живом трафике, снятый с продажи товар в чужой корзине, кнопки админа
для терминальных статусов.

Запуск: .venv\\Scripts\\python.exe bug_test.py
"""
import asyncio
import os
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ["DB_URL"] = "sqlite+aiosqlite:///bugs.db"
os.environ["SEED"] = "shop"
os.environ["CURRENCY"] = "USD"

DB_FILE = pathlib.Path("bugs.db")
if DB_FILE.exists():
    DB_FILE.unlink()

from html.parser import HTMLParser  # noqa: E402

from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import OrderStatus  # noqa: E402
from bot.handlers.checkout import order_text  # noqa: E402
from bot.keyboards.common import admin_order_kb  # noqa: E402
from bot.utils.lifecycle import apply_transition  # noqa: E402
from bot.utils.seed import seed_if_empty  # noqa: E402

ok = 0
fail = 0

# Теги, которые Telegram понимает в parse_mode=HTML. Всё остальное -> ошибка парсинга.
TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
           "a", "code", "pre", "span", "tg-spoiler", "blockquote", "br"}


class TgHtmlCheck(HTMLParser):
    """Грубая имитация парсера Telegram: ловит неизвестные и незакрытые теги."""

    def __init__(self):
        super().__init__()
        self.bad: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in TG_TAGS:
            self.bad.append(f"неизвестный тег <{tag}>")
        elif tag != "br":
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in TG_TAGS:
            self.bad.append(f"неизвестный закрывающий </{tag}>")
        elif self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.bad.append(f"непарный </{tag}>")

    def problems(self, text: str) -> list[str]:
        self.bad, self.stack = [], []
        self.feed(text)
        return self.bad + [f"незакрытый <{t}>" for t in self.stack]


def check(label: str, condition: bool, extra: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label} — {extra}")


async def main() -> None:
    await init_db()

    async with session_factory() as s:
        await seed_if_empty(s)
        cats = await repo.active_categories(s)
        prods = await repo.products_in_category(s, cats[0].id)
        p = prods[0]

        print("\n[1] Пользовательский ввод с HTML не должен ломать сообщение")
        attacker = 900000001
        await repo.cart_add(s, attacker, p.id, 1)
        evil = await repo.create_order(
            s,
            attacker,
            name="<b>Вася",                       # незакрытый тег
            phone="+7 <900> 000",                 # угловые скобки
            address="ул. <script>alert(1)</script>, 5",  # чужой тег
            comment="цена < 500 & срочно",        # голые < и &
        )
        problems = TgHtmlCheck().problems(order_text(evil, for_admin=True))
        check(
            "ввод покупателя экранируется",
            not problems,
            f"Telegram отклонит сообщение: {problems}",
        )

        print("\n[2] Название товара от админа тоже проходит через HTML")
        evil_cat = await repo.add_category(s, "Тест")
        evil_p = await repo.add_product(
            s, evil_cat.id, "Часы <Diver> 300 & Co", "описание", 100, None
        )
        buyer = 900000002
        await repo.cart_add(s, buyer, evil_p.id, 1)
        order2 = await repo.create_order(s, buyer, "Иван", "+70000000000", "адрес", "")
        problems2 = TgHtmlCheck().problems(order_text(order2))
        check("название товара экранируется", not problems2, f"{problems2}")

        print("\n[3] «Мои заказы» при живом трафике")
        target = 900000003
        await repo.cart_add(s, target, p.id, 1)
        own = await repo.create_order(s, target, "Пётр", "+70000000001", "адрес", "")
        # 60 чужих заказов поверх — больше, чем limit=50 в выборке
        for i in range(60):
            other = 800000000 + i
            await repo.cart_add(s, other, p.id, 1)
            await repo.create_order(s, other, f"Чужой{i}", "+70000000002", "адрес", "")

        # Так теперь делает хендлер «Мои заказы»: фильтр по юзеру в SQL.
        visible = await repo.orders_by_user(s, target, limit=10)
        check(
            "свой заказ виден за 60 чужими",
            [o.id for o in visible] == [own.id],
            f"ожидали только #{own.id}, получили {[o.id for o in visible]}",
        )
        check(
            "чужие заказы в выдачу не попадают",
            all(o.user_id == target for o in visible),
        )
        # Старый способ (взять последние 50 по магазину и отфильтровать в Python)
        # именно здесь и ломался — фиксируем, чтобы никто не вернул его обратно.
        legacy = [o for o in await repo.orders_by_status(s, limit=50) if o.user_id == target]
        check(
            "старый способ действительно терял заказ (регресс-маркер)",
            not legacy,
            "выборка по магазину внезапно снова находит заказ — тест потерял смысл",
        )

        print("\n[4] Снятый с продажи товар в уже собранной корзине")
        late = 900000004
        await repo.cart_add(s, late, p.id, 1)
        await repo.toggle_product(s, p.id)  # админ скрыл товар
        in_cart = await repo.cart_items(s, late)
        check(
            "скрытый товар не остаётся в корзине",
            not in_cart,
            "товар снят с продажи, но лежит в корзине и уйдёт в заказ",
        )
        await repo.toggle_product(s, p.id)  # вернуть обратно

        print("\n[5] Кнопки админа для отменённого заказа")
        cancelled, _ = await apply_transition(
            s, own.id, str(own.status), OrderStatus.cancelled, actor="admin", note="тест",
        )
        labels = [
            b.text
            for row in admin_order_kb(cancelled).inline_keyboard
            for b in row
        ]
        check(
            "у отменённого заказа нет кнопки «Оплачен»",
            "✅ Оплачен" not in labels,
            f"кнопки: {labels} — отменённый заказ можно воскресить одним кликом",
        )

    print(f"\n{'=' * 44}\nOK: {ok}   FAIL: {fail}\n{'=' * 44}")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
