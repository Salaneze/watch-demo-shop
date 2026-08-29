"""Вход для клиентов, которые не отдают initData.

Модифицированные клиенты (AyuGram и подобные) режут подпись Telegram, и витрина
у них не может доказать, кто её открыл. Обходной путь через «пришли свой id в
адресе» — не путь, а дыра: id прислал бы сам клиент, и любой подставил бы чужой.

Здесь личность подтверждает Telegram: витрина показывает ссылку на бота с
одноразовым кодом, человек жмёт «Старт», и апдейт с этим кодом приходит боту
**через серверы Telegram**. Значит отправитель — настоящий владелец аккаунта.
После этого витрина обменивает код на сессионный токен, подписанный тем же
секретом, что и всё остальное.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from bot.config import settings

# Сколько живёт одноразовый код, пока человек ходит в чат и обратно.
NONCE_TTL = 300
# Сколько живёт выданная сессия.
SESSION_TTL = 24 * 3600

_PREFIX = "wa_"


@dataclass
class _Pending:
    created: float
    user_id: int | None = None
    username: str | None = None
    full_name: str | None = None


class LoginStore:
    """Одноразовые коды входа. В памяти процесса — как и счётчики лимитов."""

    def __init__(self) -> None:
        self._codes: dict[str, _Pending] = {}

    def _sweep(self) -> None:
        now = time.time()
        for code in [c for c, p in self._codes.items() if now - p.created > NONCE_TTL]:
            del self._codes[code]

    def issue(self) -> str:
        self._sweep()
        code = secrets.token_urlsafe(16)
        self._codes[code] = _Pending(created=time.time())
        return code

    def confirm(self, code: str, user_id: int, username: str | None,
                full_name: str | None) -> bool:
        """Вызывается ботом, когда пришёл /start с этим кодом."""
        self._sweep()
        pending = self._codes.get(code)
        if pending is None:
            return False
        pending.user_id = user_id
        pending.username = username
        pending.full_name = full_name
        return True

    def take(self, code: str) -> _Pending | None:
        """Забирает подтверждённый код. Одноразово: второй раз не сработает."""
        self._sweep()
        pending = self._codes.get(code)
        if pending is None or pending.user_id is None:
            return pending  # либо нет вовсе, либо ещё не подтверждён
        del self._codes[code]
        return pending


store = LoginStore()


def deep_link(code: str, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start={_PREFIX}{code}"


def code_from_start(payload: str) -> str | None:
    """Достаёт код из аргумента /start, если он наш."""
    if payload and payload.startswith(_PREFIX):
        return payload[len(_PREFIX):]
    return None


def _sign(raw: bytes) -> str:
    # Отдельный контекст подписи: сессионный токен не должен быть перепутан
    # с initData, даже если оба считаются от одного секрета.
    key = hmac.new(b"WebAppSession", settings.bot_token.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(hmac.new(key, raw, hashlib.sha256).digest()).decode().rstrip("=")


def issue_token(user_id: int, username: str | None, full_name: str | None) -> str:
    payload = json.dumps(
        {"id": user_id, "u": username, "n": full_name, "exp": int(time.time()) + SESSION_TTL},
        separators=(",", ":"), ensure_ascii=False,
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def read_token(token: str) -> dict | None:
    """Разбирает сессионный токен. None — если подпись не сошлась или он протух."""
    body, _, signature = token.partition(".")
    if not body or not signature:
        return None
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception:
        return None
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data.get("id"), int) or data.get("exp", 0) < time.time():
        return None
    return data
