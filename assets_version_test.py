"""Метка версии статики считается по содержимому, а не по памяти разработчика.

Ручной `?v=N` в index.html однажды уже чуть не сорвал выкат: правки app.js
уехали на прод, а webview показывал старый файл, и выглядело это как
несработавший деплой. Тест сторожит, что метка меняется вместе с файлами.

Запуск: .venv\\Scripts\\python.exe assets_version_test.py
"""
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN-NOT-REAL"
os.environ["DB_URL"] = "sqlite+aiosqlite:///assets_version_test.db"
os.environ["SEED"] = "plain"
os.environ["CURRENCY"] = "USD"

from api.app import VERSIONED_ASSETS, WEBAPP_DIR, asset_version, index_html  # noqa: E402

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


def run() -> None:
    print("\n[1] Метка версии подставляется в разметку")
    version = asset_version()
    html = index_html()
    check("отпечаток непустой", len(version) == 8, version)
    check("метка попала в app.js", f"app.js?v={version}" in html, html[:200])
    check("метка попала в style.css", f"style.css?v={version}" in html, html[:200])
    # Сверка с кавычкой: отпечаток 5fb4051a начинается с «5», и голое «?v=5»
    # ложно краснело на честной метке.
    check("ручной ?v= не остался", '?v=5"' not in html)
    # 21.09: комментарий в index.html упоминает `?v=`, и подмена по всему файлу
    # съела `<link rel=` вместе с телом страницы. Проверяем именно разметку.
    check("link остался на месте", '<link rel="stylesheet" href="/style.css?v=' in html)
    check("script остался на месте", '<script src="/app.js?v=' in html)
    check("тело страницы отдаётся", 'id="app"' in html and "<!-- " in html and " -->" in html)

    print("\n[2] Правка файла меняет метку")
    target = WEBAPP_DIR / VERSIONED_ASSETS[0]
    before = target.read_bytes()
    try:
        target.write_bytes(before + "\n// проверка отпечатка\n".encode("utf-8"))
        changed = asset_version()
        check("отпечаток сменился после правки", changed != version, f"{version} -> {changed}")
        check("разметка подхватила новую метку", f"?v={changed}" in index_html())
    finally:
        target.write_bytes(before)

    check("откат вернул прежний отпечаток", asset_version() == version)

    print("\n" + "=" * 46)
    print(f"OK: {ok}   FAIL: {fail}")
    print("=" * 46)
    db = Path("assets_version_test.db")
    if db.exists():
        db.unlink()
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    run()
