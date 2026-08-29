"""Доступ к данным. Каждая функция принимает открытую AsyncSession."""
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.db.models import (
    AuditLog,
    CartItem,
    Category,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    User,
)


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


async def products_in_category(s: AsyncSession, category_id: int) -> list[Product]:
    res = await s.scalars(
        select(Product)
        .where(Product.category_id == category_id, Product.is_active)
        .order_by(Product.id)
    )
    return list(res)


async def product(s: AsyncSession, product_id: int) -> Product | None:
    return await s.get(Product, product_id)


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
) -> Order:
    items = await cart_items(s, user_id)
    if not items:
        raise ValueError("empty cart")

    total = sum(i.product.price * i.qty for i in items)

    # Цену показывали до того, как покупатель заполнил форму. Если админ успел её
    # поменять, молча списывать новую нельзя — заказ отклоняется, витрина покажет
    # свежую корзину.
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

    order = Order(
        user_id=user_id,
        status=OrderStatus.awaiting_payment,
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
) -> Product:
    p = Product(
        category_id=category_id,
        title=title,
        description=description,
        price=price,
        photo_file_id=photo_file_id,
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
