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
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from api.routes import CurrentUser, Session, _image_url, _order_out, iso_utc
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


# ---------------------------------------------------------------- каталог
# Таблица вместо пошагового /addproduct в боте: поменять цену у десяти товаров
# через FSM — это десять диалогов по пять вопросов. Фото здесь не грузятся:
# photo_file_id выдаёт Telegram, поэтому снимки по-прежнему шлются боту.

PRICE_MAX = 10_000_000
STOCK_MAX = 1_000_000


class _ProductFields(BaseModel):
    # Strict-типы: в лаксе pydantic превратил бы "120" и true в число, и
    # опечатка в клиенте молча поставила бы цену 1.
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, max_length=128)
    description: str | None = Field(None, max_length=2000)
    price: StrictInt | None = Field(None, ge=1, le=PRICE_MAX)
    stock: StrictInt | None = Field(None, ge=-1, le=STOCK_MAX)
    category_id: StrictInt | None = None

    @field_validator("title", "description")
    @classmethod
    def strip_text(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str | None) -> str | None:
        if v is not None and len(v) < 2:
            raise ValueError("название короче двух символов")
        return v


class ProductPatch(_ProductFields):
    is_active: StrictBool | None = None


class ProductNew(_ProductFields):
    title: str = Field(max_length=128)
    price: StrictInt = Field(ge=1, le=PRICE_MAX)
    category_id: StrictInt
    stock: StrictInt = Field(-1, ge=-1, le=STOCK_MAX)


def _product_row(p) -> dict:
    return {
        "id": p.id, "category_id": p.category_id, "title": p.title,
        "description": p.description, "price": p.price, "price_text": fmt(p.price),
        "stock": p.stock, "is_active": p.is_active,
        # Картинку API отдаёт только у товара на витрине — у скрытого была бы 404.
        "image": _image_url(p) if p.is_active else None,
    }


async def _known_category(session, category_id: int) -> None:
    if await repo.category(session, category_id) is None:
        raise HTTPException(status_code=422, detail="Нет такой категории")


@router.get("/products")
async def products(session: Session, user=Admin) -> dict:
    return {
        "categories": [{"id": c.id, "title": c.title, "is_active": c.is_active}
                       for c in await repo.all_categories(session)],
        "products": [_product_row(p) for p in await repo.all_products(session)],
    }


@router.patch("/products/{product_id}")
async def product_patch(product_id: int, data: ProductPatch, session: Session,
                        user=Admin) -> dict:
    p = await repo.product(session, product_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    if "category_id" in changes:
        await _known_category(session, changes["category_id"])
    # Журнал пишет «было → стало»: через неделю «кто поставил цену 1» без
    # старого значения не разобрать.
    diff = [f"{k}: {getattr(p, k)!r} → {v!r}" for k, v in changes.items() if getattr(p, k) != v]
    for k, v in changes.items():
        setattr(p, k, v)
    await session.commit()
    if diff:
        await repo.log_action(session, user.id, "product_edit", f"product#{p.id}",
                              "; ".join(diff)[:500])
    return _product_row(p)


@router.post("/products")
async def product_create(data: ProductNew, session: Session, user=Admin) -> dict:
    await _known_category(session, data.category_id)
    p = await repo.add_product(session, data.category_id, data.title, data.description or "",
                               data.price, None, stock=data.stock)
    await repo.log_action(session, user.id, "product_add", f"product#{p.id}",
                          f"{p.title} · {fmt(p.price)} · веб-панель")
    return _product_row(p)
