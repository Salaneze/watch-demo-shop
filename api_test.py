"""Проверка защиты Mini App API.

Главное, что здесь тестируется — подделать initData нельзя. Всё остальное в API
бессмысленно, если эта часть дырявая.

Запуск: .venv\\Scripts\\python.exe api_test.py
"""
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Фиктивный токен: подпись в тестах считается им же, настоящий из .env не нужен.
TOKEN = "123456:TEST-TOKEN-NOT-REAL"
os.environ["BOT_TOKEN"] = TOKEN
os.environ["DB_URL"] = "sqlite+aiosqlite:///api_test.db"
os.environ["SEED"] = "shop"
os.environ["CURRENCY"] = "USD"

from api.auth import (  # noqa: E402
    InitDataError,
    init_data_from_header,
    parse_init_data,
)

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


def rejects(label: str, raw: str, *, ttl: int = 3600, why: str = "") -> None:
    """Проверяет, что строка отвергнута, и именно по ожидаемой причине.

    Без сверки причины тест зеленеет по случайности: например строка без user
    отлетает по несошедшейся подписи, и проверка «нет user» ничего не проверяет.
    """
    try:
        user = parse_init_data(raw, ttl=ttl)
    except InitDataError as exc:
        if why and why not in str(exc):
            check(label, False, f"отклонено, но по другой причине: {exc}")
        else:
            check(label, True)
    else:
        check(label, False, f"строка принята, пользователь {user.id}")


def make_init_data(token: str = TOKEN, *, age: int = 0, with_user: bool = True,
                   **overrides) -> str:
    """Собирает initData ровно так, как это делает Telegram."""
    user = {"id": 555000111, "first_name": "Test", "last_name": "Buyer",
            "username": "buyer", "language_code": "en"}
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAF-test",
    }
    if with_user:
        fields["user"] = json.dumps(user, separators=(",", ":"), ensure_ascii=False)
    fields.update({k: v for k, v in overrides.items() if v is not None})
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


print("\n[1] Честная подпись проходит")
raw = make_init_data()
user = parse_init_data(raw)
check("пользователь распознан", user.id == 555000111, f"получили {user.id}")
check("имя собрано из first+last", user.full_name == "Test Buyer", user.full_name)
check("username прочитан", user.username == "buyer", str(user.username))
check("язык прочитан", user.language_code == "en", str(user.language_code))

print("\n[2] Подделки должны отлетать")
rejects("чужой токен (подпись другим ботом)", make_init_data("999:OTHER-TOKEN"),
        why="подпись не сошлась")
rejects("пустая строка", "", why="пустой")
rejects("нет hash", urlencode({"auth_date": str(int(time.time())), "user": "{}"}),
        why="нет hash")
rejects("hash обрезан", raw[:-4], why="подпись не сошлась")

# Самая опасная подмена: данные меняем, подпись оставляем от исходной строки.
tampered = raw.replace("555000111", "555000999")
check(
    "подмена id пользователя при живой подписи",
    tampered != raw,
    "строка не изменилась — тест не проверяет то, что должен",
)
rejects("подменённый id отвергнут", tampered, why="подпись не сошлась")

# Лишнее поле в строке тоже меняет check_string.
rejects("дописанное поле", raw + "&" + urlencode({"is_admin": "1"}),
        why="подпись не сошлась")

print("\n[3] Срок годности")
rejects("подпись старше часа", make_init_data(age=7200), why="протух")
fresh = parse_init_data(make_init_data(age=1800), ttl=3600)
check("подпись возрастом 30 минут принимается", fresh.id == 555000111)
rejects("та же подпись при коротком ttl", make_init_data(age=1800), ttl=600,
        why="протух")

print("\n[4] Строка без пользователя")
# Подпись честная, пользователя в наборе полей просто нет — отказ должен быть
# именно из-за отсутствия user, а не из-за развалившейся подписи.
rejects("initData без user", make_init_data(with_user=False), why="нет user")
rejects("user не JSON", make_init_data(with_user=False, user="не-json"),
        why="не разбирается как JSON")
rejects("user без id", make_init_data(with_user=False, user='{"first_name":"X"}'),
        why="числового id")
rejects("auth_date не число", make_init_data(auth_date="вчера"), why="auth_date не число")

print("\n[5] Заголовок Authorization")
check(
    "tma-заголовок разбирается",
    init_data_from_header(f"tma {raw}") == raw,
)
check(
    "регистр схемы не важен",
    init_data_from_header(f"TMA {raw}") == raw,
)
for bad, label in [
    (None, "заголовка нет"),
    ("", "заголовок пустой"),
    (raw, "схема не указана"),
    (f"Bearer {raw}", "чужая схема Bearer"),
    ("tma ", "схема без значения"),
]:
    try:
        init_data_from_header(bad)
    except InitDataError:
        check(f"отклонён: {label}", True)
    else:
        check(f"отклонён: {label}", False, "заголовок принят")


# ---------------------------------------------------------------- эндпоинты
# Приложение собирается голым, без lifespan из api.app: там поднимается бот с
# polling, а он подрался бы за апдейты с уже запущенным экземпляром.

import asyncio  # noqa: E402
import pathlib  # noqa: E402

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from api.routes import router as api_router  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.utils.seed import seed_if_empty  # noqa: E402

DB_FILE = pathlib.Path("api_test.db")


def headers(raw: str) -> dict:
    return {"Authorization": f"tma {raw}"}


def create_test_app() -> FastAPI:
    """Только api-роутер: витрина, вебхук и лимиты здесь не нужны."""
    app = FastAPI()
    app.include_router(api_router)
    return app


async def endpoints() -> None:
    if DB_FILE.exists():
        DB_FILE.unlink()
    await init_db()
    async with session_factory() as s:
        await seed_if_empty(s)

    transport = httpx.ASGITransport(app=create_test_app())

    buyer = make_init_data()
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        print("\n[6] Доступ к API")
        # Каталог открыт намеренно: товары и цены и так видит каждый в боте.
        r = await c.get("/api/catalog")
        check("каталог публичен", r.status_code == 200, f"код {r.status_code}")
        # А всё, что привязано к личности, подпись требует.
        for path in ("/api/cart", "/api/orders"):
            r = await c.get(path)
            check(f"{path} без заголовка — 401", r.status_code == 401, f"код {r.status_code}")
            r = await c.get(path, headers=headers(make_init_data("999:OTHER")))
            check(f"{path} с чужой подписью — 401", r.status_code == 401,
                  f"код {r.status_code}")
        r = await c.get("/api/cart", headers=headers(buyer))
        check("с честной подписью — 200", r.status_code == 200, f"код {r.status_code}")
        r = await c.get("/api/catalog", headers=headers(buyer))

        catalog = r.json()
        products = [p for cat in catalog["categories"] for p in cat["products"]]
        check("каталог не пустой", len(products) > 5, f"товаров {len(products)}")
        check("цена отформатирована", products[0]["price_text"].startswith("$"),
              products[0]["price_text"])
        check("валюта в ответе", catalog["currency"] == "USD", catalog["currency"])
        first = products[0]

        print("\n[7] Корзина")
        r = await c.post("/api/cart", headers=headers(buyer),
                         json={"product_id": first["id"], "delta": 2})
        check("товар добавлен", r.status_code == 200 and r.json()["lines"][0]["qty"] == 2,
              r.text[:120])
        check("сумма посчитана", r.json()["total"] == first["price"] * 2, r.text[:120])

        r = await c.post("/api/cart", headers=headers(buyer),
                         json={"product_id": first["id"], "delta": -1})
        check("количество уменьшилось", r.json()["lines"][0]["qty"] == 1, r.text[:120])

        r = await c.post("/api/cart", headers=headers(buyer),
                         json={"product_id": first["id"], "delta": 10**9})
        check("абсурдное количество отбито", r.status_code == 422, f"код {r.status_code}")

        r = await c.post("/api/cart", headers=headers(buyer),
                         json={"product_id": 999999, "delta": 1})
        check("несуществующий товар — 404", r.status_code == 404, f"код {r.status_code}")

        print("\n[8] Изоляция покупателей")
        # Разные подписи — разные люди. Если корзина протечёт, чужой заказ уедет
        # не тому человеку вместе с адресом и телефоном.
        other = make_init_data()
        other = other.replace("555000111", "555000222")
        # подпись после подмены невалидна, поэтому собираем корректную заново
        import json as _json
        other_user = _json.dumps({"id": 555000222, "first_name": "Other",
                                  "username": "other", "language_code": "en"},
                                 separators=(",", ":"))
        other = make_init_data(with_user=False, user=other_user)
        r = await c.get("/api/cart", headers=headers(other))
        check("у второго покупателя своя пустая корзина",
              r.status_code == 200 and r.json()["lines"] == [], r.text[:120])

        print("\n[9] Оформление заказа")
        r = await c.post("/api/order", headers=headers(other),
                         json={"name": "Other", "phone": "+15550001111",
                               "address": "Somewhere 5", "comment": "",
                               "expected_total": 0})
        check("пустая корзина не оформляется", r.status_code == 400, f"код {r.status_code}")

        # expected_total тут корректный: проверяем именно короткие поля, а не
        # отсутствие суммы — иначе тест позеленел бы не по той причине.
        cart_total = (await c.get("/api/cart", headers=headers(buyer))).json()["total"]
        r = await c.post("/api/order", headers=headers(buyer),
                         json={"name": "T", "phone": "+1", "address": "x", "comment": "",
                               "expected_total": cart_total})
        check("короткие поля отбиты валидацией", r.status_code == 422, f"код {r.status_code}")

        r = await c.post("/api/order", headers=headers(buyer),
                         json={"name": "Test Buyer", "phone": "+1 555 000 00 00",
                               "address": "Berlin, Alexanderplatz 1", "comment": "быстрее",
                               "expected_total": cart_total})
        check("заказ создан", r.status_code == 200, r.text[:160])
        order = r.json()
        check("сумма заказа совпала с корзиной", order["total"] == first["price"], r.text[:120])
        check("позиции сохранены", order["items"][0]["title"] == first["title"], r.text[:120])

        r = await c.get("/api/cart", headers=headers(buyer))
        check("корзина очистилась после заказа", r.json()["lines"] == [], r.text[:120])

        r = await c.get("/api/orders", headers=headers(buyer))
        check("заказ виден в своих", [o["id"] for o in r.json()] == [order["id"]], r.text[:160])

        r = await c.get("/api/orders", headers=headers(other))
        check("чужой заказ не виден", r.json() == [], r.text[:160])

        print("\n[10] Снятый с продажи товар")
        async with session_factory() as s:
            await repo.cart_add(s, 555000111, first["id"], 1)
            await repo.toggle_product(s, first["id"])
        r = await c.get("/api/cart", headers=headers(buyer))
        body = r.json()
        check("скрытый товар выпал из корзины", body["lines"] == [], r.text[:120])
        check("покупателю объяснили, что пропало", first["title"] in body["removed"],
              str(body["removed"]))
        r = await c.get("/api/catalog", headers=headers(buyer))
        titles = [p["title"] for cat in r.json()["categories"] for p in cat["products"]]
        check("скрытый товар исчез с витрины", first["title"] not in titles, str(titles[:3]))
        async with session_factory() as s:
            await repo.toggle_product(s, first["id"])


async def healthcheck() -> None:
    """Health check платформы — проверяем на реальном приложении.

    Живёт в `create_app`, а не в api-роутере: путь должен быть вне `/api`,
    без подписи, без похода в БД и мимо ограничителя частоты. Иначе усыпляющий
    тариф решит, что сервис мёртв, и снимет его.
    """
    from api.app import create_app  # noqa: PLC0415

    print("\n[11] Health check")
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/healthz")
        check("healthz открыт без подписи", r.status_code == 200, f"код {r.status_code}")
        check("healthz отвечает ok", r.json() == {"ok": True}, r.text[:60])


async def seed_art() -> None:
    """Картинки демо-товаров.

    Лежат файлами в репозитории и подставляются в `photo_file_id` с префиксом
    `seed:`. Проверяем и то, что витрина их отдаёт, и то, что слаг из БД нельзя
    превратить в путь к чужому файлу.
    """
    from bot.utils.seed import art_for, seed_art_path

    print("[12] Картинки демо-товаров")
    check("слаг известного товара найден", art_for("Ohio Diver 67") == "seed:diver",
          str(art_for("Ohio Diver 67")))
    check("незнакомый товар остаётся без фото", art_for("Чужой товар") is None)
    check("файл картинки на месте", seed_art_path("seed:diver") is not None)
    check("выход из каталога отбит", seed_art_path("seed:../../.env") is None)
    check("неизвестный слаг отбит", seed_art_path("seed:nope") is None)
    check("обычный file_id не путается с демо", seed_art_path("AgACAgIAAxk") is None)

    transport = httpx.ASGITransport(app=create_test_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        body = (await c.get("/api/catalog")).json()
        first = body["categories"][0]["products"][0]
        check("в каталоге есть ссылка на картинку", first["image"] is not None, str(first))
        r = await c.get(first["image"])
        check("картинка отдаётся", r.status_code == 200, f"код {r.status_code}")
        check("картинка — PNG", r.headers.get("content-type") == "image/png",
              str(r.headers.get("content-type")))
        check("картинка не пустая", len(r.content) > 5000, f"{len(r.content)} байт")


asyncio.run(endpoints())
asyncio.run(healthcheck())
asyncio.run(seed_art())

print(f"\n{'=' * 44}\nOK: {ok}   FAIL: {fail}\n{'=' * 44}")
raise SystemExit(1 if fail else 0)
