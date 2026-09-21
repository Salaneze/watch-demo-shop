from aiogram.filters.callback_data import CallbackData


class CategoryCB(CallbackData, prefix="cat"):
    id: int


class ProductCB(CallbackData, prefix="prod"):
    id: int
    category_id: int


class CartCB(CallbackData, prefix="cart"):
    action: str  # add | inc | dec | clear | open | checkout
    product_id: int = 0


class NavCB(CallbackData, prefix="nav"):
    to: str  # menu | catalog


class AdminOrderCB(CallbackData, prefix="aord"):
    order_id: int
    action: str       # целевой статус (значение OrderStatus)
    # Статус, который админ видел на кнопке. Кнопка в старом сообщении несёт
    # устаревшее значение и отбивается условным UPDATE — без отдельной ветки.
    # Самый длинный вариант «aord:999999:ready_for_pickup:awaiting_payment»
    # — 46 байт при лимите Telegram в 64.
    from_status: str


class CustomerOrderCB(CallbackData, prefix="cord"):
    order_id: int
    action: str  # history | cancel
