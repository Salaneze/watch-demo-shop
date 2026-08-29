"""Враждебный заход по витрине Mini App.

Свои зелёные тесты ничего не доказывают (урок 41), поэтому здесь не «проверим, что
работает», а «сломаем». Оси те же: враждебный ввод, объём, изменение состояния между
шагами, терминальные состояния, второй тип пользователя.

Запуск: .venv\\Scripts\\python.exe bug2_test.py
"""
import asyncio
import hashlib
import hmac
import json
import os
import pathlib
import sys
import time
from urllib.parse import urlencode

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOKEN = "123456:TEST-TOKEN-NOT-REAL"
os.environ["BOT_TOKEN"] = TOKEN
os.environ["DB_URL"] = "sqlite+aiosqlite:///bugs2.db"
os.environ["SEED"] = "shop"
os.environ["CURRENCY"] = "USD"

DB_FILE = pathlib.Path("bugs2.db")
if DB_FILE.exists():
    DB_FILE.unlink()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from html.parser import HTMLParser  # noqa: E402

from api.routes import router as api_router  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.utils.notify import order_text  # noqa: E402
from bot.utils.seed import seed_if_empty  # noqa: E402

ok = 0
fail = 0

TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
           "a", "code", "pre", "span", "tg-spoiler", "blockquote", "br"}


class TgHtmlCheck(HTMLParser):
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


def init_data(user_id: int = 700000001, name: str = "Buyer") -> str:
    user = json.dumps({"id": user_id, "first_name": name, "username": "b",
                       "language_code": "en"}, separators=(",", ":"))
    fields = {"auth_date": str(int(time.time())), "query_id": "AAF", "user": user}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def hdr(raw: str) -> dict:
    return {"Authorization": f"tma {raw}"}


ORDER_FORM = {"name": "Test Buyer", "phone": "+1 555 000 00 00",
              "address": "Berlin, Alexanderplatz 1", "comment": ""}


async def place(c, raw: str, **overrides):
    """Оформляет заказ так же, как это делает витрина: с суммой, которую видит клиент."""
    total = (await c.get("/api/cart", headers=hdr(raw))).json()["total"]
    body = {**ORDER_FORM, "expected_total": total, **overrides}
    return await c.post("/api/order", headers=hdr(raw), json=body)


async def main() -> None:
    await init_db()
    async with session_factory() as s:
        await seed_if_empty(s)
        cats = await repo.active_categories(s)
        prods = await repo.products_in_category(s, cats[0].id)
        target = prods[0]
        target_id, base_price = target.id, target.price

    app = FastAPI()
    app.include_router(api_router)
    transport = httpx.ASGITransport(app=app)
    buyer = init_data()

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        print("\n[1] Цена изменилась, пока покупатель заполнял форму")
        await c.post("/api/cart", headers=hdr(buyer),
                     json={"product_id": target_id, "delta": 1})
        shown = (await c.get("/api/cart", headers=hdr(buyer))).json()["total"]
        # Админ поднимает цену, пока форма доставки открыта.
        async with session_factory() as s:
            p = await repo.product(s, target_id)
            p.price = base_price * 3
            await s.commit()
        r = await c.post("/api/order", headers=hdr(buyer),
                         json={**ORDER_FORM, "expected_total": shown})
        charged = r.json().get("total") if r.status_code == 200 else None
        check(
            "заказ не уходит молча по новой цене",
            r.status_code == 409,
            f"показали {shown}, заказали на {charged} (код {r.status_code})",
        )
        # Защиту нельзя снять, просто не прислав поле.
        r = await c.post("/api/order", headers=hdr(buyer), json=ORDER_FORM)
        check(
            "заказ без ожидаемой суммы не принимается",
            r.status_code == 422,
            f"код {r.status_code} — старый клиент обходит проверку цены",
        )
        async with session_factory() as s:
            p = await repo.product(s, target_id)
            p.price = base_price
            await s.commit()
            await repo.cart_clear(s, 700000001)

        print("\n[2] Двойная отправка заказа")
        await c.post("/api/cart", headers=hdr(buyer),
                     json={"product_id": target_id, "delta": 1})
        total = (await c.get("/api/cart", headers=hdr(buyer))).json()["total"]
        body = {**ORDER_FORM, "expected_total": total}
        first, second = await asyncio.gather(
            c.post("/api/order", headers=hdr(buyer), json=body),
            c.post("/api/order", headers=hdr(buyer), json=body),
        )
        codes = sorted([first.status_code, second.status_code])
        check(
            "две одновременные отправки дают один заказ",
            codes.count(200) == 1,
            f"коды {codes} — покупатель получил два заказа за одну корзину",
        )
        async with session_factory() as s:
            await repo.cart_clear(s, 700000001)

        print("\n[3] Снятый с продажи товар и его картинка")
        async with session_factory() as s:
            hidden = await repo.add_product(
                s, cats[0].id, "Скрытая модель", "не для продажи", 500, "FAKE_FILE_ID"
            )
            hidden_id = hidden.id
            await repo.toggle_product(s, hidden_id)
        r = await c.get(f"/api/img/{hidden_id}")
        check(
            "картинка скрытого товара не отдаётся",
            r.status_code == 404,
            f"код {r.status_code} — снятый с продажи товар всё ещё видно по прямой ссылке",
        )

        print("\n[4] Остаток на складе")
        async with session_factory() as s:
            limited = await repo.add_product(s, cats[0].id, "Последний экземпляр",
                                             "остался один", 100, None)
            limited.stock = 1
            await s.commit()
            limited_id = limited.id
        for _ in range(5):
            await c.post("/api/cart", headers=hdr(buyer),
                         json={"product_id": limited_id, "delta": 20})
        r = await place(c, buyer)
        got = r.json().get("items", [{}])[0].get("qty") if r.status_code == 200 else None
        check(
            "нельзя заказать больше, чем есть на складе",
            r.status_code != 200 or got is None or got <= 1,
            f"stock=1, а заказали {got} шт (код {r.status_code})",
        )
        async with session_factory() as s:
            await repo.cart_clear(s, 700000001)

        print("\n[5] Враждебный ввод из витрины доходит до админа")
        await c.post("/api/cart", headers=hdr(buyer),
                     json={"product_id": target_id, "delta": 1})
        r = await place(
            c, buyer,
            name="<b>Вася",
            phone="+7 <900> 000",
            address="ул. <script>alert(1)</script>, 5",
            comment="цена < 500 & срочно",
        )
        check("заказ с угловыми скобками принят", r.status_code == 200, r.text[:160])
        async with session_factory() as s:
            order = (await repo.orders_by_user(s, 700000001, limit=1))[0]
            problems = TgHtmlCheck().problems(order_text(order, for_admin=True))
        check(
            "уведомление админу не рвётся разметкой",
            not problems,
            f"Telegram отклонит сообщение: {problems}",
        )

        print("\n[6] Границы полей формы")
        await c.post("/api/cart", headers=hdr(buyer),
                     json={"product_id": target_id, "delta": 1})
        r = await place(c, buyer, comment="я" * 5000)
        check("гигантский комментарий отбит", r.status_code == 422, f"код {r.status_code}")
        r = await place(c, buyer, name="   ")
        check(
            "имя из одних пробелов не проходит",
            r.status_code == 422,
            f"код {r.status_code} — админ получит заказ без имени",
        )

        print("\n[7] Чужой заказ через прямой запрос")
        stranger = init_data(700000999, "Stranger")
        r = await c.get("/api/orders", headers=hdr(stranger))
        check("чужие заказы недоступны", r.json() == [], r.text[:160])

    print(f"\n{'=' * 46}\nOK: {ok}   FAIL: {fail}\n{'=' * 46}")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
