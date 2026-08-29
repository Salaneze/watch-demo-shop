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
    action: str  # paid | shipped | cancel | view
