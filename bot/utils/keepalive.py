"""Самопинг публичного адреса, чтобы бесплатный контейнер не засыпал.

Render free усыпляет сервис после ~15 минут без входящих HTTP-запросов, а
просыпается он 30-50 секунд: первый покупатель за вечер смотрит на пустой
экран витрины, а webhook Telegram уезжает в повтор. Запрос уходит на свой же
публичный домен — наружу и обратно через прокси платформы, поэтому считается
входящим трафиком (обращение к localhost платформа не видит).

Расход часов инстанса при этом максимальный: на free-тарифе 750 часов в месяц
на аккаунт против 744 в длинном месяце — бодрствовать круглосуточно может только
один сервис. Поэтому пинг работает в окне KEEPALIVE_HOURS (по умолчанию 8-24 по
Москве), ночью сервис спит и отдаёт часы соседям.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from bot.config import settings

log = logging.getLogger(__name__)

# Пинг не должен переживать цикл: висящий запрос сдвигает следующий, и пауза
# незаметно уезжает за порог засыпания.
_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Москва без перехода на летнее время с 2014 года, фиксированного сдвига хватает;
# zoneinfo на Windows без пакета tzdata падает, а локально тесты гоняются там.
MSK = timezone(timedelta(hours=3))


def parse_hours(spec: str) -> tuple[int, int] | None:
    """«8-24» -> (8, 24); пусто или «0-24» -> None, то есть круглосуточно.

    Кривое значение — ValueError: упасть на старте лучше, чем молча спать
    сутками или жечь часы. Render при упавшем деплое оставляет живой прошлый."""
    spec = spec.strip()
    if not spec:
        return None
    try:
        start, end = (int(p) for p in spec.split("-"))
    except ValueError:
        raise ValueError(f"KEEPALIVE_HOURS={spec!r}: нужен формат «8-24»") from None
    if not (0 <= start <= 23 and 1 <= end <= 24) or start == end:
        raise ValueError(f"KEEPALIVE_HOURS={spec!r}: часы 0-23 и 1-24, начало не равно концу")
    if (start, end) == (0, 24):
        return None
    return start, end


def awake(window: tuple[int, int] | None, now: datetime | None = None) -> bool:
    if window is None:
        return True
    hour = (now or datetime.now(timezone.utc)).astimezone(MSK).hour
    start, end = window
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # окно через полночь, «22-6»


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


async def run_keepalive(url: str, interval: float,
                        window: tuple[int, int] | None = None) -> None:
    while True:
        # Сон первым: сразу после старта сервис и так бодр.
        await asyncio.sleep(interval)
        # Вне окна цикл молчит, и через 15 минут Render усыпляет контейнер целиком.
        # Изнутри утром будить некому: процесса нет. Будит GitHub Actions
        # (.github/workflows/wake.yml, 08:00 МСК), дальше держит этот цикл.
        # Меняешь окно — поправь cron там же.
        if not awake(window):
            continue
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
    window = parse_hours(settings.keepalive_hours)
    url = settings.public_url + "/healthz"
    log.info("Keep-alive: %s раз в %s мин, %s", url, settings.keepalive_minutes,
             "круглосуточно" if window is None else f"с {window[0]} до {window[1]} МСК")
    return asyncio.create_task(
        run_keepalive(url, settings.keepalive_minutes * 60, window),
        name="keepalive",
    )
