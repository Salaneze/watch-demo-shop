"""Запуск витрины вместе с ботом: .venv\\Scripts\\python.exe -m api

`python -m bot` продолжает поднимать только бота — режим без витрины никуда не делся.
"""
import logging
import sys

import uvicorn

from bot.config import settings

class _SkipHealthz(logging.Filter):
    """Убирает health check из access-лога.

    Платформа стучится в `/healthz` каждые несколько секунд; без фильтра эти
    строки вытесняют из лога всё остальное и диагностика по нему невозможна.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # За обратным прокси слушать только localhost нельзя — платформа стучится
    # в контейнер снаружи и не достучится. Локально остаётся 127.0.0.1: незачем
    # выставлять отладочный сервер в свою сеть.
    host = "0.0.0.0" if settings.trust_proxy else settings.web_host  # noqa: S104

    logging.getLogger("uvicorn.access").addFilter(_SkipHealthz())

    uvicorn.run(
        "api.app:app",
        host=host,
        port=settings.web_port,
        log_level="info",
        # Заголовки реального адреса разбирает наш ограничитель, а не uvicorn.
        proxy_headers=False,
    )
