"""REST для витрины Mini App.

Логика не дублируется: все операции идут через тот же `bot.db.repo`, что и кнопки
бота. Здесь только проверка подписи, преобразование в схемы и HTTP-коды.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import InitDataError, WebAppUser, init_data_from_header, parse_init_data
from api.schemas import (
    CartLine,
    CartOut,
    CartPatch,
    CatalogOut,
    CategoryOut,
    LoginPoll,
    LoginStart,
    OrderIn,
    OrderItemOut,
    OrderOut,
    ProductOut,
)
from api.session import deep_link, issue_token, read_token
from api.session import store as login_store
from bot.config import settings
from bot.db import repo
from bot.db.base import session_factory
from bot.db.models import Product, status_ru
from bot.utils.money import fmt

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)

MEDIA_DIR = Path("media")


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


async def current_user(
    authorization: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),
) -> WebAppUser:
    """Пускает дальше только с подписью Telegram.

    Ошибку наружу отдаём одной формулировкой: подробности («подпись не сошлась»,
    «протух») помогают подбирать, а честному фронту всё равно нечего с ними делать —
    он просто перезапустит приложение.
    """
    # Клиенты без initData (моды Telegram) приходят с сессионным токеном,
    # выданным после подтверждения через самого бота.
    if authorization and authorization.lower().startswith("tma-session "):
        data = read_token(authorization.split(" ", 1)[1])
        if data is None:
            log.warning("Отказ в доступе: сессионный токен недействителен")
            raise HTTPException(status_code=401, detail="Сессия истекла, войдите заново")
        user = WebAppUser(id=data["id"], username=data.get("u"),
                          full_name=data.get("n") or f"id{data['id']}", language_code=None)
        await repo.upsert_user(session, user.id, user.username, user.full_name)
        return user

    try:
        user = parse_init_data(init_data_from_header(authorization))
    except InitDataError as exc:
        # В лог — причина и длина строки, но не сама строка: внутри id, имя и
        # username покупателя. Клиенту причину не отдаём, чтобы не помогать
        # подбирать подпись, но без неё невозможно отличить «мод Telegram вырезал
        # initData» от настоящей попытки взлома.
        raw_len = len(authorization or "") - 4  # минус схема "tma "
        log.warning(
            "Отказ в доступе: %s (длина initData: %s, клиент: %s)",
            exc, max(raw_len, 0), (user_agent or "?")[:120],
        )
        raise HTTPException(status_code=401, detail="Откройте магазин через Telegram")
    await repo.upsert_user(session, user.id, user.username, user.full_name)
    return user


CurrentUser = Annotated[WebAppUser, Depends(current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


def _image_url(p: Product) -> str | None:
    return f"/api/img/{p.id}" if p.photo_file_id else None


def _product_out(p: Product) -> ProductOut:
    return ProductOut(
        id=p.id,
        title=p.title,
        description=p.description,
        price=p.price,
        price_text=fmt(p.price),
        image=_image_url(p),
        category_id=p.category_id,
    )


@router.post("/auth/start", response_model=LoginStart)
async def auth_start(request: Request) -> LoginStart:
    """Выдаёт одноразовый код и ссылку на бота для входа без initData."""
    bot = getattr(request.app.state, "bot", None)
    username = getattr(request.app.state, "bot_username", None)
    if bot is None or not username:
        raise HTTPException(status_code=503, detail="Вход через бота сейчас недоступен")
    code = login_store.issue()
    return LoginStart(code=code, link=deep_link(code, username))


@router.get("/auth/poll", response_model=LoginPoll)
async def auth_poll(code: str) -> LoginPoll:
    """Витрина спрашивает, подтвердил ли человек вход в чате."""
    pending = login_store.take(code)
    if pending is None:
        raise HTTPException(status_code=404, detail="Код входа устарел")
    if pending.user_id is None:
        return LoginPoll(ready=False, token=None)
    return LoginPoll(
        ready=True,
        token=issue_token(pending.user_id, pending.username, pending.full_name),
    )


@router.get("/catalog", response_model=CatalogOut)
async def catalog(session: Session) -> CatalogOut:
    """Каталог открыт без подписи намеренно.

    Товары и цены и так видит любой, кто написал боту, — секрета в них нет.
    Зато человек с модифицированным клиентом увидит витрину, а не экран отказа.
    Подпись требуется дальше: корзина, заказы и всё, что привязано к личности.
    """
    categories = await repo.active_categories(session)
    out = []
    for c in categories:
        products = await repo.products_in_category(session, c.id)
        out.append(
            CategoryOut(id=c.id, title=c.title, products=[_product_out(p) for p in products])
        )
    return CatalogOut(currency=settings.currency.upper(), categories=out)


async def _cart_out(session: AsyncSession, user_id: int, removed: list[str]) -> CartOut:
    items = await repo.cart_items(session, user_id)
    lines = [
        CartLine(
            product_id=i.product_id,
            title=i.product.title,
            price=i.product.price,
            qty=i.qty,
            sum=i.product.price * i.qty,
            sum_text=fmt(i.product.price * i.qty),
            image=_image_url(i.product),
        )
        for i in items
    ]
    total = sum(line.sum for line in lines)
    return CartOut(lines=lines, total=total, total_text=fmt(total), removed=removed)


@router.get("/cart", response_model=CartOut)
async def cart(user: CurrentUser, session: Session) -> CartOut:
    # Товар могли снять с продажи, пока корзина лежала собранной — покупателю
    # честно говорим, что именно исчезло, а не молча уменьшаем сумму.
    removed = await repo.drop_inactive_from_cart(session, user.id)
    return await _cart_out(session, user.id, removed)


@router.post("/cart", response_model=CartOut)
async def cart_patch(patch: CartPatch, user: CurrentUser, session: Session) -> CartOut:
    product = await repo.product(session, patch.product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=404, detail="Товар больше не продаётся")
    await repo.cart_add(session, user.id, patch.product_id, patch.delta)
    return await _cart_out(session, user.id, [])


@router.delete("/cart", response_model=CartOut)
async def cart_clear(user: CurrentUser, session: Session) -> CartOut:
    await repo.cart_clear(session, user.id)
    return await _cart_out(session, user.id, [])


def _order_out(order) -> OrderOut:
    return OrderOut(
        id=order.id,
        status=str(order.status),
        status_text=status_ru(order.status),
        total=order.total,
        total_text=fmt(order.total),
        created_at=order.created_at.isoformat(),
        items=[
            OrderItemOut(title=i.title, price=i.price, qty=i.qty) for i in order.items
        ],
    )


@router.post("/order", response_model=OrderOut)
async def create_order(data: OrderIn, request: Request, user: CurrentUser,
                       session: Session) -> OrderOut:
    removed = await repo.drop_inactive_from_cart(session, user.id)
    if removed:
        raise HTTPException(
            status_code=409,
            detail="Из корзины пропали товары: " + ", ".join(removed),
        )
    try:
        order = await repo.create_order(
            session, user.id, data.name, data.phone, data.address, data.comment,
            expected_total=data.expected_total,
        )
    except repo.CartChanged as exc:
        # 409: витрина покажет свежую корзину и попросит подтвердить заново.
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError:
        raise HTTPException(status_code=400, detail="Корзина пуста")

    notify = getattr(request.app.state, "notify_admins", None)
    if notify is not None:
        await notify(order)
    return _order_out(order)


@router.get("/orders", response_model=list[OrderOut])
async def my_orders(user: CurrentUser, session: Session) -> list[OrderOut]:
    orders = await repo.orders_by_user(session, user.id, limit=10)
    return [_order_out(o) for o in orders]


@router.get("/img/{product_id}")
async def product_image(product_id: int, request: Request, session: Session) -> FileResponse:
    """Отдаёт фото товара, скачивая его из Telegram один раз.

    В БД лежит `photo_file_id` — идентификатор внутри Telegram, в `<img src>` его не
    подставить. Ссылка из getFile живёт около часа, поэтому файл кладём на диск и
    дальше раздаём сами.

    Без проверки подписи намеренно: тег <img> не умеет слать заголовки, а картинка
    товара и так публична — она видна каждому в боте. Но снятый с продажи товар
    публичным быть перестал, поэтому его картинка тоже закрывается.
    """
    product = await repo.product(session, product_id)
    if product is None or not product.is_active or not product.photo_file_id:
        raise HTTPException(status_code=404, detail="Нет картинки")

    MEDIA_DIR.mkdir(exist_ok=True)
    # Имя от file_id: сменили фото товара — старый кэш не подсунется.
    name = hashlib.sha1(product.photo_file_id.encode()).hexdigest()[:16] + ".jpg"
    path = MEDIA_DIR / name

    if not path.exists():
        bot = getattr(request.app.state, "bot", None)
        if bot is None:
            raise HTTPException(status_code=503, detail="Загрузка картинок недоступна")
        file = await bot.get_file(product.photo_file_id)
        await bot.download_file(file.file_path, destination=path)

    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
