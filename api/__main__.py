"""Запуск витрины вместе с ботом: .venv\\Scripts\\python.exe -m api

`python -m bot` продолжает поднимать только бота — режим без витрины никуда не делся.
"""
import logging
import sys

import uvicorn

from bot.config import settings

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    uvicorn.run(
        "api.app:app",
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
    )
