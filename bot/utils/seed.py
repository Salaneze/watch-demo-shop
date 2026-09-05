"""Демо-каталог — заполняется только если БД пустая.

Две витрины, переключаются через SEED в .env:
  SEED=shop  (по умолчанию) — витрина часов с мемными отсылками, спрятанными
             в названия моделей. Незнающий видит обычные вымышленные бренды,
             знающий узнаёт 6-7, aura, sigma, Ohio, demure, touch grass и т.д.
  SEED=plain — та же витрина без единой отсылки, если заказчик душный.

Механика бота одинаковая, отличается только контент.
Бренды и товары вымышленные, совпадения с реальными марками не подразумеваются.
"""
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo

# Рисунки демо-товаров. Генерируются `tools/gen_watch_art.py` и лежат в
# репозитории — на хостинге ничего не рисуется, Pillow там не нужен.
ART_DIR = Path("media/seed")

# --- основная витрина: мемы спрятаны в названиях моделей ---
SHOP = {
    "⌚ Automatic": [
        (
            "Ohio Diver 67",
            "Automatic, NH35 movement. 40 mm steel case, 120-click bezel, sapphire crystal. "
            "Water resistance 670 ft, 41-hour power reserve.",
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
