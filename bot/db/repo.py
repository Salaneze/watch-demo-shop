"""Доступ к данным. Каждая функция принимает открытую AsyncSession."""
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.db.models import (
    AuditLog,
    CartItem,
    Category,
    Favorite,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    Promo,
    User,
)
from bot.utils.delivery import DEFAULT_DELIVERY, Totals, quote


# ---------- users ----------

async def upsert_user(s: AsyncSession, tg_id: int, username: str | None, full_name: str | None) -> None:
    user = await s.get(User, tg_id)
    if user is None:
        s.add(User(id=tg_id, username=username, full_name=full_name))
    else:
        user.username = username
        user.full_name = full_name
    await s.commit()


# ---------- catalog ----------

async def active_categories(s: AsyncSession) -> list[Category]:
    res = await s.scalars(
        select(Category).where(Category.is_active).order_by(Category.sort, Category.id)
    )
    return list(res)


async def category(s: AsyncSession, category_id: int) -> Category | None:
    return await s.get(Category, category_id)


# Как витрина умеет сортировать каталог. Ключ приходит из запроса, поэтому
# порядок задаётся здесь, а не собирается из строки: getattr(Product, поле)
# по пользовательскому вводу — это способ отдать наружу любую колонку таблицы.
SORT_ORDERS = {
    "default": (Product.id.asc(),),
    "price_asc": (Product.price.asc(), Product.id.asc()),
    "price_desc": (Product.price.desc(), Product.id.asc()),
    "title": (Product.title.asc(),),
}
DEFAULT_SORT = "default"


async def products_in_category(
    s: AsyncSession,
    category_id: int,
    *,
    query: str = "",
    sort: str = DEFAULT_SORT,
) -> list[Product]:
    stmt = select(Product).where(Product.category_id == category_id, Product.is_active)

    query = query.strip()
    if query:
        # Ищем и по названию, и по описанию: «нато» человек напишет, помня
        # ремешок, а не его артикул. like_escape — чтобы % в запросе искал
        # процент, а не «что угодно».
        pattern = f"%{_like_escape(query)}%"
        stmt = stmt.where(
            or_(
                Product.title.ilike(pattern, escape="\\"),
                Product.description.ilike(pattern, escape="\\"),
            )
        )

    return list(await s.scalars(stmt.order_by(*SORT_ORDERS.get(sort, SORT_ORDERS[DEFAULT_SORT]))))


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def product(s: AsyncSession, product_id: int) -> Product | None:
    return await s.get(Product, product_id)


# ---------- favorites ----------

async def favorite_ids(s: AsyncSession, user_id: int) -> set[int]:
    res = await s.scalars(select(Favorite.product_id).where(Favorite.user_id == user_id))
    return set(res)


async def favorite_toggle(s: AsyncSession, user_id: int, product_id: int) -> bool:
    """Ставит или снимает сердечко. Возвращает состояние ПОСЛЕ переключения."""
    existing = await s.scalar(
        select(Favorite).where(Favorite.user_id == user_id, Favorite.product_id == product_id)
    )
    if existing is not None:
        await s.delete(existing)
        await s.commit()
        return False
    s.add(Favorite(user_id=user_id, product_id=product_id))
    await s.commit()
    return True


async def catalog_is_empty(s: AsyncSession) -> bool:
    count = await s.scalar(select(func.count()).select_from(Category))
    return not count


# ---------- cart ----------

async def cart_items(s: AsyncSession, user_id: int) -> list[CartItem]:
    """Только позиции, доступные к покупке.

    Товар мог быть снят с продажи уже после того, как попал в корзину —
    показывать и продавать его нельзя. Мёртвые позиции вычищает
    drop_inactive_from_cart().
    """
    res = await s.scalars(
        select(CartItem)
        .join(Product, CartItem.product_id == Product.id)
        .where(CartItem.user_id == user_id, Product.is_active)
        .options(selectinload(CartItem.product))
        .order_by(CartItem.id)
    )
    return list(res)


async def drop_inactive_from_cart(s: AsyncSession, user_id: int) -> list[str]:
    """Убирает из корзины снятые с продажи товары. Возвращает их названия,
    чтобы покупателю можно было объяснить, почему корзина изменилась."""
    res = await s.scalars(
        select(CartItem)
        .join(Product, CartItem.product_id == Product.id)
        .where(CartItem.user_id == user_id, Product.is_active.is_(False))
        .options(selectinload(CartItem.product))
    )
    dead = list(res)
    if not dead:
        return []

    titles = [i.product.title for i in dead]
    for item in dead:
        await s.delete(item)
    await s.commit()
    return titles


async def _stock_limit(s: AsyncSession, product_id: int) -> int | None:
    """Сколько штук вообще можно положить. None — ограничения нет."""
    p = await s.get(Product, product_id)
    if p is None or p.stock < 0:  # -1 = без ограничения
        return None
    return p.stock


async def cart_add(s: AsyncSession, user_id: int, product_id: int, delta: int = 1) -> int:
    limit = await _stock_limit(s, product_id)
    item = await s.scalar(
        select(CartItem).where(CartItem.user_id == user_id, CartItem.product_id == product_id)
    )
    if item is None:
        if delta <= 0:
            return 0
        qty = delta if limit is None else min(delta, limit)
        if qty <= 0:
            return 0
        item = CartItem(user_id=user_id, product_id=product_id, qty=qty)
        s.add(item)
        await s.commit()
        return qty
    item.qty += delta
    if item.qty <= 0:
        await s.delete(item)
        await s.commit()
        return 0
    # Остаток мог уменьшиться уже после того, как товар попал в корзину.
    if limit is not None:
        item.qty = min(item.qty, limit)
    await s.commit()
    return item.qty


async def cart_clear(s: AsyncSession, user_id: int) -> None:
    await s.execute(delete(CartItem).where(CartItem.user_id == user_id))
    await s.commit()


async def cart_total(s: AsyncSession, user_id: int) -> int:
    items = await cart_items(s, user_id)
    return sum(i.product.price * i.qty for i in items)


async def cart_quote(
    s: AsyncSession, user_id: int, delivery: str = DEFAULT_DELIVERY, promo_code: str = ""
) -> Totals:
    """Итог по текущей корзине — то же, что посчитает create_order.

    Нужна для превью в чате: показать одну сумму, а списать другую нельзя, а
    считать её вторым куском кода — прямой путь к расхождению.
    """
    return await quote(s, await cart_total(s, user_id), delivery, promo_code)


# ---------- orders ----------

class CartChanged(Exception):
    """Корзина разъехалась с тем, что видел покупатель: цена или остаток."""


async def create_order(
    s: AsyncSession,
    user_id: int,
    name: str,
    phone: str,
    address: str,
    comment: str,
    expected_total: int | None = None,
    delivery: str = DEFAULT_DELIVERY,
    promo_code: str = "",
) -> Order:
    items = await cart_items(s, user_id)
    if not items:
        raise ValueError("empty cart")

    items_total = sum(i.product.price * i.qty for i in items)

    # Скидка и доставка считаются здесь, а не берутся из запроса. Клиент присылает
    # только код способа и текст промокода — иначе «курьер за 0» отправляется одним
    # curl. Расхождение с тем, что покупатель видел, поймает сверка expected_total.
    totals = await quote(s, items_total, delivery, promo_code)
    promo, discount, option, total = (
        totals.promo, totals.discount, totals.option, totals.total
    )

    # Цену показывали до того, как покупатель заполнил форму. Если админ успел её
    # поменять, молча списывать новую нельзя — заказ отклоняется, витрина покажет
    # свежую корзину. Сверяем итог целиком: подстановка чужого промокода на
    # последнем шаге тоже меняет сумму и тоже должна ломать заказ.
    if expected_total is not None and expected_total != total:
        raise CartChanged(
            f"сумма изменилась: было {expected_total}, стало {total}"
        )

    # Остаток мог кончиться, пока корзина лежала собранной.
    for i in items:
        if i.product.stock >= 0 and i.qty > i.product.stock:
            raise CartChanged(
                f"«{i.product.title}»: осталось {i.product.stock}, в корзине {i.qty}"
            )

    # Корзину забираем ДО создания заказа и смотрим, сколько строк реально удалили.
    # Две одновременные отправки (двойной тап по кнопке) доходят сюда обе, но
    # удаление строк достанется только одной — вторая увидит ноль и уйдёт ни с чем.
    taken = await s.execute(delete(CartItem).where(CartItem.user_id == user_id))
    if taken.rowcount == 0:
        await s.rollback()
        raise ValueError("empty cart")

    for i in items:
        if i.product.stock >= 0:
            i.product.stock -= i.qty

    if promo is not None:
        promo.used += 1

    order = Order(
        user_id=user_id,
        status=OrderStatus.awaiting_payment,
        items_total=items_total,
        discount=discount,
        promo_code=promo.code if promo is not None else "",
        delivery_method=option.code,
        delivery_cost=option.cost,
        total=total,
        contact_name=name,
        contact_phone=phone,
        address=address,
        comment=comment,
    )
    order.items = [
        OrderItem(product_id=i.product_id, title=i.product.title, price=i.product.price, qty=i.qty)
        for i in items
    ]
    s.add(order)
    # Корзина уже удалена выше — это и был захват заявки.
    await s.commit()
    await s.refresh(order, ["items"])
    return order


async def order(s: AsyncSession, order_id: int) -> Order | None:
    return await s.scalar(
        select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    )


async def orders_by_user(s: AsyncSession, user_id: int, limit: int = 10) -> list[Order]:
    """Заказы конкретного покупателя, свежие первыми.

    Фильтр обязан быть в SQL: выбрать последние N заказов магазина и отсеять
    чужих в Python — значит потерять заказ пользователя, как только поверх него
    придёт N чужих.
    """
    res = await s.scalars(
        select(Order)
        .where(Order.user_id == user_id)
        .options(selectinload(Order.items))
        .order_by(Order.id.desc())
        .limit(limit)
    )
    return list(res)


async def orders_by_status(s: AsyncSession, status: OrderStatus | None = None, limit: int = 20) -> list[Order]:
    q = select(Order).options(selectinload(Order.items)).order_by(Order.id.desc()).limit(limit)
    if status is not None:
        q = q.where(Order.status == status)
    res = await s.scalars(q)
    return list(res)


async def set_order_status(s: AsyncSession, order_id: int, status: OrderStatus) -> Order | None:
    o = await s.get(Order, order_id)
    if o is None:
        return None
    o.status = status
    await s.commit()
    return o


# ---------- admin: catalog edit ----------

async def add_promo(s: AsyncSession, code: str, percent: int, max_uses: int = -1) -> Promo:
    promo = Promo(code=code.strip().upper(), percent=percent, max_uses=max_uses)
    s.add(promo)
    await s.commit()
    await s.refresh(promo)
    return promo


async def add_category(s: AsyncSession, title: str) -> Category:
    c = Category(title=title)
    s.add(c)
    await s.commit()
    await s.refresh(c)
    return c


async def add_product(
    s: AsyncSession,
    category_id: int,
    title: str,
    description: str,
    price: int,
    photo_file_id: str | None,
    *,
    stock: int = -1,
    specs: list | None = None,
    extra_photos: list | None = None,
) -> Product:
    p = Product(
        category_id=category_id,
        title=title,
        description=description,
        price=price,
        photo_file_id=photo_file_id,
        stock=stock,
        specs=specs or [],
        extra_photos=extra_photos or [],
    )
    s.add(p)
    await s.commit()
    await s.refresh(p)
    return p


async def toggle_product(s: AsyncSession, product_id: int) -> Product | None:
    p = await s.get(Product, product_id)
    if p is None:
        return None
    p.is_active = not p.is_active
    await s.commit()
    return p


async def log_action(s: AsyncSession, admin_id: int, action: str, target: str,
                     details: str = "") -> None:
    """Запись в журнал действий администратора. Не должна ронять само действие."""
    s.add(AuditLog(admin_id=admin_id, action=action, target=target, details=details))
    await s.commit()


async def recent_actions(s: AsyncSession, limit: int = 20) -> list[AuditLog]:
    res = await s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
    return list(res)
