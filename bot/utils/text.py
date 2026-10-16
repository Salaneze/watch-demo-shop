"""Экранирование пользовательского ввода.

Бот работает с parse_mode=HTML, поэтому любой текст, пришедший от человека
(имя, адрес, комментарий, название товара от админа, имя профиля в Telegram),
обязан пройти через esc() перед вставкой в сообщение. Иначе одна угловая
скобка в адресе — и Telegram отклоняет отправку с "can't parse entities",
а заказ повисает неотправленным админу.
"""
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from aiogram.utils.text_decorations import html_decoration

from bot.config import settings


def esc(value: object) -> str:
    """Экранирует < > & — всё, что Telegram примет за разметку."""
    return html_decoration.quote(str(value))


@lru_cache(maxsize=1)
def _shop_zone() -> ZoneInfo:
    return ZoneInfo(settings.shop_tz)


def local_time(dt: datetime, fmt: str = "%d.%m %H:%M") -> str:
    """Время для сообщения в боте — в поясе магазина, а не в UTC.

    SQLite возвращает время наивным, но пишем мы всегда UTC (models.utcnow),
    поэтому наивное считаем UTC. Без этого админ из Москвы видел в истории
    заказа время на три часа раньше настоящего.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_shop_zone()).strftime(fmt)
