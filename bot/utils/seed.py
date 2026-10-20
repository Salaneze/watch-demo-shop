"""Демо-каталог — заполняется только если БД пустая.

Две витрины, переключаются через SEED в .env:
  SEED=shop  (по умолчанию) — витрина часов с мемными отсылками, спрятанными
             в названия моделей. Незнающий видит обычные вымышленные бренды,
             знающий узнаёт 6-7, aura, sigma, Ohio, demure, touch grass и т.д.
  SEED=plain — та же витрина без единой отсылки, если заказчик душный.

Механика бота одинаковая, отличается только контент.
Бренды и товары вымышленные, совпадения с реальными марками не подразумеваются.
"""
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import Product, Review, utcnow

# Рисунки демо-товаров лежат готовыми в репозитории — на хостинге ничего
# не рисуется, Pillow там не нужен.
ART_DIR = Path("media/seed")

# --- основная витрина: мемы спрятаны в названиях моделей ---
SHOP = {
    "⌚ Automatic": [
        (
            "Ohio Diver 67",
            "Automatic, NH35 movement. 40 mm steel case, 120-click bezel, sapphire crystal. "
            "Water resistance 300 m, 41-hour power reserve.",
            670,
        ),
        (
            "Nordwind Sigma 40",
            "In-house automatic, 40 mm. Brushed titanium case, matte anthracite dial. "
            "Built for people who wake up at 5 a.m. and do not talk about it.",
            940,
        ),
        (
            "Bombardiro Skeleton 41",
            "Hand-wound skeleton, open balance wheel. Sapphire on both sides, 41 mm. "
            "Assembled in a small workshop that answers no emails.",
            1290,
        ),
    ],
    "🌿 Field": [
        (
            "Ohio Field 38 Grass",
            "Automatic field watch, 38 mm. Matte dial, full lume, 100 m water resistance. "
            "Designed to be worn outside. Actually outside.",
            340,
        ),
        (
            "Nordwind Aura 36",
            "Compact 36 mm automatic with a sunburst dial that collects light from every angle. "
            "People will notice. That is the point.",
            410,
        ),
    ],
    "⚡ Quartz": [
        (
            "Kron Mid 36",
            "Quartz, Japanese movement. 36 mm, 7 mm thin, slides under a cuff. "
            "Honestly named: it is the middle of the range and does not pretend otherwise.",
            120,
        ),
        (
            "Kron Nonchalant 39",
            "Quartz three-hander, no date, no branding on the dial. "
            "For the person who never once mentions what they are wearing.",
            180,
        ),
        (
            "Nordwind Demure 34",
            "Quartz, 34 mm, polished case, mother-of-pearl dial. Understated and considerate. "
            "Very mindful of your wrist.",
            210,
        ),
        (
            "Ohio Chrono 42",
            "Quartz chronograph, three sub-dials, tachymeter bezel. 42 mm steel.",
            260,
        ),
    ],
    "🔗 Straps": [
        ("Leather strap, 20 mm", "Italian vegetable-tanned leather. Quick-release spring bars.", 45),
        ("Milanese bracelet, 20 mm", "Stainless mesh, sliding clasp, infinite adjustment.", 60),
        ("NATO nylon, 20 mm", "Seatbelt weave, brushed hardware. Six colourways.", 25),
    ],
    "🎁 Sets": [
        (
            "Lock In Set",
            "Ohio Field 38 Grass + leather strap + two-slot travel case. "
            "Everything you need to stop thinking about watches and get back to work.",
            420,
        ),
    ],
}

# --- нейтральная витрина: тот же ассортимент, вычищенные названия ---
PLAIN = {
    "⌚ Automatic": [
        (
            "Nordwind Diver 300",
            "Automatic, NH35 movement. 40 mm steel case, 120-click bezel, sapphire crystal. "
            "300 m water resistance, 41-hour power reserve.",
            670,
        ),
        (
            "Nordwind Meridian 40",
            "In-house automatic, 40 mm. Brushed titanium case, matte anthracite dial.",
            940,
        ),
        (
            "Kron Skeleton 41",
            "Hand-wound skeleton, open balance wheel. Sapphire on both sides, 41 mm.",
            1290,
        ),
    ],
    "🌿 Field": [
        (
            "Nordwind Field 38",
            "Automatic field watch, 38 mm. Matte dial, full lume, 100 m water resistance.",
            340,
        ),
        (
            "Nordwind Solaris 36",
            "Compact 36 mm automatic with a sunburst dial.",
            410,
        ),
    ],
    "⚡ Quartz": [
        (
            "Kron Daily 36",
            "Quartz, Japanese movement. 36 mm, 7 mm thin, slides under a cuff.",
            120,
        ),
        (
            "Kron Essential 39",
            "Quartz three-hander, no date, minimal dial.",
            180,
        ),
        (
            "Nordwind Aria 34",
            "Quartz, 34 mm, polished case, mother-of-pearl dial.",
            210,
        ),
        (
            "Kron Chrono 42",
            "Quartz chronograph, three sub-dials, tachymeter bezel. 42 mm steel.",
            260,
        ),
    ],
    "🔗 Straps": [
        ("Leather strap, 20 mm", "Italian vegetable-tanned leather. Quick-release spring bars.", 45),
        ("Milanese bracelet, 20 mm", "Stainless mesh, sliding clasp, infinite adjustment.", 60),
        ("NATO nylon, 20 mm", "Seatbelt weave, brushed hardware. Six colourways.", 25),
    ],
    "🎁 Sets": [
        (
            "Starter Set",
            "Nordwind Field 38 + leather strap + two-slot travel case.",
            420,
        ),
    ],
}

CATALOGS = {"shop": SHOP, "plain": PLAIN}

# Название товара → картинка из `media/seed/`. Отдельной картой, а не полем в
# кортеже: демо-каталоги `shop` и `plain` зовут одни и те же модели по-разному,
# а рисунок у них общий. Клиент, который заменит каталог своим, просто перестанет
# попадать в эту карту — товары останутся без фото, ничего не сломается.
#
# В БД слаг ложится в `photo_file_id` с префиксом `seed:`: так демо-картинка и
# настоящий файл из Telegram различимы одним полем, без миграции схемы.
SEED_ART_PREFIX = "seed:"

ART = {
    # shop
    "Ohio Diver 67": "diver",
    "Nordwind Sigma 40": "meridian",
    "Bombardiro Skeleton 41": "skeleton",
    "Ohio Field 38 Grass": "field",
    "Nordwind Aura 36": "sunburst",
    "Kron Mid 36": "thin",
    "Kron Nonchalant 39": "minimal",
    "Nordwind Demure 34": "pearl",
    "Ohio Chrono 42": "chrono",
    "Lock In Set": "set",
    # plain
    "Nordwind Diver 300": "diver",
    "Nordwind Meridian 40": "meridian",
    "Kron Skeleton 41": "skeleton",
    "Nordwind Field 38": "field",
    "Nordwind Solaris 36": "sunburst",
    "Kron Daily 36": "thin",
    "Kron Essential 39": "minimal",
    "Nordwind Aria 34": "pearl",
    "Kron Chrono 42": "chrono",
    "Starter Set": "set",
    # общие для обоих каталогов
    "Leather strap, 20 mm": "strap-leather",
    "Milanese bracelet, 20 mm": "strap-milanese",
    "NATO nylon, 20 mm": "strap-nato",
}


# Характеристики и остатки заданы по слагу картинки, а не по названию товара:
# `shop` и `plain` зовут одну модель по-разному, но корпус у неё один. Так
# таблица не дублируется и не разъезжается между двумя каталогами.
SPECS = {
    "diver": [["Movement", "automatic, NH35"], ["Case", "steel, 40 mm"],
              ["Water resistance", "300 m"], ["Crystal", "sapphire"]],
    "meridian": [["Movement", "in-house automatic"], ["Case", "titanium, 40 mm"],
                 ["Dial", "matte anthracite"], ["Power reserve", "48 h"]],
    "skeleton": [["Movement", "hand-wound skeleton"], ["Case", "steel, 41 mm"],
                 ["Crystal", "sapphire, front and back"], ["Power reserve", "42 h"]],
    "field": [["Movement", "automatic"], ["Case", "steel, 38 mm"],
              ["Water resistance", "100 m"], ["Lume", "full dial"]],
    "sunburst": [["Movement", "automatic"], ["Case", "steel, 36 mm"],
                 ["Dial", "sunburst"], ["Strap", "leather, 18 mm"]],
    "thin": [["Movement", "quartz, Japanese"], ["Case", "steel, 36 mm"],
             ["Thickness", "7 mm"], ["Strap", "leather, 18 mm"]],
    "minimal": [["Movement", "quartz, three-hander"], ["Case", "steel, 39 mm"],
                ["Dial", "no date, no branding"], ["Strap", "leather, 20 mm"]],
    "pearl": [["Movement", "quartz"], ["Case", "polished steel, 34 mm"],
              ["Dial", "mother-of-pearl"], ["Strap", "leather, 16 mm"]],
    "chrono": [["Movement", "quartz chronograph"], ["Case", "steel, 42 mm"],
               ["Sub-dials", "three"], ["Bezel", "tachymeter"]],
    "set": [["Includes", "watch, strap, case"], ["Case", "two slots"],
            ["Packaging", "gift box"]],
    "strap-leather": [["Material", "vegetable-tanned leather"], ["Width", "20 mm"],
                      ["Spring bars", "quick-release"]],
    "strap-milanese": [["Material", "stainless mesh"], ["Width", "20 mm"],
                       ["Clasp", "sliding, infinite adjustment"]],
    "strap-nato": [["Material", "seatbelt nylon"], ["Width", "20 mm"],
                   ["Hardware", "brushed steel"]],
}

# Остаток на складе. -1 — продаём не считая штук; у дорогих моделей числа
# маленькие намеренно: на демо это единственный способ увидеть, что «осталось N»
# и отказ при нехватке вообще работают.
STOCK = {
    "skeleton": 2,
    "chrono": 3,
    "meridian": 5,
    "set": 4,
}

# Промокоды демо-витрины. WELCOME10 без ограничений — его показывают в описании
# магазина; NORDWIND20 с лимитом, чтобы на демо было видно, как код кончается.
PROMOS = (
    ("WELCOME10", 10, -1),
    ("NORDWIND20", 20, 10),
)


def specs_for(title: str) -> list:
    slug = ART.get(title)
    return SPECS.get(slug, []) if slug else []


def stock_for(title: str) -> int:
    slug = ART.get(title)
    return STOCK.get(slug, -1) if slug else -1


def art_for(title: str) -> str | None:
    """Значение `photo_file_id` для демо-товара или None, если картинки нет."""
    slug = ART.get(title)
    return f"{SEED_ART_PREFIX}{slug}" if slug else None


def seed_art_path(photo_file_id: str | None) -> Path | None:
    """Путь к демо-картинке по значению `photo_file_id` или None.

    Слаг приходит из БД, поэтому сверяется со списком известных, а не
    подставляется в путь как есть: иначе `seed:../../.env` вычитал бы из
    каталога любой файл на диске.
    """
    if not photo_file_id or not photo_file_id.startswith(SEED_ART_PREFIX):
        return None
    slug = photo_file_id[len(SEED_ART_PREFIX):]
    if slug not in set(ART.values()):
        return None
    path = ART_DIR / f"{slug}.png"
    return path if path.exists() else None


def active_catalog() -> dict:
    """Каталог по настройке SEED. Неизвестное значение — ошибка, а не молчаливый
    откат на дефолт: иначе опечатка в .env тихо подсунет клиенту не ту витрину."""
    name = settings.seed.strip().lower()
    if name not in CATALOGS:
        raise ValueError(
            f"SEED={settings.seed!r} — неизвестный каталог. Доступны: {', '.join(CATALOGS)}"
        )
    return CATALOGS[name]


async def seed_if_empty(session: AsyncSession) -> bool:
    if not await repo.catalog_is_empty(session):
        return False

    for cat_title, products in active_catalog().items():
        cat = await repo.add_category(session, cat_title)
        for title, description, price in products:
            await repo.add_product(
                session, cat.id, title, description, price, art_for(title),
                stock=stock_for(title), specs=specs_for(title),
            )

    for code, percent, max_uses in PROMOS:
        await repo.add_promo(session, code, percent, max_uses)
    return True


# Демо-отзывы витрины часов: (товар, автор, оценка, текст, дней назад).
# Не одни пятёрки намеренно: ровные 5.0 выглядят накруткой, а тройка с
# конкретной претензией вызывает больше доверия, чем десяток «всё супер».
# Пустой текст — оценка без отзыва: в среднюю идёт, в ленте не показывается.
DEMO_REVIEWS = (
    ("Ohio Diver 67", "Артём В.", 5, "Ношу третью неделю, отстают секунд на пять в сутки — для NH35 норма. Безель щёлкает плотно, без люфта.", 4),
    ("Ohio Diver 67", "Илья С.", 4, "Часы отличные, но браслет на тонкую руку пришлось укорачивать в мастерской — звеньев снимается мало.", 11),
    ("Ohio Diver 67", "Марина К.", 5, "Брала мужу на годовщину. Упаковка аккуратная, пришли за три дня.", 19),
    ("Ohio Diver 67", "Денис", 5, "", 26),
    ("Nordwind Sigma 40", "Павел Р.", 5, "Титан реально лёгкий, на руке почти не чувствуется. Циферблат в жизни глубже, чем на фото.", 7),
    ("Nordwind Sigma 40", "Олег", 4, "Хорошие, но заводная головка мелковата, перчатками не подвести.", 23),
    ("Bombardiro Skeleton 41", "Никита Ж.", 5, "Залипаю на механизм вместо работы. Подзавожу утром, хватает до вечера следующего дня.", 5),
    ("Bombardiro Skeleton 41", "Сергей Т.", 3, "Красиво, но читать время сложно — стрелки теряются на фоне механизма. На каждый день не взял бы.", 15),
    ("Ohio Field 38 Grass", "Аня Л.", 5, "Идеальный размер на женскую руку, зелёный циферблат на солнце прямо светится.", 3),
    ("Ohio Field 38 Grass", "Константин", 5, "", 12),
    ("Ohio Field 38 Grass", "Рустам Х.", 4, "Ремешок из комплекта жестковат первые дни, потом разносился.", 30),
    ("Nordwind Aura 36", "Вика П.", 5, "Санрэй переливается от синего к почти чёрному. Подруги спрашивают, что за марка.", 9),
    ("Kron Mid 36", "Дмитрий А.", 4, "Тонкие, под манжету влезают. За эти деньги вопросов нет, но стекло минеральное — уже поймал царапину.", 6),
    ("Kron Mid 36", "Света Н.", 5, "", 17),
    ("Kron Nonchalant 39", "Глеб", 5, "Минимализм как я люблю: ни даты, ни лишних надписей.", 13),
    ("Nordwind Demure 34", "Ольга М.", 5, "Перламутр живой, на каждом свету разный. Застёжка удобная, сама надеваю одной рукой.", 8),
    ("Ohio Chrono 42", "Максим Е.", 4, "Хронограф работает чётко, но толщина ощутимая, под рубашку не всегда.", 10),
    ("Ohio Chrono 42", "Тимур", 5, "Взял для картинга, засекаю круги. Кнопки нажимаются уверенно.", 21),
    ("Leather strap, 20 mm", "Егор Б.", 5, "Быстросъёмные шпильки — поменял ремешок за минуту без инструментов.", 2),
    ("Leather strap, 20 mm", "Лиза", 4, "Кожа приятная, но цвет чуть темнее, чем на фото.", 14),
    ("Milanese bracelet, 20 mm", "Роман", 5, "Регулировка без звеньев — то, что надо. Волосы на руке не дерёт.", 18),
    ("NATO nylon, 20 mm", "Кирилл Д.", 5, "За такие деньги взял сразу три цвета, меняю под настроение.", 5),
    ("Lock In Set", "Алексей Ф.", 5, "Брал в подарок брату — открыл коробку и сказал, что это лучший подарок за годы. Футляр добротный.", 16),
)


async def seed_demo_reviews(session: AsyncSession) -> int:
    """Залить демо-отзывы, если их ещё нет. → сколько добавлено.

    Отдельно от seed_if_empty: каталог в проде засеян давно, и отзывы туда
    надо долить, не трогая товары. Повторный запуск ничего не делает — признак
    «уже засеяно» один: есть хоть один демо-отзыв.
    """
    if not settings.demo_reviews or settings.seed.strip().lower() != "shop":
        return 0
    if await session.scalar(select(Review.id).where(Review.is_demo.is_(True)).limit(1)):
        return 0
    by_title = {p.title: p.id for p in await session.scalars(select(Product))}
    now = utcnow()
    added = 0
    for n, (title, author, rating, text, days) in enumerate(DEMO_REVIEWS, start=1):
        pid = by_title.get(title)
        if pid is None:  # каталог правили руками — отзыв к чужому товару не клеим
            continue
        session.add(Review(
            # Отрицательный id: настоящий Telegram id всегда положительный, и
            # уникальность «человек + товар» не столкнёт демо с живым покупателем.
            user_id=-n, product_id=pid, order_id=None, author=author, rating=rating,
            text=text, is_demo=True, created_at=now - timedelta(days=days),
        ))
        added += 1
    await session.commit()
    return added
