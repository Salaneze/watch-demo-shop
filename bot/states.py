from aiogram.fsm.state import State, StatesGroup


class Checkout(StatesGroup):
    name = State()
    phone = State()
    address = State()
    # Доставка и промокод спрашиваются ДО комментария: оба меняют сумму, а превью
    # с итогом показывается последним шагом и запоминается для сверки. Спросить
    # их после превью — гарантированный CartChanged на ровном месте.
    delivery = State()
    promo = State()
    comment = State()
    confirm = State()
    receipt = State()


class AddProduct(StatesGroup):
    category = State()
    title = State()
    description = State()
    price = State()
    photo = State()


class AiChat(StatesGroup):
    # Пока клиент в этом состоянии, любой текст уходит модели, а не в поиск
    # по каталогу. Кнопки меню продолжают работать: их фильтры стоят раньше.
    talking = State()
