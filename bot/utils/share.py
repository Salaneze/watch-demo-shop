"""«Поделиться товаром»: ссылка на карточку и сообщение, которое уходит в чат.

Сообщение сохраняет бот (`savePreparedInlineMessage`), а отправляет покупатель
из витрины (`shareMessage`) — в любой свой чат, группу или канал.
"""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InlineQueryResultPhoto,
    InputTextMessageContent,
)

from bot.config import settings
from bot.db.models import Product
from bot.utils.money import fmt
from bot.utils.seed import seed_art_path
from bot.utils.text import esc

START_PREFIX = "p"


def product_link(bot_username: str, product_id: int) -> str:
    """Прямая ссылка на Main Mini App: витрина откроется сразу на товаре.

    Без Main Mini App в BotFather клиент откроет просто чат с ботом — карточку
    человек тогда не увидит, но и в тупик не упрётся.
    """
    return f"https://t.me/{bot_username}?startapp={START_PREFIX}{product_id}"


def share_result(product: Product, link: str, photo_url: str | None, *, plain: bool = False):
    """Карточка товара для чужого чата.

    Кнопка — обычная url, а не web_app: web_app работает только в личке с ботом,
    а сообщение уйдёт куда угодно.

    Фото из Telegram берём по file_id — оно там уже лежит. Демо-рисунок `seed:`
    в Telegram не загружен, его отдаём ссылкой на наш же /api/img; это PNG, а
    для inline-фото Telegram документирует JPEG, поэтому вызывающий при отказе
    повторяет с `plain=True` — текстом без фото, но с той же кнопкой.
    """
    caption = f"<b>{esc(product.title)}</b>\n{fmt(product.price)}\n\n{esc(settings.shop_name)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть в магазине", url=link),
    ]])
    rid = f"{START_PREFIX}{product.id}"

    if not plain and product.photo_file_id:
        if seed_art_path(product.photo_file_id) is None:
            return InlineQueryResultCachedPhoto(
                id=rid, photo_file_id=product.photo_file_id,
                caption=caption, parse_mode="HTML", reply_markup=kb,
            )
        if photo_url:
            return InlineQueryResultPhoto(
                id=rid, photo_url=photo_url, thumbnail_url=photo_url,
                caption=caption, parse_mode="HTML", reply_markup=kb,
            )
    return InlineQueryResultArticle(
        id=rid, title=product.title, description=fmt(product.price),
        input_message_content=InputTextMessageContent(message_text=caption, parse_mode="HTML"),
        reply_markup=kb,
    )
