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

    print("\n[6] Окно часов")
    from datetime import datetime, timezone
    check("пусто — круглосуточно", keepalive.parse_hours("") is None)
    check("0-24 — круглосуточно", keepalive.parse_hours("0-24") is None)
    check("8-24 разобрано", keepalive.parse_hours(" 8-24 ") == (8, 24))
    for bad in ("8", "8-8", "25-3", "a-b", "8-24-1", "-1-5"):
        try:
            keepalive.parse_hours(bad)
            check(f"{bad!r} отвергнут", False)
        except ValueError:
            check(f"{bad!r} отвергнут", True)

    def at(h_utc, m=0):
        return datetime(2026, 10, 19, h_utc, m, tzinfo=timezone.utc)
    w = (8, 24)
    # граница по Москве: 05:00 UTC = 08:00 МСК
    check("07:59 МСК спит", not keepalive.awake(w, at(4, 59)))
    check("08:00 МСК бодр", keepalive.awake(w, at(5, 0)))
    check("23:59 МСК бодр", keepalive.awake(w, at(20, 59)))
    check("00:00 МСК спит", not keepalive.awake(w, at(21, 0)))
    night = (22, 6)
    check("через полночь: 23 МСК бодр", keepalive.awake(night, at(20)))
    check("через полночь: 03 МСК бодр", keepalive.awake(night, at(0)))
    check("через полночь: 12 МСК спит", not keepalive.awake(night, at(9)))
    check("без окна бодр всегда", keepalive.awake(None, at(0)))

    print("\n[7] Вне окна цикл не стучится")
    hits = []
    base, runner = await serve(hits)
    real_awake = keepalive.awake
    keepalive.awake = lambda window, now=None: False
    task = asyncio.create_task(keepalive.run_keepalive(base + "/healthz", 0.1, (8, 24)))
    await asyncio.sleep(0.35)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    keepalive.awake = real_awake
    await runner.cleanup()
    check("ни одного пинга", hits == [], hits)

    print("\n[8] Кривое окно роняет старт")
    settings.keepalive_hours = "утро"
    try:
        keepalive.start_keepalive()
        check("ValueError на старте", False)
    except ValueError:
        check("ValueError на старте", True)
    settings.keepalive_hours = "8-24"

    print(f"\nOK: {ok} FAIL: {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
