from aiogram.fsm.state import State, StatesGroup


class Checkout(StatesGroup):
    name = State()
    phone = State()
    address = State()
    comment = State()
    confirm = State()
    receipt = State()


class AddProduct(StatesGroup):
    category = State()
    title = State()
    description = State()
    price = State()
    photo = State()
