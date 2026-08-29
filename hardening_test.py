"""Проверка того, что добавлено по итогам аудита безопасности.

Ограничитель частоты, лимит тела, вход для клиентов без initData, публичный
каталог, журнал действий администратора.

Запуск: .venv\\Scripts\\python.exe hardening_test.py
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
os.environ["DB_URL"] = "sqlite+aiosqlite:///harden.db"
os.environ["SEED"] = "shop"
os.environ["CURRENCY"] = "USD"

DB_FILE = pathlib.Path("harden.db")
if DB_FILE.exists():
    DB_FILE.unlink()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from api.limits import LimitsMiddleware, RateLimiter  # noqa: E402
from api.routes import router as api_router  # noqa: E402
from api.session import (  # noqa: E402
    NONCE_TTL,
    code_from_start,
    issue_token,
    read_token,
    store as login_store,
)
from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402

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


def init_data(user_id: int = 800000001) -> str:
    user = json.dumps({"id": user_id, "first_name": "Buyer", "username": "b"},
                      separators=(",", ":"))
    fields = {"auth_date": str(int(time.time())), "user": user}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def main() -> None:
    await init_db()
    from bot.utils.seed import seed_if_empty
    async with session_factory() as s:
        await seed_if_empty(s)

    app = FastAPI()
    app.add_middleware(LimitsMiddleware)
    app.include_router(api_router)
    app.state.bot = object()          # заглушка: боту тут звонить не будем
    app.state.bot_username = "watch_demo_shop_bot"
    transport = httpx.ASGITransport(app=app)
    buyer = init_data()

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        print("\n[1] Каталог доступен без подписи")
        r = await c.get("/api/catalog")
        check("каталог отдаётся всем", r.status_code == 200, f"код {r.status_code}")
        check("товары на месте",
              len([p for cat in r.json()["categories"] for p in cat["products"]]) > 5)
        r = await c.get("/api/cart")
        check("корзина по-прежнему закрыта", r.status_code == 401, f"код {r.status_code}")
        r = await c.get("/api/orders")
        check("заказы по-прежнему закрыты", r.status_code == 401, f"код {r.status_code}")

        print("\n[2] Ограничитель частоты")
        # Заказы: по правилам не больше 5 в минуту.
        codes = []
        for _ in range(9):
            resp = await c.post("/api/order", headers={"Authorization": f"tma {buyer}"},
                                json={"name": "Test Buyer", "phone": "+15550000000",
                                      "address": "Berlin, Alexanderplatz 1",
                                      "comment": "", "expected_total": 0})
            codes.append(resp.status_code)
        check("частые заказы упираются в лимит", 429 in codes, f"коды {codes}")
        blocked = await c.post("/api/order", headers={"Authorization": f"tma {buyer}"},
                               json={"name": "Test Buyer", "phone": "+15550000000",
                                     "address": "Berlin, Alexanderplatz 1",
                                     "comment": "", "expected_total": 0})
        check("отказ приходит с Retry-After",
              blocked.status_code == 429 and "retry-after" in blocked.headers,
              f"код {blocked.status_code}, заголовки {list(blocked.headers)}")

        print("\n[3] Лимит размера тела")
        r = await c.post(
            "/api/cart",
            headers={"Authorization": f"tma {buyer}", "Content-Type": "application/json"},
            content=b'{"product_id":1,"delta":1,"junk":"' + b"x" * (20 * 1024) + b'"}',
        )
        check("огромное тело отбито до разбора", r.status_code == 413, f"код {r.status_code}")

        print("\n[4] Вход для клиентов без initData")
        r = await c.post("/api/auth/start")
        check("код входа выдан", r.status_code == 200, r.text[:120])
        data = r.json()
        code = data["code"]
        check("ссылка ведёт на бота",
              data["link"].startswith("https://t.me/watch_demo_shop_bot?start=wa_"),
              data["link"])
        check("код из ссылки читается обратно",
              code_from_start(data["link"].split("start=")[1]) == code)

        r = await c.get(f"/api/auth/poll?code={code}")
        check("пока человек не подтвердил — не готово",
              r.status_code == 200 and r.json()["ready"] is False, r.text[:120])

        # Это делает бот, получив /start wa_<код> от Telegram.
        login_store.confirm(code, 800000777, "moduser", "Mod User")
        r = await c.get(f"/api/auth/poll?code={code}")
        body = r.json()
        check("после подтверждения выдан токен", body["ready"] and body["token"], r.text[:120])
        token = body["token"]

        r = await c.get(f"/api/auth/poll?code={code}")
        check("код одноразовый", r.status_code == 404, f"код {r.status_code}")

        print("\n[5] Сессионный токен работает как подпись")
        r = await c.get("/api/cart", headers={"Authorization": f"tma-session {token}"})
        check("корзина доступна по сессии", r.status_code == 200, r.text[:120])

        r = await c.get("/api/cart", headers={"Authorization": f"tma-session {token[:-3]}xyz"})
        check("испорченный токен отвергнут", r.status_code == 401, f"код {r.status_code}")

        forged = issue_token(999999, "hacker", "Hacker")
        tampered = forged.split(".")[0] + ".AAAA"
        r = await c.get("/api/cart", headers={"Authorization": f"tma-session {tampered}"})
        check("подменённая подпись токена отвергнута", r.status_code == 401,
              f"код {r.status_code}")

        check("протухший токен не читается", read_token(_expired_token()) is None)

        print("\n[6] Журнал действий администратора")
        async with session_factory() as s:
            await repo.log_action(s, 111, "order_status", "order#1", "-> paid")
            await repo.log_action(s, 222, "product_toggle", "product#3", "скрыт")
            entries = await repo.recent_actions(s, limit=10)
        check("записи легли в журнал", len(entries) == 2, f"записей {len(entries)}")
        check("свежие первыми", entries[0].admin_id == 222, str(entries[0].admin_id))
        check("детали сохранены", entries[1].details == "-> paid", entries[1].details)


def _expired_token() -> str:
    """Токен с истёкшим сроком — подписан честно, но пользоваться им нельзя."""
    import base64
    from api.session import _sign
    payload = json.dumps({"id": 1, "u": None, "n": "X", "exp": int(time.time()) - 10},
                         separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def test_limiter_unit() -> None:
    """Ограничитель считает по ключу, а не суммарно: чужой трафик не должен
    выбивать пользователя из лимита."""
    print("\n[7] Ограничитель по ключу")
    limiter = RateLimiter()
    for _ in range(5):
        limiter.check("1.1.1.1", "POST", "/api/order")
    check("свой лимит исчерпан", limiter.check("1.1.1.1", "POST", "/api/order") is not None)
    check("сосед не пострадал", limiter.check("2.2.2.2", "POST", "/api/order") is None)
    check("другой путь свободен", limiter.check("1.1.1.1", "GET", "/api/catalog") is None)
    check("время ожидания осмысленное",
          0 < (limiter.check("1.1.1.1", "POST", "/api/order") or 0) <= 61)
    check("одноразовый код живёт минуты, а не вечность", 60 <= NONCE_TTL <= 900, str(NONCE_TTL))


if __name__ == "__main__":
    asyncio.run(main())
    test_limiter_unit()
    print(f"\n{'=' * 46}\nOK: {ok}   FAIL: {fail}\n{'=' * 46}")
    raise SystemExit(1 if fail else 0)
