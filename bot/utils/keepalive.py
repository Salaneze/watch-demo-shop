"""Самопинг публичного адреса, чтобы бесплатный контейнер не засыпал.

Render free усыпляет сервис после ~15 минут без входящих HTTP-запросов, а
просыпается он 30-50 секунд: первый покупатель за вечер смотрит на пустой
экран витрины, а webhook Telegram уезжает в повтор. Запрос уходит на свой же
публичный домен — наружу и обратно через прокси платформы, поэтому считается
входящим трафиком (обращение к localhost платформа не видит).

Расход часов инстанса при этом максимальный: на free-тарифе 750 часов в месяц
на аккаунт против 730 в месяце — бодрствовать круглосуточно может только один
сервис. Второй такой же уронит оба в середине месяца.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from bot.config import settings

log = logging.getLogger(__name__)

# Пинг не должен переживать цикл: висящий запрос сдвигает следующий, и пауза
# незаметно уезжает за порог засыпания.
_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def ping_once(url: str) -> int | None:
    """Код ответа, либо None если достучаться не вышло."""
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url) as resp:
                return resp.status
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Keep-alive: %s недоступен (%s)", url, exc)
        return None


async def run_keepalive(url: str, interval: float) -> None:
    while True:
        # Сон первым: сразу после старта сервис и так бодр.
        await asyncio.sleep(interval)
        status = await ping_once(url)
        if status is not None and status != 200:
            log.warning("Keep-alive: %s ответил %s", url, status)


def start_keepalive() -> asyncio.Task | None:
    """Поднять пинг или сказать в лог, почему его нет."""
    if settings.keepalive_minutes <= 0:
        log.info("Keep-alive выключен (KEEPALIVE_MINUTES=0)")
        return None
    if not settings.has_webapp:
        # Локально публичного адреса нет и пинговать нечего — это норма,
        # но молчать нельзя: на хостинге та же ветка означает потерянный сон.
        log.info("Keep-alive не запущен: публичный HTTPS-адрес не определён")
        return None
    url = settings.public_url + "/healthz"
    log.info("Keep-alive: %s раз в %s мин", url, settings.keepalive_minutes)
    return asyncio.create_task(
        run_keepalive(url, settings.keepalive_minutes * 60),
        name="keepalive",
    )
