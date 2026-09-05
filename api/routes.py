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
    DeliveryOut,
    FavoriteOut,
    FavoritePatch,
    LoginPoll,
    LoginStart,
    OrderIn,
    OrderItemOut,
    OrderOut,
    ProductOut,
    PromoIn,
    PromoOut,
)
from api.session import deep_link, issue_token, read_token
from api.session import store as login_store
from bot.config import settings
from bot.db import repo
from bot.db.base import session_factory
from bot.db.models import Product, status_ru
from bot.utils.delivery import (
    DELIVERY_OPTIONS,
    delivery_option,
    discount_for,
    resolve_promo,
)
from bot.utils.money import fmt
from bot.utils.seed import seed_art_path

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


def _image_version(file_id: str) -> str:
    """Метка версии картинки для адреса.

    Картинки отдаются с суточным Cache-Control, поэтому без метки замена фото
    доходит до покупателя через сутки: адрес `/api/img/7` не изменился, и браузер
    даже по Ctrl+Shift+R берёт старый файл из дискового кэша (проверено на живой
    витрине). Для файла из Telegram достаточно самого file_id — новое фото
    получает новый идентификатор. Для демо-рисунка идентификатор постоянный,
    поэтому в метку идёт время изменения файла на диске.
    """
    art = seed_art_path(file_id)
    stamp = f"{file_id}:{art.stat().st_mtime_ns}" if art is not None else file_id
    return hashlib.sha1(stamp.encode()).hexdigest()[:8]


def _image_url(p: Product) -> str | None:
    if not p.photo_file_id:
        return None
    return f"/api/img/{p.id}?v={_image_version(p.photo_file_id)}"


def _image_urls(p: Product) -> list[str]:
    """Адреса всех кадров галереи. Число в адресе — позиция в общем списке."""
    if not p.photo_file_id:
        return []
    urls = [_image_url(p)]
    for n, file_id in enumerate(p.extra_photos or [], start=1):
        urls.append(f"/api/img/{p.id}/{n}?v={_image_version(file_id)}")
    return urls


def _product_out(p: Product, favorites: set[int] | None = None) -> ProductOut:
    return ProductOut(
        id=p.id,
        title=p.title,
        description=p.description,
        price=p.price,
        price_text=fmt(p.price),
        image=_image_url(p),
        images=_image_urls(p),
        category_id=p.category_id,
        stock=p.stock,
        # У товаров, заведённых до появления характеристик, поля просто нет —
        # карточка тогда обходится без таблицы, а не падает.
        specs=[[str(k), str(v)] for k, v in (p.specs or [])],
        is_favorite=bool(favorites and p.id in favorites),
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
async def catalog(session: Session, q: str = "", sort: str = repo.DEFAULT_SORT) -> CatalogOut:
    """Каталог открыт без подписи намеренно.

    Товары и цены и так видит любой, кто написал боту, — секрета в них нет.
    Зато человек с модифицированным клиентом увидит витрину, а не экран отказа.
    Подпись требуется дальше: корзина, заказы и всё, что привязано к личности.

    Поиск и сортировка идут сюда же параметрами: держать отдельный /search
    значило бы дублировать сборку категорий ради одного `where`. Пустые
    категории после фильтра не показываем — вкладка без товаров выглядит
    поломкой, а не результатом поиска.
    """
    if sort not in repo.SORT_ORDERS:
        raise HTTPException(status_code=400, detail="Неизвестная сортировка")

    categories = await repo.active_categories(session)
    out = []
    for c in categories:
        products = await repo.products_in_category(session, c.id, query=q, sort=sort)
        if not products and q:
            continue
        out.append(
            CategoryOut(id=c.id, title=c.title, products=[_product_out(p) for p in products])
        )
    return CatalogOut(currency=settings.currency.upper(), categories=out)


@router.get("/delivery", response_model=list[DeliveryOut])
async def delivery_options() -> list[DeliveryOut]:
    """Способы доставки с ценами. Витрина их только показывает — считает сервер."""
    return [
        DeliveryOut(code=o.code, title=o.title, note=o.note, cost=o.cost, cost_text=fmt(o.cost))
        for o in DELIVERY_OPTIONS
    ]


@router.get("/favorites", response_model=list[ProductOut])
async def favorites(user: CurrentUser, session: Session) -> list[ProductOut]:
    ids = await repo.favorite_ids(session, user.id)
    products = [await repo.product(session, pid) for pid in sorted(ids)]
    # Товар мог быть снят с продажи, пока лежал в отложенных: показываем только живые,
    # но запись не удаляем — вернут в продажу, и сердечко останется на месте.
    return [_product_out(p, ids) for p in products if p is not None and p.is_active]


@router.post("/favorites", response_model=FavoriteOut)
async def favorite_toggle(patch: FavoritePatch, user: CurrentUser, session: Session) -> FavoriteOut:
    product = await repo.product(session, patch.product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=404, detail="Товар больше не продаётся")
    state = await repo.favorite_toggle(session, user.id, patch.product_id)
    return FavoriteOut(product_id=patch.product_id, is_favorite=state)


@router.post("/promo", response_model=PromoOut)
async def check_promo(patch: PromoIn, user: CurrentUser, session: Session) -> PromoOut:
    """Считает скидку для текущей корзины, ничего не меняя.

    Ответ 200 и с недействительным кодом: «нет такого промокода» — это нормальный
    исход проверки, а не ошибка запроса, и витрине проще показать текст из поля
    message, чем разбирать коды ошибок.
    """
    promo = await resolve_promo(session, patch.code)
    items_total = await repo.cart_total(session, user.id)
    if promo is None:
        return PromoOut(valid=False, code=patch.code.strip().upper(), percent=0,
                        discount=0, discount_text=fmt(0),
                        message="Такого промокода нет или он уже не действует")
    discount = discount_for(items_total, promo)
    return PromoOut(valid=True, code=promo.code, percent=promo.percent, discount=discount,
                    discount_text=fmt(discount), message=f"Скидка {promo.percent}%")


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
    # Заказы, оформленные до появления доставки, лежат в базе без разбивки:
    # items_total у них 0, поэтому за сумму товаров берём общий итог.
    items_total = order.items_total or order.total
    try:
        delivery_title = delivery_option(order.delivery_method).title
    except ValueError:
        # Способ доставки могли убрать из прайса уже после оформления — заказ
        # от этого не перестал существовать, показываем как есть.
        delivery_title = order.delivery_method

    return OrderOut(
        id=order.id,
        status=str(order.status),
        status_text=status_ru(order.status),
        items_total=items_total,
        items_total_text=fmt(items_total),
        discount=order.discount,
        discount_text=fmt(order.discount),
        promo_code=order.promo_code,
        delivery_title=delivery_title,
        delivery_cost=order.delivery_cost,
        delivery_cost_text=fmt(order.delivery_cost),
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
            delivery=data.delivery,
            promo_code=data.promo_code,
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


@router.get("/img/{product_id}/{index}")
async def product_image_extra(product_id: int, index: int, request: Request,
                              session: Session) -> FileResponse:
    """Второй и следующие кадры галереи. Первый живёт по адресу без индекса."""
    return await product_image(product_id, request, session, index=index)


@router.get("/img/{product_id}")
async def product_image(product_id: int, request: Request, session: Session,
                        index: int = 0) -> FileResponse:
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

    # Индекс 0 — основное фото, дальше идут кадры галереи по порядку.
    if index:
        extra = product.extra_photos or []
        if not 1 <= index <= len(extra):
            raise HTTPException(status_code=404, detail="Нет картинки")
        file_id = extra[index - 1]
    else:
        file_id = product.photo_file_id

    # Демо-товары ссылаются на рисунок из репозитория, а не на файл в Telegram.
    art = seed_art_path(file_id)
    if art is not None:
        return FileResponse(art, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    MEDIA_DIR.mkdir(exist_ok=True)
    # Имя от file_id: сменили фото товара — старый кэш не подсунется.
    name = hashlib.sha1(file_id.encode()).hexdigest()[:16] + ".jpg"
    path = MEDIA_DIR / name

    if not path.exists():
        bot = getattr(request.app.state, "bot", None)
        if bot is None:
            raise HTTPException(status_code=503, detail="Загрузка картинок недоступна")
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, destination=path)

    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
