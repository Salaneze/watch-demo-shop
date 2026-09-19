"""ИИ-консультант: цикл агента и инструменты на фейковом провайдере, без сети.

Главное — не «модель отвечает», а границы: она не может назначить цену, обойти
остаток, вызвать несуществующий инструмент или крутиться в цикле бесконечно.

Запуск: .venv\\Scripts\\python.exe ai_test.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///ai_test.db"
os.environ["SEED"] = "plain"
os.environ["ADMIN_IDS"] = "777, 778"
os.environ["AI_PROVIDER"] = ""

from bot.ai import tools  # noqa: E402
from bot.ai.agent import FALLBACK_TEXT, MAX_TOOL_ROUNDS, Agent  # noqa: E402
from bot.ai.provider import FakeProvider, Reply, build_provider  # noqa: E402
from bot.ai.tools import ToolContext  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.db.base import init_db, session_factory  # noqa: E402
from bot.keyboards.common import main_menu  # noqa: E402
from bot.utils.seed import seed_if_empty  # noqa: E402

DB_FILE = Path("ai_test.db")
ok = fail = 0


def check(label: str, condition: bool, extra: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label} — {extra}")


class FakeBot:
    """Запоминает отправленное и умеет «терять» одного админа, как живой Bot."""

    def __init__(self, dead: set[int] = frozenset()) -> None:
        self.sent: list[tuple[int, str]] = []
        self.dead = dead

    async def send_message(self, chat_id: int, text: str, **kw) -> None:
        if chat_id in self.dead:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))


async def run() -> None:
    if DB_FILE.exists():
        DB_FILE.unlink()
    await init_db()
    async with session_factory() as s:
        await seed_if_empty(s)
        cats = await repo.active_categories(s)
        all_products = []
        for c in cats:
            all_products += await repo.products_in_category(s, c.id)
        limited = next(p for p in all_products if p.stock > 0)
        cheapest = min(all_products, key=lambda p: p.price)
        # Один товар выключаем: модель не должна его ни найти, ни положить.
        hidden = max(all_products, key=lambda p: p.price)
        await repo.toggle_product(s, hidden.id)

    print("\n[1] Инструменты")
    async with session_factory() as s:
        ctx = ToolContext(session=s, bot=FakeBot(), user_id=5001, user_name="Тест")

        out = await tools.run("search_products", ctx, {"query": ""})
        check("пустой запрос отдаёт каталог", f"#{cheapest.id} " in out, out[:120])
        check("выключенный товар не находится", f"#{hidden.id} " not in out, out[:120])
        check("лимит выдачи держится", out.count("\n") <= tools.SEARCH_LIMIT, out)

        out = await tools.run("search_products", ctx, {"query": "", "max_price": cheapest.price})
        check("бюджет режет по цене", out.strip() == tools._product_line(cheapest), out)

        out = await tools.run("product_details", ctx, {"product_id": hidden.id})
        check("детали выключенного — «нет»", out == "Такого товара нет.", out)

        out = await tools.run("add_to_cart", ctx, {"product_id": limited.id, "qty": limited.stock + 5})
        got = await repo.cart_items(s, 5001)
        check("остаток нельзя обойти через qty", got and got[0].qty == limited.stock,
              f"{[(i.product_id, i.qty) for i in got]}")
        check("модели сказано, что положили меньше", "больше нет на складе" in out, out)

        out = await tools.run("add_to_cart", ctx, {"product_id": hidden.id})
        check("выключенный товар в корзину не попадает", "нет" in out and len(await repo.cart_items(s, 5001)) == 1, out)

        out = await tools.run("add_to_cart", ctx, {"product_id": cheapest.id, "qty": 0})
        check("qty=0 отбит", "от 1" in out, out)

        out = await tools.run("add_to_cart", ctx, {"product_id": cheapest.id, "price": 1})
        items = {i.product_id: i for i in await repo.cart_items(s, 5001)}
        check("лишний параметр price игнорируется, цена из базы",
              items[cheapest.id].product.price == cheapest.price, str(items))

        out = await tools.run("show_cart", ctx, {})
        check("корзина считается сервером", f"Итого: " in out and "Оформить" in out, out)

        out = await tools.run("teleport_money", ctx, {})
        check("выдуманный инструмент — ответ модели, не падение", "нет. Доступны" in out, out)

        out = await tools.run("product_details", ctx, {"product_id": "abc"})
        check("кривой тип аргумента — ошибка модели", out.startswith("Неверные аргументы"), out)

    print("\n[2] Эскалация менеджеру")
    async with session_factory() as s:
        bot = FakeBot(dead={778})
        ctx = ToolContext(session=s, bot=bot, user_id=5002, user_name="Ирина <b>")
        out = await tools.run("call_manager", ctx, {"summary": "хочет оптовую цену"})
        check("доставлено живому админу", [c for c, _ in bot.sent] == [777], str(bot.sent))
        check("мёртвый админ не ломает ответ", out.startswith("Менеджеру передано"), out)
        check("имя клиента экранировано", "&lt;b&gt;" in bot.sent[0][1], bot.sent[0][1])
        out = await tools.run("call_manager", ctx, {"summary": ""})
        check("пустая суть отбита", "summary" in out, out)

    print("\n[3] Цикл агента")
    async with session_factory() as s:
        llm = FakeProvider([
            Reply(tool_name="search_products", tool_args={"query": ""}),
            Reply(tool_name="add_to_cart", tool_args={"product_id": cheapest.id}),
            Reply(text="Положил в корзину, что-то ещё?"),
        ])
        agent = Agent(llm)
        ctx = ToolContext(session=s, bot=FakeBot(), user_id=5003, user_name="Т")
        reply = await agent.ask(ctx, "хочу что-нибудь недорогое")
        check("финальный текст вернулся", reply == "Положил в корзину, что-то ещё?", reply)
        check("три вызова модели", len(llm.calls) == 3, str(len(llm.calls)))
        second = llm.calls[1]
        check("результат инструмента дошёл до модели",
              second[-1]["role"] == "function" and f"#{cheapest.id} " in second[-1]["content"],
              str(second[-1])[:120])
        check("system prompt первым", llm.calls[0][0]["role"] == "system")
        items = await repo.cart_items(s, 5003)
        check("корзина реально изменилась", [i.product_id for i in items] == [cheapest.id])

        reply = await agent.ask(ctx, "спасибо")
        hist = llm.calls[-1]
        check("история без служебных сообщений",
              all(m["role"] in ("system", "user", "assistant") and "function_call" not in m for m in hist),
              str([m["role"] for m in hist]))
        check("история хранит прошлый ответ", any(m.get("content") == "Положил в корзину, что-то ещё?" for m in hist))

        # Модель просит инструмент MAX_TOOL_ROUNDS раз подряд: последний, добивающий
        # вызов должен прийти без инструментов и взять текст.
        llm = FakeProvider([Reply(tool_name="show_cart")] * MAX_TOOL_ROUNDS + [Reply(text="стоп")])
        agent = Agent(llm)
        reply = await agent.ask(ctx, "цикл")
        check("цикл рвётся на лимите", len(llm.calls) == MAX_TOOL_ROUNDS + 1 and reply == "стоп",
              f"{len(llm.calls)} вызовов, ответ {reply!r}")
        check("последний вызов без инструментов", llm.tools_seen[-1] == 0 and llm.tools_seen[0] > 0,
              str(llm.tools_seen))

        class Boom:
            async def complete(self, messages, tools):
                raise ConnectionError("нет сети")

        agent = Agent(Boom())
        reply = await agent.ask(ctx, "привет")
        check("ошибка провайдера — человеческий ответ", reply == FALLBACK_TEXT, reply)
        check("упавший вопрос не остался в истории", agent._history.get(5003, []) == [])

    print("\n[4] Выключенная фича")
    check("провайдер по пустому имени — None", build_provider("") is None)
    kb = main_menu()
    texts = [b.text for row in kb.keyboard for b in row]
    check("кнопки консультанта нет в меню", "🤖 Консультант" not in texts, str(texts))
    try:
        build_provider("gigachat", credentials="")
        check("gigachat без ключа падает явно", False)
    except ValueError as e:
        check("gigachat без ключа падает явно", "GIGACHAT_CREDENTIALS" in str(e), str(e))
    try:
        build_provider("skynet")
        check("неизвестный провайдер отбит", False)
    except ValueError:
        check("неизвестный провайдер отбит", True)

    print(f"\nИтого: {ok} OK, {fail} FAIL")
    if DB_FILE.exists():
        DB_FILE.unlink()
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(run())
