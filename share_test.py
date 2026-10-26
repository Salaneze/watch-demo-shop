"""«Поделиться товаром»: что уходит в Telegram и кому.

Главное — сообщение сохраняется только на того, кто подписан, кнопка в нём
ведёт на прямую ссылку (web_app в чужом чате не работает), а отказ Telegram
принять демо-фото не ломает кнопку целиком.

Запуск: .venv\\Scripts\\python.exe share_test.py
"""
import asyncio
import os
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BUYER, OTHER = 700400, 700500
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///share_test.db"
os.environ["SEED"] = "shop"
os.environ["WEBAPP_URL"] = "https://shop.example"

import httpx  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from aiogram.methods import SavePreparedInlineMessage  # noqa: E402
from aiogram.types import (InlineQueryResultArticle, InlineQueryResultCachedPhoto,  # noqa: E402
                           InlineQueryResultPhoto, PreparedInlineMessage)
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import select  # noqa: E402

from api.limits import LimitsMiddleware  # noqa: E402
from api.routes import router as api_router  # noqa: E402
from api.session import issue_token  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import Product  # noqa: E402
from bot.utils.seed import seed_if_empty  # noqa: E402
from bot.utils.share import product_link, share_result  # noqa: E402

DB_FILE = pathlib.Path("share_test.db")
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
    """Запоминает каждый вызов; `reject` — сколько первых вызовов Telegram отклонит."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.reject = 0

    async def save_prepared_inline_message(self, **kw):
        self.calls.append(kw)
        if self.reject:
            self.reject -= 1
            raise TelegramBadRequest(method=SavePreparedInlineMessage(user_id=0, result=kw["result"]),
                                     message="Bad Request: PHOTO_INVALID_DIMENSIONS")
        return PreparedInlineMessage(id=f"prep{len(self.calls)}", expiration_date=0)


def H(user_id: int) -> dict:
    return {"Authorization": "tma-session " + issue_token(user_id, None, f"user{user_id}")}


def button(result):
    return result.reply_markup.inline_keyboard[0][0]


def unit() -> None:
    print("\n[1] Ссылка и сообщение")
    link = product_link("watch_demo_shop_bot", 7)
    check("прямая ссылка на Main Mini App", link == "https://t.me/watch_demo_shop_bot?startapp=p7", link)

    p = Product(id=7, title="Diver <b>200m</b> & co", price=12900, photo_file_id="AgACAgIAAxkBreal")
    r = share_result(p, link, "https://shop.example/api/img/7")
    check("фото из Telegram — по file_id", isinstance(r, InlineQueryResultCachedPhoto), type(r).__name__)
    check("кнопка — url на товар", button(r).url == link, button(r))
    check("кнопка не web_app (в чужом чате не работает)", button(r).web_app is None)
    check("название экранировано", "&lt;b&gt;200m&lt;/b&gt; &amp; co" in r.caption, r.caption)

    p.photo_file_id = "seed:diver"
    r = share_result(p, link, "https://shop.example/api/img/7?v=1")
    check("демо-рисунок — ссылкой на наш https", isinstance(r, InlineQueryResultPhoto)
          and r.photo_url == "https://shop.example/api/img/7?v=1", r)
    r = share_result(p, link, None)
    check("демо без публичного адреса — текстом", isinstance(r, InlineQueryResultArticle), type(r).__name__)
    r = share_result(p, link, "https://shop.example/x", plain=True)
    check("plain — текст с той же кнопкой", isinstance(r, InlineQueryResultArticle) and button(r).url == link)

    p.photo_file_id = None
    r = share_result(p, link, None)
    check("товар без фото — текстом", isinstance(r, InlineQueryResultArticle), type(r).__name__)


async def api(app: FastAPI, spy: SpyBot) -> None:
    async with session_factory() as s:
        products = list(await s.scalars(select(Product).where(Product.is_active).order_by(Product.id)))
        real, demo, hidden = products[0], products[1], products[2]
        real.photo_file_id = "AgACAgIAAxkBreal"
        hidden.is_active = False
        await s.commit()
        real_id, demo_id, hidden_id = real.id, demo.id, hidden.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        print("\n[2] Каталог отдаёт имя бота")
        r = await c.get("/api/catalog")
        check("bot в каталоге", r.json().get("bot") == "watch_demo_shop_bot", r.json().get("bot"))

        print("\n[3] Кто и что")
        r = await c.post(f"/api/share/{real_id}")
        check("без входа — 401", r.status_code == 401, r.status_code)
        check("Telegram без входа не дёргали", not spy.calls)

        r = await c.post(f"/api/share/{real_id}", headers=H(BUYER), json={"user_id": OTHER})
        check("свой товар — 200 и id", r.status_code == 200 and r.json()["id"] == "prep1", r.text)
        kw = spy.calls[-1]
        check("сохранено на подписанного, не на id из тела", kw["user_id"] == BUYER, kw["user_id"])
        check("в личку, группы и каналы", kw["allow_user_chats"] and kw["allow_group_chats"]
              and kw["allow_channel_chats"], kw)
        check("ботам — нет", not kw.get("allow_bot_chats"), kw.get("allow_bot_chats"))
        check("ссылка в кнопке на этот товар",
              button(kw["result"]).url.endswith(f"?startapp=p{real_id}"), button(kw["result"]))

        r = await c.post(f"/api/share/{hidden_id}", headers=H(BUYER))
        check("снятый с продажи — 404", r.status_code == 404, r.status_code)
        r = await c.post("/api/share/999999", headers=H(BUYER))
        check("несуществующий — 404", r.status_code == 404, r.status_code)
        r = await c.post("/api/share/abc", headers=H(BUYER))
        check("мусор вместо id — 422", r.status_code == 422, r.status_code)

        print("\n[4] Telegram не принял фото")
        before = len(spy.calls)
        spy.reject = 1
        r = await c.post(f"/api/share/{demo_id}", headers=H(BUYER))
        tries = spy.calls[before:]
        check("повтор после отказа — 200", r.status_code == 200, r.text)
        check("первая попытка — фото ссылкой", isinstance(tries[0]["result"], InlineQueryResultPhoto),
              type(tries[0]["result"]).__name__)
        check("вторая — текст с кнопкой", len(tries) == 2 and isinstance(tries[1]["result"], InlineQueryResultArticle),
              [type(t["result"]).__name__ for t in tries])

        spy.reject = 2
        r = await c.post(f"/api/share/{demo_id}", headers=H(BUYER))
        check("отказ дважды — 502, а не 500", r.status_code == 502, r.status_code)
        spy.reject = 0

        print("\n[5] Бот не запущен")
        app.state.bot = None
        r = await c.post(f"/api/share/{real_id}", headers=H(BUYER))
        check("503", r.status_code == 503, r.status_code)
        app.state.bot = spy

        print("\n[6] Лимит")
        codes = [(await c.post(f"/api/share/{real_id}", headers=H(OTHER))).status_code for _ in range(12)]
        # ключ лимита — адрес, а не человек; с этого адреса уже было 7 запросов выше
        check("больше 10 в минуту — 429", 429 in codes, codes)


async def main() -> None:
    DB_FILE.unlink(missing_ok=True)
    await init_db()
    async with session_factory() as s:
        await seed_if_empty(s)
    unit()

    app = FastAPI()
    app.add_middleware(LimitsMiddleware)
    app.include_router(api_router)
    spy = SpyBot()
    app.state.bot = spy
    app.state.bot_username = "watch_demo_shop_bot"
    try:
        await api(app, spy)
    finally:
        DB_FILE.unlink(missing_ok=True)

    print(f"\n{'=' * 40}\nOK: {ok}   FAIL: {fail}")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
