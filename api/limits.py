"""Ограничение частоты запросов и размера тела.

Без этого API открыт нараспашку: можно долбить оформление заказов, перебирать
номера товаров и заставлять сервер ходить в Telegram за файлами картинок.
Проверено живьём до появления этого модуля — 40 запросов подряд без единого отказа.

Счётчики держатся в памяти процесса. Для одного контейнера этого достаточно;
при нескольких репликах счётчики нужно выносить в Redis — тогда меняется только
хранилище, а правила остаются те же.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from bot.config import settings

# Правила: путь (по началу строки) -> (сколько запросов, за сколько секунд).
# Оформление заказа строже всего: это конец воронки, туда незачем ломиться часто.
RULES: list[tuple[str, int, int]] = [
    ("POST /api/order", 5, 60),
    ("POST /api/cart", 40, 60),
    ("GET /api/img", 60, 60),
    ("", 120, 60),  # общий предел на всё остальное
]

# Тело больше этого сервер даже не читает: заказ — это несколько сотен байт.
MAX_BODY = 16 * 1024


@dataclass
class _Bucket:
    hits: list[float] = field(default_factory=list)


class RateLimiter:
    """Скользящее окно: помним времена последних запросов по каждому ключу."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._last_cleanup = time.monotonic()

    def _cleanup(self, now: float) -> None:
        # Раз в минуту выкидываем ключи, по которым давно тихо, иначе словарь
        # растёт вечно и превращается в утечку памяти.
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        dead = [
            key for key, bucket in self._buckets.items()
            if not bucket.hits or now - bucket.hits[-1] > 300
        ]
        for key in dead:
            del self._buckets[key]

    def check(self, client: str, method: str, path: str) -> int | None:
        """Возвращает время ожидания в секундах, если лимит исчерпан."""
        now = time.monotonic()
        self._cleanup(now)
        target = f"{method} {path}"

        for prefix, limit, window in RULES:
            if not target.startswith(prefix):
                continue
            bucket = self._buckets.setdefault((client, prefix), _Bucket())
            bucket.hits = [t for t in bucket.hits if now - t < window]
            if len(bucket.hits) >= limit:
                return int(window - (now - bucket.hits[0])) + 1
            bucket.hits.append(now)
        return None


def client_key(request: Request) -> str:
    """Кто именно стучится.

    За туннелем и обратным прокси реальный адрес приходит заголовком, а
    `request.client` показывает сам прокси. Заголовку верим только когда
    подключение действительно пришло с локального адреса либо когда мы явно
    объявлены стоящими за прокси (`TRUST_PROXY=true`) — иначе его подставит
    кто угодно и обойдёт ограничитель, меняя значение на каждый запрос.

    Без этого флага на хостинге всё наоборот: прокси приходит с внутреннего
    адреса, заголовок игнорируется, и все посетители попадают в общую корзину —
    один активный покупатель начинает упираться в лимит за всех остальных.
    """
    direct = request.client.host if request.client else "?"
    if settings.trust_proxy or direct in {"127.0.0.1", "::1", "localhost"}:
        forwarded = (
            request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        )
        if forwarded:
            return forwarded
    return direct


class LimitsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app) -> None:
        super().__init__(app)
        self.limiter = RateLimiter()

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/api"):
            return await call_next(request)

        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY:
            return JSONResponse(
                {"detail": "Слишком большой запрос"},
                status_code=413,
            )

        retry = self.limiter.check(client_key(request), request.method, request.url.path)
        if retry is not None:
            return JSONResponse(
                {"detail": "Слишком часто. Подождите немного."},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )

        return await call_next(request)
