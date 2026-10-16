"""Заявки с лендинга: /api/lead.

Бот подставной — проверяем, что и кому уходит, а не сам Telegram.

Запуск: .venv\\Scripts\\python.exe lead_test.py
"""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SITE = "https://salaneze.github.io"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["ADMIN_IDS"] = "111,222"
os.environ["LEAD_ORIGINS"] = SITE + "/, https://other.example"

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import api.lead as lead_mod  # noqa: E402
from api.lead import router as lead_router  # noqa: E402
from api.limits import LimitsMiddleware  # noqa: E402
from bot.config import settings  # noqa: E402

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


class FakeBot:
    def __init__(self, broken: set[int] = frozenset()) -> None:
        self.sent: list[tuple[int, str]] = []
        self.broken = broken

    async def send_message(self, chat_id: int, text: str, **kw) -> None:
        if chat_id in self.broken:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))


def make_app(bot: FakeBot) -> FastAPI:
    app = FastAPI()
    app.add_middleware(LimitsMiddleware)
    app.include_router(lead_router)
    app.state.bot = bot
    return app


GOOD = {"name": "Иван", "phone": "+79991234567", "car": "Kia Rio 2017",
        "comment": "Троит <b>на</b> холостых & горит чек"}


async def main() -> None:
    print("Настройки")
    check("список доменов: слеш и пробелы срезаны",
          settings.lead_origin_list == {SITE, "https://other.example"},
          str(settings.lead_origin_list))

    bot = FakeBot()
    app = make_app(bot)
    # Каждый запрос — с нового адреса, чтобы лимит на IP не мешал остальным проверкам.
    ip = iter(range(1, 10_000))

    async def post(body, origin=SITE, client=None):
        tr = httpx.ASGITransport(app=app, client=(client or f"10.0.0.{next(ip) % 250}", 1))
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            headers = {"Origin": origin} if origin else {}
            if isinstance(body, str):
                headers["Content-Type"] = "application/json"
                return await c.post("/api/lead", content=body, headers=headers)
            return await c.post("/api/lead", json=body, headers=headers)

    async def preflight(origin):
        tr = httpx.ASGITransport(app=app, client=("10.1.1.1", 1))
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            return await c.options("/api/lead", headers={
                "Origin": origin, "Access-Control-Request-Method": "POST"})

    print("CORS")
    r = await preflight(SITE)
    check("предзапрос со своего домена — 204", r.status_code == 204, str(r.status_code))
    check("разрешён ровно свой домен, не *",
          r.headers.get("access-control-allow-origin") == SITE,
          str(r.headers.get("access-control-allow-origin")))
    check("разрешён заголовок Content-Type",
          "content-type" in r.headers.get("access-control-allow-headers", "").lower())
    r = await preflight("https://evil.example")
    check("предзапрос с чужого — 403 без allow-origin",
          r.status_code == 403 and "access-control-allow-origin" not in r.headers,
          str(r.status_code))

    print("Нормальная заявка")
    r = await post(GOOD)
    check("200 и ok", r.status_code == 200 and r.json().get("ok"), r.text)
    check("CORS-заголовок в ответе", r.headers.get("access-control-allow-origin") == SITE)
    check("ушла обоим админам", sorted(c for c, _ in bot.sent) == [111, 222], str(bot.sent))
    text = bot.sent[0][1] if bot.sent else ""
    check("в тексте сайт, имя, телефон, авто",
          all(s in text for s in ("salaneze.github.io", "Иван", "+79991234567", "Kia Rio 2017")), text)
    check("HTML из комментария экранирован",
          "&lt;b&gt;на&lt;/b&gt;" in text and "&amp;" in text and "<b>на</b>" not in text, text)

    bot.sent.clear()
    r = await post({"name": "Анна", "phone": "+79990000000"})
    check("без авто и комментария — тоже ок", r.status_code == 200 and len(bot.sent) == 2, r.text)
    check("пустых строк «Авто:» нет", bot.sent and "Авто" not in bot.sent[0][1])

    print("Отказы")
    bot.sent.clear()
    r = await post(GOOD, origin="https://evil.example")
    check("чужой Origin — 403", r.status_code == 403, str(r.status_code))
    r = await post(GOOD, origin=None)
    check("без Origin — 403", r.status_code == 403, str(r.status_code))
    r = await post(GOOD, origin="null")
    check("Origin null (файл с диска) — 403", r.status_code == 403, str(r.status_code))
    check("ничего не ушло", bot.sent == [], str(bot.sent))

    for label, body in [
        ("пустое имя", {**GOOD, "name": ""}),
        ("имя из пробелов", {**GOOD, "name": "   "}),
        ("телефон без плюса", {**GOOD, "phone": "89991234567"}),
        ("телефон с маской", {**GOOD, "phone": "+7 (999) 123-45-67"}),
        ("телефон короткий", {**GOOD, "phone": "+7999"}),
        ("комментарий 1001 символ", {**GOOD, "comment": "x" * 1001}),
        ("имя не строка", {**GOOD, "name": {"a": 1}}),
    ]:
        r = await post(body)
        check(f"{label} — 422 с CORS",
              r.status_code == 422 and r.headers.get("access-control-allow-origin") == SITE,
              f"{r.status_code} {r.text}")
    r = await post("{не json")
    check("битый JSON — 422 с CORS",
          r.status_code == 422 and r.headers.get("access-control-allow-origin") == SITE,
          f"{r.status_code} {r.text}")
    r = await post("[]")
    check("JSON-массив вместо объекта — 422", r.status_code == 422, str(r.status_code))
    check("ни одна кривая заявка не ушла", bot.sent == [], str(bot.sent))

    print("Ловушка")
    r = await post({**GOOD, "website": "http://spam"})
    check("бот получает обычный ok", r.status_code == 200 and r.json().get("ok"), r.text)
    check("но админам ничего", bot.sent == [], str(bot.sent))

    print("Лимит на адрес")
    codes = [(await post(GOOD, client="10.9.9.9")).status_code for _ in range(4)]
    check("три заявки проходят, четвёртая 429", codes == [200, 200, 200, 429], str(codes))

    print("Общий потолок")
    lead_mod._recent.clear()
    bot.sent.clear()
    codes = [(await post(GOOD)).status_code for _ in range(lead_mod.GLOBAL_LIMIT + 2)]
    check(f"после {lead_mod.GLOBAL_LIMIT} за час — 429",
          codes.count(200) == lead_mod.GLOBAL_LIMIT and codes[-2:] == [429, 429], str(codes[-4:]))
    check("лишние не доставлены", len(bot.sent) == lead_mod.GLOBAL_LIMIT * 2, str(len(bot.sent)))
    lead_mod._recent.clear()

    print("Доставка")
    app.state.bot = FakeBot(broken={111})
    r = await post(GOOD)
    check("один админ недоступен — всё равно 200",
          r.status_code == 200 and [c for c, _ in app.state.bot.sent] == [222], r.text)
    app.state.bot = FakeBot(broken={111, 222})
    r = await post(GOOD)
    check("никому не доставлено — 502, страница покажет телефон",
          r.status_code == 502 and r.headers.get("access-control-allow-origin") == SITE,
          f"{r.status_code} {r.text}")

    print("Выключено")
    saved = settings.lead_origins
    settings.lead_origins = ""
    r = await post(GOOD)
    check("пустой LEAD_ORIGINS — 404", r.status_code == 404, str(r.status_code))
    r = await preflight(SITE)
    check("и предзапрос 404", r.status_code == 404, str(r.status_code))
    settings.lead_origins = saved

    print(f"\nИтого: {ok} OK, {fail} FAIL")
    sys.exit(1 if fail else 0)


asyncio.run(main())
