"""Проверка подписи initData, которую Telegram кладёт в Mini App.

Единственная защита API: фронт присылает строку с данными пользователя и подписью,
сервер пересчитывает HMAC ключом из токена бота. Без этой проверки любой человек
с curl оформит заказ от чужого имени и прочитает чужую корзину — в вебе нет
"сообщения от Telegram", есть только то, что прислал клиент.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from bot.config import settings

# Сколько живёт подпись. Telegram срок не задаёт, час — общепринятый дефолт:
# достаточно на долгую сессию покупателя и мало для переигровки украденной строки.
DEFAULT_TTL = 3600


class InitDataError(Exception):
    """Подпись не сошлась, протухла или в строке нет пользователя."""


@dataclass(frozen=True)
class WebAppUser:
    id: int
    username: str | None
    full_name: str
    language_code: str | None

    @property
    def is_admin(self) -> bool:
        return self.id in settings.admins


def _secret_key(bot_token: str) -> bytes:
    # Порядок именно такой: ключ — литерал "WebAppData", сообщение — токен бота.
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def parse_init_data(raw: str, *, ttl: int = DEFAULT_TTL) -> WebAppUser:
    """Проверяет подпись и возвращает пользователя. Кидает InitDataError на любой отказ."""
    if not raw:
        raise InitDataError("пустой initData")

    try:
        pairs = dict(parse_qsl(raw, strict_parsing=True))
    except ValueError as exc:
        raise InitDataError("initData не разбирается как query string") from exc

    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("в initData нет hash")

    # Строка проверки: пары key=value, отсортированные по ключу, склеенные переводом строки.
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    calculated = hmac.new(
        _secret_key(settings.bot_token), check_string.encode(), hashlib.sha256
    ).hexdigest()

    # compare_digest, а не ==: обычное сравнение выходит на первом несовпавшем байте
    # и по времени ответа подпись можно подобрать посимвольно.
    if not hmac.compare_digest(calculated, received):
        raise InitDataError("подпись не сошлась")

    try:
        auth_date = int(pairs.get("auth_date", ""))
    except ValueError as exc:
        raise InitDataError("auth_date не число") from exc
    age = time.time() - auth_date
    if age > ttl:
        raise InitDataError(f"initData протух: {int(age)}с > {ttl}с")

    raw_user = pairs.get("user")
    if not raw_user:
        raise InitDataError("в initData нет user")
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise InitDataError("user не разбирается как JSON") from exc

    user_id = user.get("id")
    if not isinstance(user_id, int):
        raise InitDataError("у пользователя нет числового id")

    full_name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name")) if part
    )
    return WebAppUser(
        id=user_id,
        username=user.get("username"),
        full_name=full_name or f"id{user_id}",
        language_code=user.get("language_code"),
    )


def init_data_from_header(header: str | None) -> str:
    """Достаёт initData из заголовка `Authorization: tma <initData>`.

    Заголовок, а не query-параметр: URL оседает в логах прокси и в истории браузера,
    а там лежат имя и id покупателя.
    """
    if not header:
        raise InitDataError("нет заголовка Authorization")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "tma" or not value:
        raise InitDataError("ожидался Authorization: tma <initData>")
    return value
