"""Всё, что меняется вместе с нишей магазина, кроме самих товаров.

Подсказки к терминам и блок «О магазине». Каталог живёт в seed.py, а здесь то,
без чего карточка часов понятна знатоку и непонятна обычному покупателю.
Под клиента с кроссовками или чаем меняется этот файл, а код витрины не трогаем.
"""
from __future__ import annotations

import re

from bot.config import settings

# Порядок важен: побеждает первое совпадение в строке характеристики, поэтому
# узкие термины стоят раньше общих. «automatic, NH35» должен объяснить NH35, а
# не «automatic», который покупатель и без нас поймёт.
WATCH_GLOSSARY: tuple[tuple[str, str], ...] = (
    ("NH35", "A proven Seiko-made automatic movement. Any watchmaker can service it."),
    ("in-house", "The movement is designed and built by the brand itself, not bought in."),
    ("hand-wound", "No rotor: you turn the crown every day or two, and that is part of the ritual."),
    ("chronograph", "A built-in stopwatch, started and stopped with the pushers on the case."),
    ("automatic", "Winds itself from wrist movement, no battery. Left in a drawer, it stops "
                  "when the power reserve runs out."),
    ("quartz", "Battery-powered and accurate to a few seconds a month. Battery change every 2-3 years."),
    ("sapphire", "A crystal that practically does not scratch: only diamond cuts it."),
    ("power reserve", "How long the watch keeps running off the wrist once fully wound."),
    ("water resistance", "100 m is fine for swimming, 300 m for diving. 30 m means splashes only, "
                         "it is not a depth."),
    ("lume", "Luminous paint on the hands and markers. Glows in the dark after being in the light."),
    ("tachymeter", "A scale on the bezel for measuring speed over a known distance."),
    ("sunburst", "Dial finish with rays from the centre: it changes shade as you turn the wrist."),
    ("mother-of-pearl", "A natural shell layer, so no two dials have the same pattern."),
    ("quick-release", "Spring bars with a small lever: swap straps without tools."),
    ("vegetable-tanned", "Leather tanned with plant extracts. Darkens and softens as you wear it."),
)

# Блок на языке интерфейса, а не каталога: это слова магазина, как кнопки.
WATCH_ABOUT: tuple[str, ...] = (
    "Витрина-образец: бренды вымышленные, деньги не списываются",
    "Доставка по стране 2–5 дней, самовывоз по договорённости",
    "Гарантия 24 месяца, возврат 14 дней, если часы не носили",
    "Сомневаетесь — попросите живое фото в карточке товара",
)

GLOSSARIES = {"shop": WATCH_GLOSSARY, "plain": WATCH_GLOSSARY}
ABOUTS = {"shop": WATCH_ABOUT, "plain": WATCH_ABOUT}

# Дефис считаем частью слова: иначе «hand-wound» нашёлся бы внутри
# «hand-wound-like», а «quartz» внутри «quartz-free».
_EDGE = r"[\w-]"


def _glossary() -> tuple[tuple[str, str], ...]:
    return GLOSSARIES.get(settings.seed.strip().lower(), ())


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!{_EDGE}){re.escape(term)}(?!{_EDGE})", re.IGNORECASE)


def spec_hints(specs: list[list[str]]) -> list[str | None]:
    """Пояснение к каждой строке характеристик или None. Длина та же, что у specs.

    Одна подсказка на строку: если объяснять всё подряд, карточка превращается
    в справочник, и покупатель перестаёт открывать подсказки вообще.
    """
    glossary = [(_pattern(term), text) for term, text in _glossary()]
    out: list[str | None] = []
    for row in specs:
        line = " ".join(str(x) for x in row)
        out.append(next((text for pat, text in glossary if pat.search(line)), None))
    return out


def about_lines() -> list[str]:
    """Строки блока «О магазине». Пустой список — витрина блок не рисует.

    SHOP_ABOUT в .env важнее демо-текста; строки в нём разделяются «|», потому
    что перевод строки в переменной окружения переживает не каждый хостинг.
    """
    own = settings.shop_about.strip()
    if own:
        return [s.strip() for s in own.split("|") if s.strip()]
    if not settings.demo_reviews:
        # Флаг демо-витрины: в клиентском магазине выдуманное «24 месяца
        # гарантии» хуже, чем отсутствие блока.
        return []
    return list(ABOUTS.get(settings.seed.strip().lower(), ()))
