"""Экранирование пользовательского ввода.

Бот работает с parse_mode=HTML, поэтому любой текст, пришедший от человека
(имя, адрес, комментарий, название товара от админа, имя профиля в Telegram),
обязан пройти через esc() перед вставкой в сообщение. Иначе одна угловая
скобка в адресе — и Telegram отклоняет отправку с "can't parse entities",
а заказ повисает неотправленным админу.
"""
from aiogram.utils.text_decorations import html_decoration


def esc(value: object) -> str:
    """Экранирует < > & — всё, что Telegram примет за разметку."""
    return html_decoration.quote(str(value))
