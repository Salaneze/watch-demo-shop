"""«С этим берут» и запасной подбор аксессуаров.

Проверяется не «что-то показалось», а что подбор не врёт: заголовок «из
заказов» только при реальных общих заказах, к часам не предлагаются другие
часы, снятое и кончившееся не всплывает.

Запуск: .venv\\Scripts\\python.exe related_test.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///related_test.db"
os.environ["SEED"] = "shop"

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from api.routes import router as api_router  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.db.models import Category, Order, Product  # noqa: E402
from bot.utils.seed import seed_if_empty  # noqa: E402

DB_FILE = Path("related_test.db")
USER = 424000951

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


async def order_with(s, pids: list[int], status: str = "delivered") -> int:
    for pid in pids:
        await repo.cart_add(s, USER, pid, 1)
    o = await repo.create_order(s, USER, "Тест", "+70000000000", "адрес", "")
    await s.execute(update(Order).where(Order.id == o.id).values(status=status))
    await s.commit()
    return o.id


def titles(products) -> list[str]:
    return [p.title for p in products]


async def main() -> None:
    if DB_FILE.exists():
        DB_FILE.unlink()
    await init_db()

    async with session_factory() as s:
        await seed_if_empty(s)
        await s.execute(update(Product).values(stock=-1))
        await s.commit()
        by_title = {p.title: p for p in await s.scalars(select(Product))}
        cats = {c.title: c.id for c in await s.scalars(select(Category))}
        straps = {p.id for p in by_title.values() if p.category_id == cats["🔗 Straps"]}
        diver = by_title["Ohio Diver 67"].id
        quartz = by_title["Kron Mid 36"].id
        nato = by_title["NATO nylon, 20 mm"].id
        leather = by_title["Leather strap, 20 mm"].id
        chrono = by_title["Ohio Chrono 42"].id
        set_ = by_title["Lock In Set"].id

        print("\n[1] Запасной подбор без истории")
        found, hist = await repo.related_products(s, {diver})
        check("к автоматическим часам — только ремешки",
              found and {p.id for p in found} <= straps and not hist, str(titles(found)))
        check("дешёвые первыми", [p.price for p in found] == sorted(p.price for p in found))
        found, _ = await repo.related_products(s, {nato})
        check("к ремешку ничего (часы — не допродажа)", found == [], str(titles(found)))
        found, _ = await repo.related_products(s, {quartz})
        check("к дешёвым часам ремешки дороже трети — молчим, а не предлагаем часы",
              all(p.id in straps for p in found), str(titles(found)))
        found, _ = await repo.related_products(s, {set_})
        check("к набору — ремешки", found and {p.id for p in found} <= straps, str(titles(found)))
        found, _ = await repo.related_products(s, {diver, by_title["Milanese bracelet, 20 mm"].id})
        check("корзина часы+ремешок: ремешок из корзины не предлагается, часы тоже",
              found and all(p.id in straps and p.title != "Milanese bracelet, 20 mm" for p in found),
              str(titles(found)))
        found, _ = await repo.related_products(s, {diver}, exclude={nato})
        check("exclude уважается", nato not in {p.id for p in found})
        check("пустой набор — пусто", await repo.related_products(s, set()) == ([], False))
        check("несуществующий товар — пусто", await repo.related_products(s, {999999}) == ([], False))

        print("\n[2] Что нельзя купить — не предлагается")
        await s.execute(update(Product).where(Product.id == nato).values(is_active=False))
        await s.execute(update(Product).where(Product.id == leather).values(stock=0))
        await s.commit()
        found, _ = await repo.related_products(s, {diver})
        check("снятый и кончившийся пропали", not {nato, leather} & {p.id for p in found}, str(titles(found)))
        await s.execute(update(Product).where(Product.id.in_([nato, leather])).values(is_active=True, stock=-1))
        await s.execute(update(Category).where(Category.id == cats["🔗 Straps"]).values(is_active=False))
        await s.commit()
        found, _ = await repo.related_products(s, {diver})
        check("скрытая категория не всплывает", not straps & {p.id for p in found}, str(titles(found)))
        await s.execute(update(Category).where(Category.id == cats["🔗 Straps"]).values(is_active=True))
        await s.commit()

        print("\n[3] История заказов")
        await order_with(s, [diver, chrono])
        found, hist = await repo.related_products(s, {diver})
        check("один общий заказ — ещё не «берут»", not hist and chrono not in {p.id for p in found},
              str(titles(found)))
        await order_with(s, [diver, chrono], status="cancelled")
        found, hist = await repo.related_products(s, {diver})
        check("отменённый заказ не считается", not hist, str(titles(found)))
        await order_with(s, [diver, chrono])
        found, hist = await repo.related_products(s, {diver})
        check("два заказа — «С этим берут»", hist and found[0].id == chrono, str(titles(found)))
        found, hist = await repo.related_products(s, {chrono})
        check("связь в обе стороны", hist and found[0].id == diver, str(titles(found)))
        found, hist = await repo.related_products(s, {diver}, exclude={chrono})
        check("из истории убран то, что уже в корзине", chrono not in {p.id for p in found})
        await s.execute(update(Product).where(Product.id == chrono).values(is_active=False))
        await s.commit()
        found, hist = await repo.related_products(s, {diver})
        check("снятый товар из истории не всплывает, откат на запасной",
              chrono not in {p.id for p in found} and not hist, str(titles(found)))
        await s.execute(update(Product).where(Product.id == chrono).values(is_active=True))
        await s.commit()

    print("\n[4] API")
    app = FastAPI()
    app.include_router(api_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/api/products/{diver}/related")
        body = r.json()
        check("подборка товара публична", r.status_code == 200 and body["products"], r.text[:200])
        check("флаг истории отдаётся", body["from_orders"] is True, str(body.get("from_orders")))
        r = await c.get("/api/products/999999/related")
        check("несуществующий — 404", r.status_code == 404, str(r.status_code))
        r = await c.get("/api/cart/related")
        check("подборка корзины без подписи — 401", r.status_code == 401, str(r.status_code))

    print(f"\n{'=' * 40}\nOK: {ok}   FAIL: {fail}\n{'=' * 40}")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
