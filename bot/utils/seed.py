"""Демо-каталог — заполняется только если БД пустая.

Две витрины, переключаются через SEED в .env:
  SEED=shop  (по умолчанию) — витрина часов с мемными отсылками, спрятанными
             в названия моделей. Незнающий видит обычные вымышленные бренды,
             знающий узнаёт 6-7, aura, sigma, Ohio, demure, touch grass и т.д.
  SEED=plain — та же витрина без единой отсылки, если заказчик душный.

Механика бота одинаковая, отличается только контент.
Бренды и товары вымышленные, совпадения с реальными марками не подразумеваются.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo

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
            await repo.add_product(session, cat.id, title, description, price, None)
    return True
