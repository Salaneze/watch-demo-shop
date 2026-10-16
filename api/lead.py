"""Заявки с внешних лендингов: форма на чужом домене -> сообщение админам бота.

Лендинг статический (GitHub Pages, любой хостинг), своего сервера у него нет,
а токен бота в страницу класть нельзя — его вытащит любой из исходника. Поэтому
форма шлёт POST сюда, а в Telegram пишет уже этот процесс.

Выключено по умолчанию: пока `LEAD_ORIGINS` пуст, эндпоинт отвечает 404 и не
выдаёт, что он вообще есть.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from bot.config import settings
from bot.utils.text import esc

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)

# Потолок на весь процесс поверх лимита на адрес из limits.py. Тот держит
# одного настойчивого, этот — ботнет с сотней адресов: иначе владельцу за ночь
# прилетает тысяча уведомлений, и настоящую заявку в них уже не найти.
GLOBAL_LIMIT = 30
GLOBAL_WINDOW = 3600
_recent: deque[float] = deque()


class LeadIn(BaseModel):
    # Обрезка до проверки длины: иначе имя из трёх пробелов проходит как заполненное.
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)
    # Нормализованный номер: плюс и цифры. Маску рисует страница, сюда она не доходит.
    phone: str = Field(pattern=r"^\+\d{10,15}$")
    car: str = Field(default="", max_length=80)
    comment: str = Field(default="", max_length=1000)
    # Ловушка для ботов: человек это поле не видит и не заполняет.
    website: str = Field(default="", max_length=200)


def allowed(origin: str | None) -> bool:
    return bool(origin) and origin in settings.lead_origin_list


def cors_headers(origin: str) -> dict[str, str]:
    # Отражаем только свой домен из списка, не `*`: со звёздочкой форму можно
    # повесить на любой сайт и слать заявки нашим ботом от чужого имени.
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


def over_global_limit(now: float) -> bool:
    while _recent and now - _recent[0] > GLOBAL_WINDOW:
        _recent.popleft()
    if len(_recent) >= GLOBAL_LIMIT:
        return True
    _recent.append(now)
    return False


def lead_text(lead: LeadIn, origin: str) -> str:
    site = urlsplit(origin).netloc or origin
    lines = [
        f"📩 <b>Заявка с сайта</b> ({esc(site)})",
        "",
        f"Имя: {esc(lead.name)}",
        f"Телефон: <code>{esc(lead.phone)}</code>",
    ]
    if lead.car:
        lines.append(f"Авто: {esc(lead.car)}")
    if lead.comment:
        lines += ["", esc(lead.comment)]
    return "\n".join(lines)


@router.options("/lead", include_in_schema=False)
async def lead_preflight(origin: str | None = Header(None)) -> Response:
    if not settings.lead_origin_list:
        return Response(status_code=404)
    if not allowed(origin):
        return Response(status_code=403)
    return Response(status_code=204, headers=cors_headers(origin))


@router.post("/lead", include_in_schema=False)
async def lead(
    request: Request,
    origin: str | None = Header(None),
) -> JSONResponse:
    if not settings.lead_origin_list:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    # Origin подделывается curl-ом за секунду, это не защита, а отсечка чужих
    # страниц в браузере. От долбёжки защищают лимиты.
    if not allowed(origin):
        return JSONResponse({"detail": "Чужой сайт"}, status_code=403)
    headers = cors_headers(origin)

    # Тело разбираем сами, а не параметром ручки: при ошибке валидации FastAPI
    # ответит 422 без CORS-заголовков, и браузер покажет на странице сетевую
    # ошибку вместо понятного «проверьте поля».
    try:
        lead_in = LeadIn.model_validate(await request.json())
    except Exception:
        return JSONResponse({"detail": "Проверьте поля формы"}, status_code=422, headers=headers)

    if lead_in.website:
        # Бот заполнил ловушку. Отвечаем как обычно, чтобы не подсказать, на чём попался.
        log.info("Заявка с %s отброшена ловушкой", origin)
        return JSONResponse({"ok": True}, headers=headers)

    if over_global_limit(time.monotonic()):
        log.warning("Заявок больше %s за час — новые не пересылаю", GLOBAL_LIMIT)
        return JSONResponse({"detail": "Слишком много заявок"}, status_code=429, headers=headers)

    bot = request.app.state.bot
    text = lead_text(lead_in, origin)
    delivered = 0
    for admin_id in settings.admins:
        try:
            await bot.send_message(admin_id, text)
            delivered += 1
        except Exception as e:  # админ мог не нажать /start у бота
            log.warning("Заявку не доставил админу %s: %s", admin_id, e)

    if not delivered:
        # Честно говорим странице, что заявка потерялась: она покажет телефон,
        # и клиент позвонит, а не будет ждать звонка, которого не будет.
        return JSONResponse({"detail": "Не доставлено"}, status_code=502, headers=headers)
    return JSONResponse({"ok": True}, headers=headers)
