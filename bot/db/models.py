from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderStatus(StrEnum):
    new = "new"
    awaiting_payment = "awaiting_payment"
    paid = "paid"
    shipped = "shipped"
    cancelled = "cancelled"


# Ключи — обычные строки: из SQLite статус приходит как str, а Enum.__hash__
# считается по имени члена, поэтому смешивать str и Enum в одном dict опасно.
STATUS_RU = {
    "new": "🆕 новый",
    "awaiting_payment": "⏳ ждёт оплаты",
    "paid": "✅ оплачен",
    "shipped": "📦 отправлен",
    "cancelled": "❌ отменён",
}


def status_ru(status: str) -> str:
    return STATUS_RU.get(str(status), str(status))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram id
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128))
    sort: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[int] = mapped_column(Integer)  # в рублях, без копеек (v1)
    photo_file_id: Mapped[str | None] = mapped_column(String(255))
    stock: Mapped[int] = mapped_column(Integer, default=-1)  # -1 = без ограничения
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Характеристики карточки: [["Материал", "сталь"], ["Диаметр", "40 мм"]].
    # Список пар, а не словарь: порядок строк в таблице задаёт продавец, а
    # словарь в JSON его гарантирует только по счастливой случайности.
    specs: Mapped[list] = mapped_column(JSON, default=list)
    # Дополнительные фото для галереи, теми же идентификаторами, что photo_file_id.
    # Первым кадром идёт photo_file_id, здесь только второй и дальше.
    extra_photos: Mapped[list] = mapped_column(JSON, default=list)

    category: Mapped[Category] = relationship(back_populates="products")


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_cart_user_product"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int] = mapped_column(Integer, default=1)

    product: Mapped[Product] = relationship()


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[OrderStatus] = mapped_column(String(32), default=OrderStatus.new)
    # total — то, что человек платит: товары минус скидка плюс доставка.
    # Слагаемые хранятся отдельно, иначе через месяц не восстановить, почему
    # в заказе стоит именно эта сумма, а промокод к тому времени уже удалён.
    items_total: Mapped[int] = mapped_column(Integer, default=0)
    discount: Mapped[int] = mapped_column(Integer, default=0)
    promo_code: Mapped[str] = mapped_column(String(32), default="")
    delivery_method: Mapped[str] = mapped_column(String(32), default="pickup")
    delivery_cost: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer)
    contact_name: Mapped[str] = mapped_column(String(255))
    contact_phone: Mapped[str] = mapped_column(String(64))
    address: Mapped[str] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(Text, default="")
    payment_method: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(128))  # снимок на момент заказа
    price: Mapped[int] = mapped_column(Integer)      # снимок
    qty: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="items")


class Favorite(Base):
    """Отложенные товары. Живут отдельно от корзины: сердечко ни к чему не обязывает."""

    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_fav_user_product"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product] = relationship()


class Promo(Base):
    """Промокод со скидкой в процентах.

    Счётчик использований хранится здесь же: «10 первым покупателям» без него
    превращается в «всем желающим», а узнаём мы об этом по выручке.
    """

    __tablename__ = "promos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    percent: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=-1)  # -1 = без ограничения
    used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Кто и что менял в магазине.

    При одном администраторе кажется лишним, при двух это первый источник споров:
    «я не менял цену», «заказ отменил не я». Пишется отдельной строкой, ничего не
    перезаписывает.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(32))       # order_status, product_add, ...
    target: Mapped[str] = mapped_column(String(64))       # что именно затронуто
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
