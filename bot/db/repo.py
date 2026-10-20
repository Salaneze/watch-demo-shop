"""Доступ к данным. Каждая функция принимает открытую AsyncSession."""
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.db.models import (
    AdminOrderMessage,
    AuditLog,
    CartItem,
    Category,
    Favorite,
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    Product,
    Promo,
    Review,
    ReviewAsk,
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
        stmt = stmt.where(or_(*(
            _contains_ci(col, term)
            for term in [query, *_aliases(query)]
            for col in (Product.title, Product.description)
        )))

    return list(await s.scalars(stmt.order_by(*SORT_ORDERS.get(sort, SORT_ORDERS[DEFAULT_SORT]))))


# Демо-каталог на английском, а покупатель пишет по-русски: без этой таблицы
# «ремешок» и «кварц» дают пустую выдачу. Ключ сверяется по началу слова, чтобы
# работало и на «ремеш», и на «ремешки». Клиенту с каталогом на своём языке таблица не нужна.
# TODO: при переводе каталога на русский удалить вместе с _aliases.
SEARCH_ALIASES = {
    "часы": ("automatic", "quartz", "hand-wound"),
    "механика": ("automatic", "hand-wound"),
    "механические": ("automatic", "hand-wound"),
    "автомат": ("automatic",),
    "скелетон": ("skeleton",),
    "кварц": ("quartz",),
    "кварцевые": ("quartz",),
    "хронограф": ("chrono",),
    "дайвер": ("diver",),
    "полевые": ("field",),
    "ремешок": ("strap", "bracelet", "nato"),
    "ремень": ("strap", "nato"),
    "браслет": ("bracelet",),
    "миланский": ("milanese",),
    "кожа": ("leather",),
    "кожаный": ("leather",),
    "нейлон": ("nylon",),
    "нато": ("nato",),
    "набор": ("set",),
    "подарок": ("set",),
    "сталь": ("steel",),
    "титан": ("titanium",),
    "сапфир": ("sapphire",),
    "перламутр": ("mother-of-pearl",),
}


def _aliases(query: str) -> list[str]:
    q = query.lower()
    if len(q) < 3:  # «ча» совпало бы с половиной таблицы
        return []
    # Общая основа до пяти букв: «ремешки» находит «ремешок», «кварцевые» — «кварц»
    return [en for ru, terms in SEARCH_ALIASES.items()
            if q[:min(len(q), len(ru), 5)] == ru[:min(len(q), len(ru), 5)]
            for en in terms]


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains_ci(column, query: str):
    """Подстрока без учёта регистра, и для кириллицы тоже.

    ilike() тут не годится: в SQLite он снимает регистр только у латиницы.
    lower() с обеих сторон работает везде — в SQLite он подменён на
    юникодный в bot.db.base.
    """
    return func.lower(column).like(f"%{_like_escape(query.lower())}%", escape="\\")


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
    await s.flush()
    # Первая запись истории — здесь, а не ждём первого действия админа: засев
    # в init_db помечает «статус на момент миграции» любой заказ без истории,
    # и свежий заказ после рестарта выглядел бы как доисторический.
    s.add(OrderStatusHistory(
        order_id=order.id, status=str(order.status), actor="customer", actor_id=user_id,
    ))
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


async def search_orders(s: AsyncSession, status: str | None = None, query: str = "",
                        limit: int = 100) -> list[Order]:
    """Список для веб-панели: фильтр по статусу и поиск по номеру, имени, телефону.

    Номер ищем точным совпадением, а не LIKE: «12» не должно находить #112
    и #1200 — админ, набравший номер, хочет ровно этот заказ.
    """
    q = select(Order).options(selectinload(Order.items)).order_by(Order.id.desc()).limit(limit)
    if status:
        q = q.where(Order.status == status)
    query = query.strip()
    if query:
        conds = [_contains_ci(Order.contact_name, query)]
        digits = "".join(ch for ch in query if ch.isdigit())
        # Телефон целиком — тоже «одни цифры», но в int4 Postgres он не влезает,
        # и сравнение с id роняет запрос. Такой номер заказа не бывает, пропускаем.
        if query.lstrip("#").isdigit() and int(query.lstrip("#")) < 2**31:
            conds.append(Order.id == int(query.lstrip("#")))
        if len(digits) >= 4:
            # Телефон лежит как его набрал покупатель: «+7 (916) 555-12-34».
            # Срезаем разделители в самом SQL — replace() есть и в SQLite, и в Postgres.
            bare = Order.contact_phone
            for ch in (" ", "-", "(", ")", "+"):
                bare = func.replace(bare, ch, "")
            conds.append(bare.like(f"%{digits}%"))
        q = q.where(or_(*conds))
    return list(await s.scalars(q))


async def order_status_counts(s: AsyncSession) -> dict[str, int]:
    rows = await s.execute(select(Order.status, func.count()).group_by(Order.status))
    return {str(st): n for st, n in rows.all()}


async def set_order_status(s: AsyncSession, order_id: int, expected: str, new: str) -> bool:
    """Условный UPDATE: статус меняется, только если он всё ещё `expected`.

    Два админа на одной кнопке или кнопка в старом сообщении — оба случая
    сводятся к «строка не изменилась». SELECT FOR UPDATE в SQLite нет, а
    записи он сериализует сам, так что rowcount и есть атомарная проверка.
    Без commit: история должна лечь в ту же транзакцию, коммитит вызывающий.
    """
    res = await s.execute(
        update(Order).where(Order.id == order_id, Order.status == expected).values(status=new)
    )
    return res.rowcount == 1


async def add_status_entry(
    s: AsyncSession, order_id: int, status: str, actor: str, actor_id: int | None, note: str = "",
) -> OrderStatusHistory:
    entry = OrderStatusHistory(
        order_id=order_id, status=status, actor=actor, actor_id=actor_id, note=note,
    )
    s.add(entry)
    await s.flush()
    return entry


async def history_for(s: AsyncSession, order_id: int) -> list[OrderStatusHistory]:
    res = await s.scalars(
        select(OrderStatusHistory)
        .where(OrderStatusHistory.order_id == order_id)
        .order_by(OrderStatusHistory.created_at, OrderStatusHistory.id)
    )
    return list(res)


async def mark_not_notified(s: AsyncSession, entry_id: int) -> None:
    """Единственный UPDATE по истории. Дошло ли сообщение, известно только после
    коммита перехода, а откатывать запись ради чистоты append-only нельзя —
    потерять факт смены статуса хуже, чем поправить один флаг."""
    await s.execute(
        update(OrderStatusHistory).where(OrderStatusHistory.id == entry_id).values(notified=False)
    )
    await s.commit()


async def remember_admin_message(s: AsyncSession, order_id: int, chat_id: int, message_id: int) -> None:
    s.add(AdminOrderMessage(order_id=order_id, chat_id=chat_id, message_id=message_id))
    await s.commit()


async def admin_messages(s: AsyncSession, order_id: int) -> list[AdminOrderMessage]:
    res = await s.scalars(select(AdminOrderMessage).where(AdminOrderMessage.order_id == order_id))
    return list(res)


async def forget_admin_messages(s: AsyncSession, order_id: int) -> None:
    await s.execute(delete(AdminOrderMessage).where(AdminOrderMessage.order_id == order_id))
    await s.commit()


async def unpaid_older_than(s: AsyncSession, statuses: set[str], cutoff: datetime) -> list[Order]:
    res = await s.scalars(
        select(Order)
        .where(Order.status.in_(statuses), Order.created_at < cutoff)
        .options(selectinload(Order.items))
        .order_by(Order.id)
    )
    return list(res)


async def return_stock(s: AsyncSession, o: Order) -> None:
    """Вернуть позиции отменённого заказа в наличие. Без commit."""
    for item in o.items:
        p = await s.get(Product, item.product_id)
        # -1 = без ограничения: там нечего возвращать, и «-1 + qty» стало бы лимитом.
        if p is not None and p.stock >= 0:
            p.stock += item.qty


# ---------- reviews ----------

REVIEW_TEXT_MAX = 1000
REVIEWABLE_STATUS = "delivered"


class ReviewDenied(Exception):
    """Отзыв не принят. Текст — для человека, но без подробностей о чужих заказах."""


def public_name(full_name: str | None) -> str:
    """Имя для витрины: «Анна К.», а не полное имя из профиля Telegram.

    Отзыв видят все, а в профиле у людей бывает и фамилия, и отчество, и
    ник, по которому их находят. Первое слово и инициал второго — как у
    маркетплейсов, и покупатель не удивится, увидев себя в карточке.
    """
    # Слова без букв выкидываем: из «Даныло (тест)» инициалом становилась
    # скобка, а у имён с эмодзи — половинка суррогатной пары.
    parts = [w for w in (full_name or "").split() if any(c.isalpha() for c in w)]
    if not parts:
        return "Покупатель"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {next(c for c in parts[1] if c.isalpha()).upper()}."


async def _reviewable_order(s: AsyncSession, user_id: int, order_id: int) -> Order:
    o = await order(s, order_id)
    # Чужой и несуществующий заказ — одна ошибка, как и везде с номерами заказов.
    if o is None or o.user_id != user_id:
        raise ReviewDenied("Заказ не найден")
    if str(o.status) != REVIEWABLE_STATUS:
        raise ReviewDenied("Отзыв можно оставить после получения заказа")
    return o


async def reviewable_items(s: AsyncSession, user_id: int, order_id: int) -> list[OrderItem]:
    """Позиции заказа, на которые можно написать отзыв: по одной на товар.

    Товар, удалённый из базы, пропускаем — отзыв повис бы ни к чему.
    """
    o = await _reviewable_order(s, user_id, order_id)
    seen: set[int] = set()
    out = []
    for item in o.items:
        if item.product_id in seen or await s.get(Product, item.product_id) is None:
            continue
        seen.add(item.product_id)
        out.append(item)
    return out


async def add_review(
    s: AsyncSession,
    user_id: int,
    order_id: int,
    product_id: int,
    rating: int,
    text: str | None = None,
    author: str = "",
    photo_file_id: str | None = None,
) -> Review:
    """Создать или поправить отзыв. Единственная дверь для бота и витрины.

    text=None — текст не трогать (бот сначала сохраняет одни звёзды, текст
    приходит следующим сообщением или не приходит вовсе). Фото заменяется
    только новым фото: правка звёзд не должна стирать снимок.

    Скрытый админом отзыв после правки остаётся скрытым — иначе модерация
    обходится одной сменой оценки.
    """
    # bool — подкласс int, и True прошёл бы как «1 звезда».
    if type(rating) is not int or not 1 <= rating <= 5:
        raise ReviewDenied("Оценка — от 1 до 5")
    o = await _reviewable_order(s, user_id, order_id)
    if product_id not in {i.product_id for i in o.items}:
        raise ReviewDenied("Этого товара нет в заказе")
    if await s.get(Product, product_id) is None:
        raise ReviewDenied("Товар больше не продаётся")

    review = await s.scalar(
        select(Review).where(Review.user_id == user_id, Review.product_id == product_id)
    )
    if review is None:
        review = Review(user_id=user_id, product_id=product_id, text="")
        s.add(review)
    review.order_id = order_id
    review.rating = rating
    if author:
        review.author = author
    if text is not None:
        review.text = text.strip()[:REVIEW_TEXT_MAX]
    if photo_file_id:
        review.photo_file_id = photo_file_id
    await s.commit()
    await s.refresh(review)
    return review


async def review(s: AsyncSession, review_id: int) -> Review | None:
    return await s.get(Review, review_id)


async def product_reviews(s: AsyncSession, product_id: int, limit: int = 20) -> list[Review]:
    """Видимые отзывы товара, свежие первыми. Только звёзды без текста не
    показываем в ленте — пустая карточка с «★★★★☆» ничего не говорит, а в
    средней оценке они и так учтены."""
    res = await s.scalars(
        select(Review)
        .where(Review.product_id == product_id, Review.is_hidden.is_(False),
               or_(Review.text != "", Review.photo_file_id.is_not(None)))
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(limit)
    )
    return list(res)


async def rating_stats(s: AsyncSession) -> dict[int, tuple[float, int]]:
    """product_id -> (средняя оценка, число оценок). Скрытые не считаются.

    Одним запросом на весь каталог: по запросу на товар витрина из 20
    позиций делала бы 20 походов в базу на каждый поиск.
    """
    rows = await s.execute(
        select(Review.product_id, func.avg(Review.rating), func.count())
        .where(Review.is_hidden.is_(False))
        .group_by(Review.product_id)
    )
    return {pid: (round(float(avg), 1), int(n)) for pid, avg, n in rows}


async def user_ratings(s: AsyncSession, user_id: int) -> dict[int, int]:
    """Какие товары человек уже оценил и на сколько — для кнопок в заказах."""
    rows = await s.execute(
        select(Review.product_id, Review.rating).where(Review.user_id == user_id)
    )
    return {pid: r for pid, r in rows}


async def set_review_hidden(s: AsyncSession, review_id: int, hidden: bool) -> Review | None:
    r = await s.get(Review, review_id)
    if r is None:
        return None
    r.is_hidden = hidden
    await s.commit()
    return r


async def orders_to_ask_review(s: AsyncSession, cutoff: datetime, floor: datetime) -> list[Order]:
    """Выданные заказы, которым пора напомнить об отзыве.

    Выдан в окне [floor, cutoff), напоминания ещё не было и отзыва по заказу
    нет. Нижняя граница — чтобы первый запуск на живой базе не разослал
    просьбы по заказам полугодовой давности.
    """
    delivered = (
        select(OrderStatusHistory.order_id)
        .where(OrderStatusHistory.status == REVIEWABLE_STATUS,
               OrderStatusHistory.created_at < cutoff,
               OrderStatusHistory.created_at >= floor)
    )
    asked = select(ReviewAsk.order_id)
    reviewed = select(Review.order_id).where(Review.order_id.is_not(None))
    res = await s.scalars(
        select(Order)
        .where(Order.status == REVIEWABLE_STATUS,
               Order.id.in_(delivered), Order.id.not_in(asked), Order.id.not_in(reviewed))
        .options(selectinload(Order.items))
        .order_by(Order.id)
    )
    return list(res)


async def mark_review_asked(s: AsyncSession, order_id: int) -> bool:
    """Отметить, что просьба ушла. False — кто-то успел раньше (второй процесс)."""
    if await s.get(ReviewAsk, order_id) is not None:
        return False
    s.add(ReviewAsk(order_id=order_id))
    await s.commit()
    return True


async def repeat_order(s: AsyncSession, user_id: int, order_id: int) -> tuple[list[str], list[str]]:
    """Положить позиции старого заказа в корзину. → (что положили, что не вышло).

    Цена берётся текущая, а не из заказа: корзина всегда по сегодняшнему
    прайсу, и итог покупатель увидит до оформления. Кончился или снят с
    продажи — называем, а не теряем молча.
    """
    o = await order(s, order_id)
    if o is None or o.user_id != user_id:
        raise LookupError("Заказ не найден")
    added, skipped = [], []
    for item in o.items:
        p = await s.get(Product, item.product_id)
        if p is None or not p.is_active or p.stock == 0:
            skipped.append(item.title)
            continue
        if await cart_add(s, user_id, item.product_id, item.qty):
            added.append(p.title)
        else:
            skipped.append(item.title)
    return added, skipped


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


async def all_categories(s: AsyncSession) -> list[Category]:
    """Все категории, включая скрытые: панели надо показать и их."""
    res = await s.scalars(select(Category).order_by(Category.sort, Category.id))
    return list(res)


async def all_products(s: AsyncSession) -> list[Product]:
    """Весь каталог для панели, со снятыми с продажи — их тоже правят и возвращают."""
    res = await s.scalars(select(Product).order_by(Product.category_id, Product.id))
    return list(res)


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
