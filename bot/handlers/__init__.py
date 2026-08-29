from aiogram import Router

from bot.handlers import admin, cart, catalog, checkout, common


def setup_routers() -> Router:
    router = Router(name="root")
    router.include_router(common.router)
    router.include_router(catalog.router)
    router.include_router(cart.router)
    router.include_router(checkout.router)
    router.include_router(admin.router)
    return router
