"""Карточка под нишу: подсказки к терминам, «О магазине», «Запросить живое фото».

Главное — подсказка цепляется к своей строке и не ловит слово внутри другого,
чужой текст «О магазине» не всплывает в клиентском магазине, а живое фото
уходит тому, кто подписан initData, и только по метке из сообщения самого бота.

Запуск: .venv\\Scripts\\python.exe card_test.py
"""
import asyncio
import os
import pathlib
import sys
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BUYER, OTHER = 700600, 700700
ADMINS = (111, 222)
BOT_ID = 999000
os.environ["BOT_TOKEN"] = f"{BOT_ID}:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///card_test.db"
os.environ["SEED"] = "shop"
os.environ["ADMIN_IDS"] = ",".join(map(str, ADMINS))
os.environ["SHOP_ABOUT"] = ""

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import select  # noqa: E402

from api.limits import LimitsMiddleware  # noqa: E402
from api.routes import router as api_router  # noqa: E402
from api.session import issue_token  # noqa: E402
from bot.config import settings  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import AuditLog, Product  # noqa: E402
from bot.handlers.admin import answer_photo_request  # noqa: E402
from bot.utils import ask_photo  # noqa: E402
from bot.utils.niche import about_lines, spec_hints  # noqa: E402
from bot.utils.seed import seed_if_empty  # noqa: E402

DB_FILE = pathlib.Path("card_test.db")
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
    """Бот, который всё запоминает. `dead` — админы, не нажавшие /start;
    `closed` — покупатели, закрывшие личку."""

    def __init__(self) -> None:
        self.id = BOT_ID
        self.sent: list[tuple[int, str]] = []
        self.copies: list[dict] = []
        self.dead: set[int] = set()
        self.closed: set[int] = set()

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.dead:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        self.sent.append((chat_id, text))

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        if chat_id in self.closed:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.copies.append(dict(chat_id=chat_id, from_chat_id=from_chat_id,
                                message_id=message_id, **kw))

    async def me(self):
        return SimpleNamespace(username="watch_demo_shop_bot")


def H(user_id: int, name: str = "Покупатель") -> dict:
    return {"Authorization": "tma-session " + issue_token(user_id, "buyer_nick", name)}


def unit() -> None:
    print("\n[1] Подсказки к терминам")
    specs = [["Movement", "automatic, NH35"], ["Case", "steel, 40 mm"],
             ["Crystal", "Sapphire"], ["Power reserve", "41 h"]]
    hints = spec_hints(specs)
    check("длина как у характеристик", len(hints) == len(specs), hints)
    check("узкий термин побеждает общий: NH35, а не automatic", hints[0] and "Seiko" in hints[0], hints[0])
    check("строка без терминов — None", hints[1] is None, hints[1])
    check("регистр не важен", hints[2] and "scratch" in hints[2], hints[2])
    check("термин в названии строки тоже ловится", hints[3] and "off the wrist" in hints[3], hints[3])
    check("слово внутри другого не ловится",
          spec_hints([["Movement", "quartz-free"], ["Note", "unquartzed"]]) == [None, None])
    check("пустые характеристики — пустой список", spec_hints([]) == [])
    check("кривые строки не роняют", spec_hints([["Only key"], []]) == [None, None])

    real_seed = settings.seed
    settings.seed = "sneakers"
    check("ниша без словаря — без подсказок", spec_hints(specs) == [None] * 4)
    settings.seed = real_seed

    print("\n[2] О магазине")
    check("демо-витрина — демо-текст", len(about_lines()) == 4, about_lines())
    check("в демо-тексте честно сказано, что это образец", "образец" in about_lines()[0])
    settings.demo_reviews = False
    check("клиентский магазин без своего текста — блока нет", about_lines() == [])
    settings.shop_about = " Работаем с 2015 года | | Возврат 14 дней |"
    check("свой текст — по «|», пустые куски выкинуты",
          about_lines() == ["Работаем с 2015 года", "Возврат 14 дней"], about_lines())
    settings.demo_reviews = True
    check("свой текст важнее демо", about_lines() == ["Работаем с 2015 года", "Возврат 14 дней"])
    settings.shop_about = ""

    print("\n[3] Отметка просьбы")
    ask_photo._recent.clear()
    check("первая — отправлять", ask_photo.should_ask(1, 1, now=1000.0))
    check("повтор через час — нет", not ask_photo.should_ask(1, 1, now=1000.0 + 3600))
    check("другой товар — да", ask_photo.should_ask(1, 2, now=1000.0 + 3600))
    check("через 6 часов — снова да",
          ask_photo.should_ask(1, 1, now=1000.0 + ask_photo.COOLDOWN_SECONDS + 1))
    ask_photo.forget(1, 2)
    check("после forget — снова да", ask_photo.should_ask(1, 2, now=1000.0 + 3600))
    check("метка читается", ask_photo.parse_marker("…\nask:123:45") == (123, 45))
    check("без метки — None", ask_photo.parse_marker("просто текст") is None
          and ask_photo.parse_marker(None) is None)
    text = ask_photo.ask_text(5, None, "<b>Вася</b>", 9, "Diver & <i>co</i>")
    check("имя и товар экранированы", "&lt;b&gt;Вася" in text and "Diver &amp; &lt;i&gt;" in text, text)
    sneaky = ask_photo.ask_text(5, "ask:555:1", "ask:555:1", 9, "ask:555:1 Diver")
    check("метка из имени и названия не перехватывает адресата",
          ask_photo.parse_marker(sneaky) == (5, 9), ask_photo.parse_marker(sneaky))
    ask_photo._recent.clear()


async def api(spy: SpyBot) -> tuple[int, int]:
    app = FastAPI()
    app.include_router(api_router)
    app.state.bot = spy
    app.state.bot_username = "watch_demo_shop_bot"

    async with session_factory() as s:
        products = list(await s.scalars(select(Product).where(Product.is_active).order_by(Product.id)))
        first, second, hidden = products[0], products[1], products[2]
        hidden.is_active = False
        await s.commit()
        first_id, second_id, hidden_id = first.id, second.id, hidden.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        print("\n[4] Каталог")
        body = (await c.get("/api/catalog")).json()
        check("about в каталоге", len(body.get("about", [])) == 4, body.get("about"))
        items = [p for cat in body["categories"] for p in cat["products"]]
        check("spec_hints той же длины у каждого товара",
              all(len(p["spec_hints"]) == len(p["specs"]) for p in items))
        check("у демо-часов подсказки есть", sum(1 for p in items for h in p["spec_hints"] if h) >= 10)

        print("\n[5] Запросить живое фото")
        r = await c.post(f"/api/ask-photo/{first_id}")
        check("без входа — 401", r.status_code == 401, r.status_code)
        check("админов без входа не дёргали", not spy.sent)

        r = await c.post(f"/api/ask-photo/{first_id}", headers=H(BUYER), json={"user_id": OTHER})
        check("просьба — 200, sent", r.status_code == 200 and r.json() == {"sent": True}, r.text)
        check("ушла каждому админу", sorted(a for a, _ in spy.sent) == sorted(ADMINS), spy.sent)
        text = spy.sent[-1][1]
        check("метка на подписанного, не на id из тела",
              ask_photo.parse_marker(text) == (BUYER, first_id), text)
        check("в просьбе название товара", first.title in text, text)

        before = len(spy.sent)
        r = await c.post(f"/api/ask-photo/{first_id}", headers=H(BUYER))
        check("повтор — 200, sent=false", r.status_code == 200 and r.json() == {"sent": False}, r.text)
        check("повтор админам не ушёл", len(spy.sent) == before)

        r = await c.post(f"/api/ask-photo/{second_id}", headers=H(BUYER))
        check("другой товар — снова sent", r.json() == {"sent": True}, r.text)

        r = await c.post(f"/api/ask-photo/{hidden_id}", headers=H(BUYER))
        check("снятый с продажи — 404", r.status_code == 404, r.status_code)
        r = await c.post("/api/ask-photo/999999", headers=H(BUYER))
        check("несуществующий — 404", r.status_code == 404, r.status_code)
        r = await c.post("/api/ask-photo/abc", headers=H(BUYER))
        check("мусор вместо id — 422", r.status_code == 422, r.status_code)

        spy.dead = set(ADMINS)
        r = await c.post(f"/api/ask-photo/{first_id}", headers=H(OTHER))
        check("ни один админ не получил — 502", r.status_code == 502, r.status_code)
        spy.dead = {ADMINS[0]}
        r = await c.post(f"/api/ask-photo/{first_id}", headers=H(OTHER))
        check("после 502 можно повторить, хватит одного живого админа",
              r.status_code == 200 and r.json() == {"sent": True}, r.text)
        spy.dead = set()

        app.state.bot = None
        r = await c.post(f"/api/ask-photo/{second_id}", headers=H(OTHER))
        check("бот не запущен — 503", r.status_code == 503, r.status_code)
        app.state.bot = spy

    print("\n[6] Лимит")
    limited = FastAPI()
    limited.add_middleware(LimitsMiddleware)
    limited.include_router(api_router)
    limited.state.bot = spy
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=limited), base_url="http://t") as c:
        codes = [(await c.post(f"/api/ask-photo/{pid}", headers=H(BUYER + i))).status_code
                 for i, pid in enumerate([first_id, second_id] * 4)]
        check("больше 5 за 10 минут с адреса — 429", codes[:5].count(429) == 0 and 429 in codes[5:], codes)
    return first_id, first.title


def msg(reply_from: int, reply_text: str, caption: str | None = None):
    replies: list[str] = []

    async def reply(text, **kw):
        replies.append(text)

    m = SimpleNamespace(
        reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=reply_from), text=reply_text),
        caption=caption, chat=SimpleNamespace(id=ADMINS[0]), message_id=77,
        from_user=SimpleNamespace(id=ADMINS[0]), reply=reply,
    )
    return m, replies


async def handler(spy: SpyBot, product_id: int, title: str) -> None:
    print("\n[7] Ответ админа фото")
    marker = ask_photo.ask_text(BUYER, None, "Покупатель", product_id, title)
    async with session_factory() as s:
        m, replies = msg(BOT_ID, marker, caption="ремешок <b>новый</b>")
        await answer_photo_request(m, spy, s)
        copy = spy.copies[-1] if spy.copies else {}
        check("фото ушло покупателю из метки", copy.get("chat_id") == BUYER, copy)
        check("копия того самого сообщения", copy.get("message_id") == 77
              and copy.get("from_chat_id") == ADMINS[0], copy)
        check("в подписи товар и экранированная подпись админа",
              title in copy.get("caption", "") and "&lt;b&gt;новый" in copy.get("caption", ""), copy)
        btn = copy["reply_markup"].inline_keyboard[0][0] if copy.get("reply_markup") else None
        check("кнопка ведёт на товар", btn and btn.url.endswith(f"?startapp=p{product_id}"), btn)
        check("админу — «отправлено»", replies and "Отправлено" in replies[-1], replies)
        logged = await s.scalar(select(AuditLog).where(AuditLog.action == "live_photo"))
        check("действие в журнале", logged is not None and logged.target == f"product#{product_id}")

        before = len(spy.copies)
        m, replies = msg(OTHER, marker)
        await answer_photo_request(m, spy, s)
        check("метка в чужом сообщении — тишина", len(spy.copies) == before and not replies,
              (spy.copies[before:], replies))

        m, replies = msg(BOT_ID, "Новый заказ #12")
        await answer_photo_request(m, spy, s)
        check("ответ фото на другое сообщение бота — тишина", len(spy.copies) == before and not replies)

        spy.closed = {BUYER}
        m, replies = msg(BOT_ID, marker)
        await answer_photo_request(m, spy, s)
        check("личка закрыта — админ узнаёт", replies and "Не доставлено" in replies[-1], replies)
        spy.closed = set()

        hidden_marker = ask_photo.ask_text(BUYER, None, "x", 999999, "x")
        m, replies = msg(BOT_ID, hidden_marker)
        await answer_photo_request(m, spy, s)
        copy = spy.copies[-1]
        check("товар удалён — фото всё равно уходит, без кнопки",
              copy["chat_id"] == BUYER and copy["reply_markup"] is None, copy)


async def main() -> None:
    DB_FILE.unlink(missing_ok=True)
    await init_db()
    async with session_factory() as s:
        await seed_if_empty(s)
    unit()
    spy = SpyBot()
    try:
        product_id, title = await api(spy)
        await handler(spy, product_id, title)
    finally:
        DB_FILE.unlink(missing_ok=True)

    print(f"\n{'=' * 40}\nOK: {ok}   FAIL: {fail}")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
