"""Веб-панель админа: заказы в браузере.

Вход тот же, что у витрины: подпись Telegram (initData) или сессионный токен
после подтверждения в боте. Дальше — только id из ADMIN_IDS. Пароля у панели
нет намеренно: его пришлось бы хранить, менять и защищать от перебора, а
личность админа и так подтверждает Telegram.

Статусы меняются через admin_transition() — тот же путь, что у кнопок в
боте, поэтому журнал /audit и уведомление покупателю не расходятся.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.routes import CurrentUser, Session, _order_out, iso_utc
from bot.config import settings
from bot.db import repo
from bot.db.models import OrderStatus, status_ru
from bot.utils.admin_ops import admin_transition
from bot.utils.lifecycle import (
    CANCEL_REASON_MAX,
    InvalidTransition,
    allowed_next,
    note_required,
)
from bot.utils.money import fmt

router = APIRouter(prefix="/api/admin")
log = logging.getLogger(__name__)


def require_admin(user: CurrentUser):
    if user.id not in settings.admins:
        # Тот, кто не админ, о существовании панели узнавать не должен — но
        # 404 на каждый путь мешал бы честному админу понять, что он просто
        # вошёл не тем аккаунтом. Поэтому 403 с понятным текстом.
        log.warning("Панель: отказ id%s — не в ADMIN_IDS", user.id)
        raise HTTPException(status_code=403, detail="Этот аккаунт не админ магазина")
    return user


Admin = Depends(require_admin)


class TransitionIn(BaseModel):
    # Что админ видел на экране: если заказ уже ушёл дальше, переход не пройдёт.
    from_status: str = Field(max_length=32)
    to_status: str = Field(max_length=32)
    note: str = Field("", max_length=CANCEL_REASON_MAX)


def _row(o) -> dict:
    return {
        "id": o.id, "status": str(o.status), "status_text": status_ru(o.status),
        "contact_name": o.contact_name, "contact_phone": o.contact_phone,
        "total_text": fmt(o.total), "items_count": sum(i.qty for i in o.items),
        "delivery_method": o.delivery_method, "created_at": iso_utc(o.created_at),
    }


async def _card(session, order) -> dict:
    out = _order_out(order, await repo.history_for(session, order.id)).model_dump()
    out.update(
        contact_name=order.contact_name, contact_phone=order.contact_phone,
        address=order.address, comment=order.comment, user_id=order.user_id,
        delivery_method=order.delivery_method,
        items=[{"title": i.title, "qty": i.qty, "sum_text": fmt(i.price * i.qty)} for i in order.items],
        actions=[
            {"to": st, "title": status_ru(st), "note": note_required(st, order.delivery_method),
             "note_label": "Причина отмены — покупатель её увидит" if st == "cancelled" else "Трек-номер"}
            for st in allowed_next(order)
        ],
    )
    return out


@router.get("/me")
async def me(user=Admin) -> dict:
    return {"id": user.id, "name": user.full_name}


@router.get("/orders")
async def orders(session: Session, status: str = "", q: str = "", user=Admin) -> dict:
    if status and status not in {s.value for s in OrderStatus}:
        raise HTTPException(status_code=400, detail="Неизвестный статус")
    rows = await repo.search_orders(session, status or None, q[:64])
    counts = await repo.order_status_counts(session)
    return {
        "orders": [_row(o) for o in rows],
        "counts": counts,
        "statuses": [{"code": s.value, "title": status_ru(s.value)} for s in OrderStatus],
    }


@router.get("/orders/{order_id}")
async def order_card(order_id: int, session: Session, user=Admin) -> dict:
    order = await repo.order(session, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return await _card(session, order)


@router.post("/orders/{order_id}/transition")
async def transition(order_id: int, data: TransitionIn, request: Request, session: Session,
                     user=Admin) -> dict:
    bot = getattr(request.app.state, "bot", None)
    try:
        order, _, notified = await admin_transition(
            session, bot, user.id, order_id, data.from_status, data.to_status, data.note,
            source="веб-панель",
        )
    except InvalidTransition as exc:
        if not exc.current:
            raise HTTPException(status_code=404, detail="Заказ не найден")
        raise HTTPException(status_code=409, detail=f"Заказ уже в статусе «{status_ru(exc.current)}»")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    card = await _card(session, order)
    card["notified"] = notified
    return card
