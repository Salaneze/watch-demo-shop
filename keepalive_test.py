"""Проверка самопинга: когда задача поднимается, куда стучится, переживает ли отказ.

Запуск: .venv\\Scripts\\python.exe keepalive_test.py
"""
import asyncio
import os
import sys

os.environ.setdefault("BOT_TOKEN", "123456:AAHtest-token-placeholder_0000000000")
os.environ["DB_URL"] = "sqlite+aiosqlite:///keepalive_test.db"

from aiohttp import web  # noqa: E402

from bot.config import settings  # noqa: E402
from bot.utils import keepalive  # noqa: E402

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ok, fail = 0, 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label} {extra}")


async def serve(hits: list[str], status: int = 200):
    """Поднять сервер-мишень на свободном порту, вернуть (base_url, runner)."""
    async def handler(request: web.Request) -> web.Response:
        hits.append(request.path)
        return web.Response(status=status, text="ok")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return f"http://127.0.0.1:{port}", runner


async def main():
    print("\n[1] Когда задача не поднимается")
    settings.keepalive_minutes = 0
    check("KEEPALIVE_MINUTES=0 — задачи нет", keepalive.start_keepalive() is None)

    settings.keepalive_minutes = 10
    settings.webapp_url = ""
    os.environ.pop("RENDER_EXTERNAL_URL", None)
    check("без публичного адреса — задачи нет", keepalive.start_keepalive() is None)

    print("\n[2] Пинг уходит на /healthz")
    hits: list[str] = []
    base, runner = await serve(hits)
    code = await keepalive.ping_once(base + "/healthz")
    check("код ответа 200", code == 200, code)
    check("запрос пришёл на /healthz", hits == ["/healthz"], hits)

    print("\n[3] Цикл: сон первым, дальше по интервалу")
    hits.clear()
    task = asyncio.create_task(keepalive.run_keepalive(base + "/healthz", 0.2))
    await asyncio.sleep(0.1)
    check("сразу после старта не пингует", hits == [], hits)
    await asyncio.sleep(0.45)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    check("за два интервала два пинга", len(hits) == 2, len(hits))

    print("\n[4] Отказ мишени не убивает цикл")
    await runner.cleanup()
    dead = base + "/healthz"  # порт уже никто не слушает
    check("недоступный адрес — None, без исключения", await keepalive.ping_once(dead) is None)

    task = asyncio.create_task(keepalive.run_keepalive(dead, 0.1))
    await asyncio.sleep(0.35)
    alive = not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    check("цикл жив после серии отказов", alive)

    print("\n[5] Задача поднимается, когда адрес есть")
    settings.webapp_url = "https://example.invalid"
    started = keepalive.start_keepalive()
    check("задача создана", started is not None)
    if started is not None:
        started.cancel()
        await asyncio.gather(started, return_exceptions=True)

    print(f"\nOK: {ok} FAIL: {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
