"""«Запросить живое фото»: покупатель жмёт кнопку в карточке, админ отвечает фото.

Фото не идёт через витрину. Админ отвечает реплаем на сообщение бота, бот
пересылает кадр покупателю в личку. Кому и про какой товар, бот читает из
своего же сообщения (метка ask:<user>:<product>), а не из памяти процесса,
поэтому ответ через час после рестарта на Render всё равно дойдёт.
"""
from __future__ import annotations

import re
import time

from aiogram import Bot

from bot.config import settings
from bot.utils.text import esc

# Одна просьба на человека и товар за это время. Повторное нажатие не должно
# снова звонить владельцу в личку: живое фото — это поход на склад.
COOLDOWN_SECONDS = 6 * 3600

# Только в самом конце сообщения: выше стоят имя покупателя и название товара,
# и покупатель по имени «ask:555:1» иначе перенаправил бы фото на чужой id.
MARKER = re.compile(r"ask:(\d+):(\d+)\s*\Z")

# Память процесса, а не таблица: потеря счётчика при рестарте даст в худшем
# случае одну лишнюю просьбу, ради этого схему БД не трогаем.
# TODO: при нескольких репликах уехать в общий Redis вместе с api/limits.py.
_recent: dict[tuple[int, int], float] = {}


def should_ask(user_id: int, product_id: int, now: float | None = None) -> bool:
    """True — просьба новая, её надо отправить. Заодно ставит отметку."""
    now = time.monotonic() if now is None else now
    if len(_recent) > 5000:
        for key, at in list(_recent.items()):
            if now - at > COOLDOWN_SECONDS:
                del _recent[key]
    last = _recent.get((user_id, product_id))
    if last is not None and now - last < COOLDOWN_SECONDS:
        return False
    _recent[(user_id, product_id)] = now
    return True


def ask_text(user_id: int, username: str | None, full_name: str,
             product_id: int, title: str) -> str:
    who = f'<a href="tg://user?id={user_id}">{esc(full_name or "покупатель")}</a>'
    if username:
        who += f" @{esc(username)}"
    return (
        "📷 <b>Просят живое фото</b>\n"
        f"Товар: {esc(title)} (#{product_id})\n"
        f"Покупатель: {who}\n\n"
        "Ответьте на это сообщение фото или видео — бот перешлёт покупателю.\n"
        f"ask:{user_id}:{product_id}"
    )


def parse_marker(text: str | None) -> tuple[int, int] | None:
    m = MARKER.search(text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def forget(user_id: int, product_id: int) -> None:
    # Никто из админов не получил просьбу: отметку снимаем, пусть повторит.
    _recent.pop((user_id, product_id), None)


async def notify_admins_ask(bot: Bot, text: str) -> int:
    """Сколько админов получили просьбу. Ноль — витрина скажет, что не вышло."""
    delivered = 0
    for admin_id in settings.admins:
        try:
            await bot.send_message(admin_id, text)
            delivered += 1
        except Exception:  # админ не нажал /start у бота
            continue
    return delivered
