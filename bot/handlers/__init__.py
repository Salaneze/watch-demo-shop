from aiogram import Router

from bot.handlers import admin, ai, cart, catalog, checkout, common


def setup_routers() -> Router:
    router = Router(name="root")
    router.include_router(common.router)
    router.include_router(catalog.router)
    router.include_router(cart.router)
    router.include_router(checkout.router)
    router.include_router(admin.router)
    # Последним намеренно: в состоянии диалога с консультантом любой текст уходит
    # модели, но кнопки меню из роутеров выше должны продолжать работать.
    router.include_router(ai.router)
    return router
