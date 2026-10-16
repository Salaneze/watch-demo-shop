"""Веб-панель заказов: кого пускает и что меняет.

Главное — панель не должна стать обходным путём мимо схемы статусов и мимо
журнала: всё, что умеют кнопки бота, она делает тем же admin_transition().

Запуск: .venv\\Scripts\\python.exe admin_panel_test.py
"""
import asyncio
import os
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ADMIN, BUYER, STRANGER = 700100, 700200, 700300
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///admin_panel_test.db"
os.environ["SEED"] = "plain"
os.environ["ADMIN_IDS"] = str(ADMIN)

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from api.admin import router as admin_router  # noqa: E402
from api.routes import router as api_router  # noqa: E402
from api.session import issue_token  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import Product  # noqa: E402
from sqlalchemy import select  # noqa: E402

DB_FILE = pathlib.Path("admin_panel_test.db")
ok = fail = 0


def check(label: str, cond: bool, extra: object = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label} — {extra}")


class SpyBot:
    """Вместо Telegram: запоминает, кому что ушло; может «быть заблокированным»."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.blocked = False

    async def send_message(self, chat_id, text, **kw):
        if self.blocked:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))


def H(user_id: int) -> dict:
    return {"Authorization": "tma-session " + issue_token(user_id, None, f"user{user_id}")}


async def make_order(name: str, phone: str, delivery: str = "pickup") -> int:
    async with session_factory() as s:
        pid = (await s.scalars(select(Product.id).where(Product.is_active))).first()
        await repo.cart_add(s, BUYER, pid, 1)
        o = await repo.create_order(s, BUYER, name, phone, "Казань, ул. Пушкина 1", "", delivery=delivery)
        return o.id


async def main() -> None:
    if DB_FILE.exists():
        DB_FILE.unlink()
    await init_db()
    from bot.utils.seed import seed_if_empty
    async with session_factory() as s:
        await seed_if_empty(s)

    first = await make_order("Анна Петрова", "+7 (916) 555-12-34", "pickup")
    second = await make_order("Борис", "8 900 111 22 33", "post")
    for _ in range(10):  # заказы #3..#12 — чтобы «1» не путалась с «12» в поиске
        last = await make_order("Прочие", "+7 000 000 00 00")

    app = FastAPI()
    app.include_router(api_router)
    app.include_router(admin_router)
    spy = SpyBot()
    app.state.bot = spy

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        print("\n[1] Кого пускает")
        check("без входа — 401", (await c.get("/api/admin/orders")).status_code == 401)
        r = await c.get("/api/admin/orders", headers=H(BUYER))
        check("покупатель с честной сессией — 403", r.status_code == 403, r.status_code)
        r = await c.post(f"/api/admin/orders/{first}/transition", headers=H(STRANGER),
                         json={"from_status": "new", "to_status": "paid"})
        check("посторонний не меняет статус", r.status_code == 403, r.status_code)
        r = await c.get("/api/admin/orders", headers={"Authorization": "tma-session eyJpZCI6NzAwMTAwfQ.forged"})
        check("подделанный токен — 401", r.status_code == 401, r.status_code)
        check("админ — 200", (await c.get("/api/admin/me", headers=H(ADMIN))).status_code == 200)

        print("\n[2] Список и поиск")
        d = (await c.get("/api/admin/orders", headers=H(ADMIN))).json()
        check("все 12 заказов", len(d["orders"]) == 12, len(d["orders"]))
        check("свежие первыми", d["orders"][0]["id"] == last, d["orders"][0]["id"])
        st0 = d["orders"][0]["status"]
        check("счётчик по статусам", d["counts"].get(st0) == 12, d["counts"])

        async def found(q):
            r = await c.get("/api/admin/orders", headers=H(ADMIN), params={"q": q})
            return [o["id"] for o in r.json()["orders"]]

        check("номер ищется точно, не подстрокой", await found("1") == [first], await found("1"))
        check("номер с решёткой", await found(f"#{second}") == [second])
        check("по имени без регистра", await found("анна") == [first])
        check("по телефону цифрами при скобках в базе", await found("9165551234") == [first],
              await found("9165551234"))
        check("по телефону с пробелами в запросе", await found("900 111") == [second])
        check("% в запросе — не шаблон", await found("%") == [])
        r = await c.get("/api/admin/orders", headers=H(ADMIN), params={"status": "hacked"})
        check("неизвестный статус — 400", r.status_code == 400)

        print("\n[3] Карточка")
        card = (await c.get(f"/api/admin/orders/{first}", headers=H(ADMIN))).json()
        check("телефон и адрес в карточке", card["contact_phone"] == "+7 (916) 555-12-34" and card["address"])
        check("сумма позиции посчитана сервером", card["items"][0]["sum_text"], card["items"])
        acts = {a["to"]: a for a in card["actions"]}
        check("кнопки по схеме: оплачен и отмена", set(acts) == {"paid", "cancelled"}, set(acts))
        check("отмене нужна причина", acts["cancelled"]["note"] is True)
        check("нет заказа — 404", (await c.get("/api/admin/orders/9999", headers=H(ADMIN))).status_code == 404)

        print("\n[4] Переходы")
        r = await c.post(f"/api/admin/orders/{first}/transition", headers=H(ADMIN),
                         json={"from_status": st0, "to_status": "paid"})
        check("оплачен", r.status_code == 200 and r.json()["status"] == "paid", r.text)
        check("покупатель уведомлён", r.json()["notified"] and spy.sent and spy.sent[-1][0] == BUYER, spy.sent)
        r = await c.post(f"/api/admin/orders/{first}/transition", headers=H(ADMIN),
                         json={"from_status": st0, "to_status": "paid"})
        check("старая кнопка (заказ уже ушёл) — 409", r.status_code == 409, r.status_code)
        check("второго сообщения нет", len(spy.sent) == 1, len(spy.sent))
        r = await c.post(f"/api/admin/orders/{first}/transition", headers=H(ADMIN),
                         json={"from_status": "paid", "to_status": "delivered"})
        check("прыжок мимо схемы — 409", r.status_code == 409, r.status_code)
        r = await c.post(f"/api/admin/orders/{first}/transition", headers=H(ADMIN),
                         json={"from_status": "paid", "to_status": "cancelled", "note": "   "})
        check("отмена без причины — 422", r.status_code == 422, r.status_code)
        r = await c.post(f"/api/admin/orders/{second}/transition", headers=H(ADMIN),
                         json={"from_status": st0, "to_status": "cancelled", "note": "x" * 500})
        check("причина длиннее лимита — 422", r.status_code == 422, r.status_code)

        # Почтовый заказ доводим до «собран», дальше нужен трек.
        for a, b in ((st0, "paid"), ("paid", "assembled")):
            await c.post(f"/api/admin/orders/{second}/transition", headers=H(ADMIN),
                         json={"from_status": a, "to_status": b})
        card = (await c.get(f"/api/admin/orders/{second}", headers=H(ADMIN))).json()
        check("почте не предлагаем «готов к выдаче»", [a["to"] for a in card["actions"]] == ["shipped", "cancelled"],
              card["actions"])
        r = await c.post(f"/api/admin/orders/{second}/transition", headers=H(ADMIN),
                         json={"from_status": "assembled", "to_status": "shipped"})
        check("почта без трека — 422", r.status_code == 422, r.status_code)
        spy.blocked = True
        r = await c.post(f"/api/admin/orders/{second}/transition", headers=H(ADMIN),
                         json={"from_status": "assembled", "to_status": "shipped", "note": "RA123456789RU"})
        check("покупатель заблокировал бота — переход всё равно прошёл",
              r.status_code == 200 and r.json()["status"] == "shipped" and r.json()["notified"] is False, r.text)
        check("трек в истории", any(h["note"] == "RA123456789RU" for h in r.json()["history"]))

        print("\n[5] Журнал")
        async with session_factory() as s:
            audit = await repo.recent_actions(s, 20)
        web = [a for a in audit if "веб-панель" in (a.details or "")]
        check("каждый переход панели в /audit", len(web) == 4, [(a.target, a.details) for a in audit])
        check("в журнале id админа", all(a.admin_id == ADMIN for a in web))

        print("\n[6] Время")
        card = (await c.get(f"/api/admin/orders/{first}", headers=H(ADMIN))).json()
        # Без пояса браузер прочитал бы UTC как местное и сдвинул историю.
        check("время заказа с поясом", card["created_at"].endswith("+00:00"), card["created_at"])
        check("время в истории с поясом", all(h["created_at"].endswith("+00:00") for h in card["history"]),
              [h["created_at"] for h in card["history"]])
        from datetime import datetime
        from bot.utils.text import local_time
        check("бот печатает время в поясе магазина", local_time(datetime(2026, 10, 16, 9, 0)) == "16.10 12:00",
              local_time(datetime(2026, 10, 16, 9, 0)))

        print("\n[7] Бот выключен")
        app.state.bot = None
        r = await c.post(f"/api/admin/orders/{last}/transition", headers=H(ADMIN),
                         json={"from_status": st0, "to_status": "paid"})
        check("без бота переход проходит, notified=false", r.status_code == 200 and r.json()["notified"] is False, r.text)

    print("\n[8] Кнопка панели в боте")
    from bot.config import settings
    from bot.keyboards.common import admin_panel_kb
    saved = settings.webapp_url
    try:
        settings.webapp_url = "https://shop.example/"
        btn = admin_panel_kb().inline_keyboard[0][0]
        # Именно web_app, а не url: обычная ссылка откроет браузер без initData,
        # и админ снова пойдёт через «Войти» и подтверждение.
        check("кнопка открывает /admin как Mini App",
              btn.web_app is not None and btn.web_app.url == "https://shop.example/admin", btn)
        settings.webapp_url = "http://localhost:8080"
        check("без https кнопки нет (Telegram не откроет)", admin_panel_kb() is None)
    finally:
        settings.webapp_url = saved

    print(f"\n{'=' * 40}\nOK: {ok}   FAIL: {fail}")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
