"""Проверка витринных фич: поиск, сортировка, избранное, доставка, промокод.

Главное здесь — не «кнопка работает», а деньги: цену доставки и размер скидки
считает сервер, и подменить их из клиента нельзя. Остальное (поиск, сердечки)
проверяется заодно, чтобы регрессия не прошла тихо.

Запуск: .venv\\Scripts\\python.exe features_test.py
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOKEN = "123456:TEST-TOKEN-NOT-REAL"
os.environ["BOT_TOKEN"] = TOKEN
os.environ["DB_URL"] = "sqlite+aiosqlite:///features_test.db"
os.environ["SEED"] = "plain"
os.environ["CURRENCY"] = "USD"
os.environ["DELIVERY_COURIER"] = "9"
os.environ["DELIVERY_POST"] = "15"

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from api.routes import router as api_router  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.utils.delivery import resolve_promo  # noqa: E402
from bot.utils.seed import seed_if_empty  # noqa: E402

DB_FILE = Path("features_test.db")

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


def init_data(user_id: int = 424000001, name: str = "Buyer") -> str:
    user = {"id": user_id, "first_name": name, "username": "buyer", "language_code": "en"}
    fields = {"auth_date": str(int(time.time())), "query_id": "AAF-test",
              "user": json.dumps(user, separators=(",", ":"))}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def headers(raw: str) -> dict:
    return {"Authorization": f"tma {raw}"}


async def run() -> None:
    if DB_FILE.exists():
        DB_FILE.unlink()
    await init_db()
    async with session_factory() as s:
        await seed_if_empty(s)

    app = FastAPI()
    app.include_router(api_router)
    transport = httpx.ASGITransport(app=app)
    me = headers(init_data())

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        print("\n[1] Поиск и сортировка")
        r = await c.get("/api/catalog", params={"q": "nylon"})
        found = [p["title"] for cat in r.json()["categories"] for p in cat["products"]]
        check("поиск находит по названию", found == ["NATO nylon, 20 mm"], str(found))

        r = await c.get("/api/catalog", params={"q": "mother-of-pearl"})
        found = [p["title"] for cat in r.json()["categories"] for p in cat["products"]]
        check("поиск смотрит и в описание", found == ["Nordwind Aria 34"], str(found))

        r = await c.get("/api/catalog", params={"q": "%"})
        found = [p["title"] for cat in r.json()["categories"] for p in cat["products"]]
        # Если % уедет в LIKE как есть, он совпадёт со всем каталогом.
        check("процент ищется как символ, а не как маска", found == [], str(found))

        r = await c.get("/api/catalog", params={"sort": "price_asc"})
        prices = [p["price"] for cat in r.json()["categories"] for p in cat["products"]]
        cats = r.json()["categories"]
        check("сортировка по возрастанию внутри категории",
              all(sorted(p["price"] for p in cat["products"]) ==
                  [p["price"] for p in cat["products"]] for cat in cats), str(prices))

        r = await c.get("/api/catalog", params={"sort": "; drop table products"})
        check("мусорная сортировка отбита", r.status_code == 400, f"код {r.status_code}")

        print("\n[2] Характеристики и остатки")
        r = await c.get("/api/catalog")
        products = {p["title"]: p for cat in r.json()["categories"] for p in cat["products"]}
        skeleton = products["Kron Skeleton 41"]
        check("характеристики приехали", len(skeleton["specs"]) == 4, str(skeleton["specs"]))
        check("остаток виден", skeleton["stock"] == 2, str(skeleton["stock"]))
        check("товар без учёта склада отдаёт -1", products["Nordwind Diver 300"]["stock"] == -1)
        diver_images = products["Nordwind Diver 300"]["images"]
        check("галерея содержит основной кадр", len(diver_images) == 1, str(diver_images))
        # Метка версии обязана быть: без неё замена фото доходит до покупателя
        # через сутки — ровно столько живёт наш Cache-Control.
        check("в адресе картинки есть версия",
              diver_images[0].startswith("/api/img/1?v=") and len(diver_images[0]) > 16,
              diver_images[0])

        print("\n[3] Избранное")
        r = await c.post("/api/favorites", json={"product_id": skeleton["id"]})
        check("избранное без подписи — 401", r.status_code == 401, f"код {r.status_code}")

        r = await c.post("/api/favorites", json={"product_id": skeleton["id"]}, headers=me)
        check("сердечко ставится", r.json()["is_favorite"] is True, r.text[:120])
        r = await c.get("/api/favorites", headers=me)
        check("товар в избранном", [p["id"] for p in r.json()] == [skeleton["id"]], r.text[:120])
        r = await c.post("/api/favorites", json={"product_id": skeleton["id"]}, headers=me)
        check("повторное нажатие снимает", r.json()["is_favorite"] is False, r.text[:120])
        r = await c.get("/api/favorites", headers=me)
        check("список опустел", r.json() == [], r.text[:120])

        stranger = headers(init_data(424000999, "Stranger"))
        await c.post("/api/favorites", json={"product_id": skeleton["id"]}, headers=me)
        r = await c.get("/api/favorites", headers=stranger)
        check("чужое избранное не видно", r.json() == [], r.text[:120])

        print("\n[4] Доставка")
        r = await c.get("/api/delivery")
        options = {o["code"]: o for o in r.json()}
        check("три способа", set(options) == {"pickup", "courier", "post"}, str(list(options)))
        check("самовывоз бесплатный", options["pickup"]["cost"] == 0)
        check("цена курьера из настроек, а не из рублёвого прайса",
              options["courier"]["cost"] == 9, str(options["courier"]["cost"]))

        print("\n[5] Промокод")
        diver = products["Nordwind Diver 300"]
        await c.post("/api/cart", json={"product_id": diver["id"], "delta": 1}, headers=me)

        r = await c.post("/api/promo", json={"code": "нет-такого"}, headers=me)
        check("несуществующий код — не ошибка, а ответ", r.status_code == 200, f"код {r.status_code}")
        check("и он недействителен", r.json()["valid"] is False, r.text[:120])

        r = await c.post("/api/promo", json={"code": " welcome10 "}, headers=me)
        body = r.json()
        check("регистр и пробелы не мешают", body["valid"] is True, r.text[:160])
        check("скидка 10% от корзины", body["discount"] == diver["price"] // 10,
              f"{body['discount']} при цене {diver['price']}")

        print("\n[6] Деньги: подменить цену доставки нельзя")
        items_total = diver["price"]
        honest_total = items_total - items_total // 10 + 9  # courier

        # Клиент говорит «курьер», но платить хочет как за самовывоз.
        r = await c.post("/api/order", headers=me, json={
            "name": "Test Buyer", "phone": "+15550000000", "address": "Город, улица, 1",
            "comment": "", "expected_total": items_total - items_total // 10,
            "delivery": "courier", "promo_code": "WELCOME10",
        })
        check("курьер по цене самовывоза отбит", r.status_code == 409, f"код {r.status_code}")

        # Промокода в запросе нет, а скидку клиент себе всё равно посчитал.
        r = await c.post("/api/order", headers=me, json={
            "name": "Test Buyer", "phone": "+15550000000", "address": "Город, улица, 1",
            "comment": "", "expected_total": items_total - items_total // 10 + 9,
            "delivery": "courier", "promo_code": "",
        })
        check("скидка без промокода отбита", r.status_code == 409, f"код {r.status_code}")

        # Несуществующий способ доставки не должен молча стать самовывозом.
        r = await c.post("/api/order", headers=me, json={
            "name": "Test Buyer", "phone": "+15550000000", "address": "Город, улица, 1",
            "comment": "", "expected_total": items_total, "delivery": "teleport",
            "promo_code": "",
        })
        check("выдуманный способ доставки отбит", r.status_code == 422, f"код {r.status_code}")

        # Честный заказ проходит и раскладывается по слагаемым.
        r = await c.post("/api/order", headers=me, json={
            "name": "Test Buyer", "phone": "+15550000000", "address": "Город, улица, 1",
            "comment": "", "expected_total": honest_total,
            "delivery": "courier", "promo_code": "WELCOME10",
        })
        check("честный заказ принят", r.status_code == 200, r.text[:200])
        order = r.json()
        check("сумма товаров сохранена", order["items_total"] == items_total, str(order))
        check("скидка сохранена", order["discount"] == items_total // 10, str(order["discount"]))
        check("доставка сохранена", order["delivery_cost"] == 9, str(order["delivery_cost"]))
        check("итог сходится",
              order["total"] == order["items_total"] - order["discount"] + order["delivery_cost"],
              str(order))
        check("промокод записан в заказ", order["promo_code"] == "WELCOME10", str(order["promo_code"]))

        print("\n[7] Лимит использований промокода")
        async with session_factory() as s:
            promo = await resolve_promo(s, "NORDWIND20")
            check("код с лимитом жив, пока лимит не выбран", promo is not None)
            promo.used = promo.max_uses
            await s.commit()
        async with session_factory() as s:
            spent = await resolve_promo(s, "NORDWIND20")
            check("выбранный лимит закрывает код", spent is None, str(spent))


def main() -> int:
    asyncio.run(run())
    print("\n" + "=" * 46)
    print(f"OK: {ok}   FAIL: {fail}")
    print("=" * 46)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
