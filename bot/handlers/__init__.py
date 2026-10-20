from aiogram import Router

from bot.handlers import admin, ai, cart, catalog, checkout, common, reviews


def setup_routers() -> Router:
    router = Router(name="root")
    router.include_router(common.router)
    router.include_router(catalog.router)
    router.include_router(cart.router)
    router.include_router(checkout.router)
    router.include_router(admin.router)
    # После меню и админки: пока человек в шаге «напишите отзыв», кнопки
    # «Корзина» и «Каталог» должны работать, а не уходить текстом в отзыв.
    router.include_router(reviews.router)
    # Последним намеренно: в состоянии диалога с консультантом любой текст уходит
    # модели, но кнопки меню из роутеров выше должны продолжать работать.
    router.include_router(ai.router)
    return router
