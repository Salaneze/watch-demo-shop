"""Отзывы, просьба оценить, «Повторить заказ».

Главное — кто и что может оставить: только свой выданный заказ, только товар
из него, оценка строго 1-5. Дальше скрытие, средняя, окно напоминания и API.

Запуск: .venv\\Scripts\\python.exe review_test.py
"""
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///review_test.db"
os.environ["SEED"] = "shop"
os.environ["DEMO_REVIEWS"] = "true"
os.environ["REVIEW_ASK_DAYS"] = "3"
os.environ["ADMIN_IDS"] = "700100"

import httpx  # noqa: E402
from aiogram.exceptions import TelegramForbiddenError  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import func, select, update  # noqa: E402

from api.routes import router as api_router  # noqa: E402
from api.session import issue_token  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import Order, OrderStatusHistory, Product, Review, utcnow  # noqa: E402
from bot.keyboards.callbacks import ReviewAdminCB, ReviewCB  # noqa: E402
from bot.keyboards.common import customer_order_kb  # noqa: E402
from bot.utils.notify import notify_customer_status  # noqa: E402
from bot.utils.reviews import admin_review_text, ask_reviews_once  # noqa: E402
from bot.utils.seed import DEMO_REVIEWS, seed_demo_reviews, seed_if_empty  # noqa: E402

DB_FILE = Path("review_test.db")
BUYER = 424000901
OTHER = 424000902

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
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.blocked = False

    async def send_message(self, chat_id: int, text: str, reply_markup=None, **kw) -> None:
        if self.blocked:
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=chat_id, text=text), message="Forbidden: bot was blocked by the user",
            )
        self.sent.append((chat_id, text, reply_markup))

    async def send_photo(self, chat_id: int, photo: str, caption: str = "", reply_markup=None, **kw) -> None:
        self.sent.append((chat_id, caption, reply_markup))


async def delivered_order(s, product_ids: list[int], user: int = BUYER, days_ago: float = 0) -> int:
    """Заказ в статусе «выдан» с записью истории нужной давности."""
    for pid in product_ids:
        await repo.cart_add(s, user, pid, 1)
    o = await repo.create_order(s, user, "Тест", "+70000000000", "адрес", "")
    await s.execute(update(Order).where(Order.id == o.id).values(status="delivered"))
    s.add(OrderStatusHistory(order_id=o.id, status="delivered", actor="admin",
                             created_at=utcnow() - timedelta(days=days_ago)))
    await s.commit()
    return o.id


async def denied(coro) -> str:
    try:
        await coro
    except repo.ReviewDenied as e:
        return str(e)
    return ""


async def main() -> None:
    if DB_FILE.exists():
        DB_FILE.unlink()
    await init_db()

    async with session_factory() as s:
        await seed_if_empty(s)
        pids = list(await s.scalars(select(Product.id).order_by(Product.id)))
        a, b, c = pids[0], pids[1], pids[2]
        # У демо-товаров остаток штучный, а заказов здесь много.
        await s.execute(update(Product).values(stock=-1))
        await s.commit()

        print("\n[1] Имя на витрине")
        check("имя + инициал", repo.public_name("Анна Каренина") == "Анна К.")
        check("одно слово", repo.public_name("Анна") == "Анна")
        check("пусто — Покупатель", repo.public_name("") == "Покупатель" and repo.public_name(None) == "Покупатель")
        check("скобка не становится инициалом", repo.public_name("Даныло (тест)") == "Даныло Т.",
              repo.public_name("Даныло (тест)"))
        check("слово из одних символов пропускается", repo.public_name("Анна 123 ✨") == "Анна",
              repo.public_name("Анна 123 ✨"))

        print("\n[2] Кто может оставить отзыв")
        mine = await delivered_order(s, [a, b])
        await repo.cart_add(s, BUYER, c, 1)
        live = (await repo.create_order(s, BUYER, "Тест", "+70000000000", "адрес", "")).id
        others = await delivered_order(s, [a], user=OTHER)

        check("чужой заказ — отказ без подробностей",
              await denied(repo.add_review(s, BUYER, others, a, 5)) == "Заказ не найден")
        check("несуществующий заказ — та же ошибка",
              await denied(repo.add_review(s, BUYER, 999999, a, 5)) == "Заказ не найден")
        check("невыданный заказ — отказ", "после получения" in await denied(repo.add_review(s, BUYER, live, c, 5)))
        check("товар не из заказа — отказ", await denied(repo.add_review(s, BUYER, mine, c, 5)) != "")
        for bad in (0, 6, -1, True, "5", 4.5, None):
            check(f"оценка {bad!r} не проходит", await denied(repo.add_review(s, BUYER, mine, a, bad)) != "")
        n = await s.scalar(select(func.count()).select_from(Review).where(Review.user_id == BUYER))
        check("после всех отказов отзывов нет", n == 0, str(n))

        print("\n[3] Создание и правка")
        r = await repo.add_review(s, BUYER, mine, a, 4, text="  <b>ок</b>  ", author="Анна К.",
                                  photo_file_id="PHOTO1")
        check("сохранено, пробелы срезаны, HTML как есть", r.text == "<b>ок</b>" and r.rating == 4, r.text)
        check("в сообщении админу HTML экранирован", "&lt;b&gt;" in admin_review_text(r, "Товар"))
        r2 = await repo.add_review(s, BUYER, mine, a, 2)
        check("повторный отзыв правит тот же", r2.id == r.id and r2.rating == 2)
        check("text=None не стирает текст, фото остаётся", r2.text == "<b>ок</b>" and r2.photo_file_id == "PHOTO1")
        r3 = await repo.add_review(s, BUYER, mine, a, 3, text="x" * 5000)
        check("текст обрезан до 1000", len(r3.text) == repo.REVIEW_TEXT_MAX, str(len(r3.text)))
        n = await s.scalar(select(func.count()).select_from(Review).where(Review.user_id == BUYER))
        check("на пару человек+товар одна строка", n == 1, str(n))
        # Второй выданный заказ с тем же товаром — всё равно один отзыв.
        again = await delivered_order(s, [a])
        r4 = await repo.add_review(s, BUYER, again, a, 5)
        check("новый заказ с тем же товаром правит старый отзыв", r4.id == r.id and r4.order_id == again)

        print("\n[4] Скрытие и средняя")
        await repo.add_review(s, BUYER, mine, b, 1, text="")
        stats = await repo.rating_stats(s)
        check("оценка без текста учтена в средней", b in stats, str(stats.get(b)))
        feed_b = await repo.product_reviews(s, b)
        check("оценка без текста не в ленте", all(x.user_id != BUYER for x in feed_b))
        await repo.set_review_hidden(s, r.id, True)
        stats = await repo.rating_stats(s)
        feed_a = await repo.product_reviews(s, a)
        check("скрытый не в ленте", all(x.id != r.id for x in feed_a))
        a_count = stats.get(a, (0, 0))[1]
        visible_a = await s.scalar(select(func.count()).select_from(Review).where(
            Review.product_id == a, Review.is_hidden.is_(False)))
        check("скрытый не в средней", a_count == visible_a, f"{a_count} vs {visible_a}")
        r5 = await repo.add_review(s, BUYER, mine, a, 5, text="передумал, всё супер")
        check("правка не снимает скрытие", r5.is_hidden is True)

        print("\n[5] Демо-отзывы")
        added = await seed_demo_reviews(s)
        check("засеяны все, у которых есть товар", added == len(DEMO_REVIEWS), f"{added}/{len(DEMO_REVIEWS)}")
        check("повторный засев ничего не делает", await seed_demo_reviews(s) == 0)
        demo = list(await s.scalars(select(Review).where(Review.is_demo.is_(True))))
        check("у демо нет заказа и id отрицательный", all(d.order_id is None and d.user_id < 0 for d in demo))
        avg = sum(d.rating for d in demo) / len(demo)
        check("средняя демо правдоподобная, не 5.0", 4.2 <= avg < 4.9, f"{avg:.2f}")

        print("\n[6] Кнопки")
        delivered = await repo.order(s, mine)
        labels = [btn.text for row in customer_order_kb(delivered).inline_keyboard for btn in row]
        check("у выданного есть «Оценить» и «Повторить»", any("Оценить" in t for t in labels)
              and any("Повторить" in t for t in labels), str(labels))
        live_o = await repo.order(s, live)
        labels = [btn.text for row in customer_order_kb(live_o).inline_keyboard for btn in row]
        check("у живого заказа ни оценки, ни повтора", not any("Оценить" in t or "Повторить" in t for t in labels),
              str(labels))
        longest = ReviewCB(action="rate", order_id=9_999_999, product_id=9_999_999, rating=5).pack()
        check("ReviewCB влезает в 64 байта", len(longest.encode()) <= 64, longest)
        check("ReviewAdminCB round-trip", ReviewAdminCB.unpack(ReviewAdminCB(review_id=7, hide=True).pack()).hide is True)

        print("\n[7] Сообщение «выдан» несёт кнопки")
        bot = FakeBot()
        entry = (await repo.history_for(s, mine))[-1]
        await notify_customer_status(bot, s, delivered, entry)
        markup = bot.sent[-1][2]
        check("под «выдан» кнопка оценки", markup is not None and "Оценить" in markup.inline_keyboard[0][0].text)

        print("\n[8] Напоминание об отзыве")
        due = await delivered_order(s, [c], days_ago=4)
        fresh = await delivered_order(s, [c], user=OTHER, days_ago=1)
        ancient = await delivered_order(s, [b], user=OTHER, days_ago=40)
        bot = FakeBot()
        sent = await ask_reviews_once(bot, s)
        targets = {t for t, _, _ in bot.sent}
        check("просьба ушла по заказу 4-дневной давности", sent == 1 and targets == {BUYER},
              f"{sent} {targets}")
        check("свежий и древний заказы не тронуты",
              await s.get(repo.ReviewAsk, fresh) is None and await s.get(repo.ReviewAsk, ancient) is None)
        check("повторный проход молчит", await ask_reviews_once(FakeBot(), s) == 0)
        # Заказ с отзывом — без напоминания.
        reviewed = await delivered_order(s, [b], user=OTHER, days_ago=5)
        await repo.add_review(s, OTHER, reviewed, b, 5)
        check("по заказу с отзывом не просим", await ask_reviews_once(FakeBot(), s) == 0)
        # Заблокировал бота — отметка всё равно ставится, иначе спам раз в минуту.
        blocked_o = await delivered_order(s, [c], user=OTHER, days_ago=6)
        bot = FakeBot()
        bot.blocked = True
        await ask_reviews_once(bot, s)
        check("заблокированный бот не зацикливает просьбу", await s.get(repo.ReviewAsk, blocked_o) is not None)
        check("due отмечен", await s.get(repo.ReviewAsk, due) is not None)

        print("\n[9] Повторить заказ")
        await repo.cart_clear(s, BUYER)
        await s.execute(update(Product).where(Product.id == b).values(is_active=False))
        await s.commit()
        added, skipped = await repo.repeat_order(s, BUYER, mine)
        check("снятый с продажи назван, живой положен", skipped and added, f"{added} / {skipped}")
        await s.execute(update(Product).where(Product.id == b).values(is_active=True, stock=0))
        await s.commit()
        await repo.cart_clear(s, BUYER)
        added, skipped = await repo.repeat_order(s, BUYER, mine)
        check("кончившийся назван", len(skipped) == 1 and len(added) == 1, f"{added} / {skipped}")
        try:
            await repo.repeat_order(s, BUYER, others)
            check("чужой заказ не повторить", False, "прошло")
        except LookupError:
            check("чужой заказ не повторить", True)
        await s.execute(update(Product).where(Product.id == b).values(stock=-1))
        await s.commit()

    print("\n[10] API")
    app = FastAPI()
    app.include_router(api_router)
    transport = httpx.ASGITransport(app=app)
    auth = {"Authorization": f"tma-session {issue_token(BUYER, 'buyer', 'Анна Каренина')}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cl:
        r = await cl.post("/api/reviews", json={"order_id": mine, "product_id": b, "rating": 5})
        check("без подписи — 401", r.status_code == 401, str(r.status_code))
        r = await cl.post("/api/reviews", headers=auth, json={"order_id": live, "product_id": c, "rating": 5})
        check("невыданный — 409", r.status_code == 409, str(r.status_code))
        r = await cl.post("/api/reviews", headers=auth, json={"order_id": others, "product_id": a, "rating": 5})
        check("чужой — 409 с той же формулировкой", r.status_code == 409 and "не найден" in r.text, r.text)
        for bad in ("5", 6, 0, True, 4.0):
            r = await cl.post("/api/reviews", headers=auth, json={"order_id": mine, "product_id": b, "rating": bad})
            check(f"оценка {bad!r} — 422", r.status_code == 422, str(r.status_code))
        r = await cl.post("/api/reviews", headers=auth,
                          json={"order_id": mine, "product_id": b, "rating": 4, "text": "x" * 1001})
        check("текст длиннее 1000 — 422", r.status_code == 422, str(r.status_code))
        r = await cl.post("/api/reviews", headers=auth,
                          json={"order_id": mine, "product_id": b, "rating": 4, "text": "норм"})
        check("свой выданный — 200, имя сокращено", r.status_code == 200 and r.json()["author"] == "Анна К.", r.text)

        r = await cl.get(f"/api/products/{b}/reviews")
        body = r.json()
        check("лента публична", r.status_code == 200 and body, r.text[:200])
        check("наружу нет user_id и order_id", all("user_id" not in x and "order_id" not in x for x in body))

        async with session_factory() as s:
            with_photo = await s.scalar(select(Review).where(Review.photo_file_id.is_not(None)))
            hidden_id = with_photo.id
        r = await cl.get(f"/api/review-img/{hidden_id}")
        check("фото скрытого отзыва — 404", r.status_code == 404, str(r.status_code))
        r = await cl.get("/api/review-img/999999")
        check("фото несуществующего — 404", r.status_code == 404, str(r.status_code))

        r = await cl.get("/api/orders", headers=auth)
        o = next(x for x in r.json() if x["id"] == mine)
        check("в заказе can_review и моя оценка", o["can_review"] and any(i["my_rating"] for i in o["items"]),
              str(o["items"]))
        o_live = next(x for x in r.json() if x["id"] == live)
        check("у невыданного can_review=false", not o_live["can_review"])

        r = await cl.get("/api/catalog")
        prods = {p["id"]: p for cat in r.json()["categories"] for p in cat["products"]}
        check("в каталоге рейтинг и число оценок", prods[b]["reviews_count"] > 0 and prods[b]["rating"])

        r = await cl.post(f"/api/orders/{others}/repeat", headers=auth)
        check("повтор чужого — 404", r.status_code == 404, str(r.status_code))
        r = await cl.post(f"/api/orders/{mine}/repeat", headers=auth)
        check("повтор своего — корзина в ответе", r.status_code == 200 and r.json()["cart"]["lines"], r.text[:200])
        line = r.json()["cart"]["lines"][0]
        check("в строке корзины есть рейтинг", "rating" in line and "reviews_count" in line)

    print(f"\n{'=' * 40}\nOK: {ok}   FAIL: {fail}\n{'=' * 40}")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
