"""Форматирование цен. Валюта задаётся в .env, а не хардкодится по коду —
под клиента меняется одной строкой.
"""
from bot.config import settings

# код -> (символ, символ перед суммой?)
CURRENCIES = {
    "USD": ("$", True),
    "EUR": ("€", True),
    "GBP": ("£", True),
    "RUB": ("₽", False),
    "KZT": ("₸", False),
}

# Разделитель тысяч для валют, где символ идёт после суммы. Взят неразрывный
# пробел: число не должно переноситься отдельно от знака валюты.
# Записан escape-последовательностью, а не самим символом — иначе он невидим
# в коде и неотличим от обычного пробела при чтении диффа.
NBSP = "\u00a0"


def _cur() -> tuple[str, bool]:
    code = settings.currency.strip().upper()
    if code not in CURRENCIES:
        raise ValueError(
            f"CURRENCY={settings.currency!r} не поддерживается. Доступны: {', '.join(CURRENCIES)}"
        )
    return CURRENCIES[code]


def fmt(amount: int) -> str:
    """2490 -> '$2,490' (USD) либо '2 490 ₽' (RUB, пробелы неразрывные)."""
    sym, prefix = _cur()
    if prefix:
        return f"{sym}{amount:,}"
    return f"{amount:,}".replace(",", NBSP) + NBSP + sym


def symbol() -> str:
    return _cur()[0]
