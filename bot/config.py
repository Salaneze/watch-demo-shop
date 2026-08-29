from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_ids: str = ""
    payment_details: str = "Реквизиты не заданы"
    db_url: str = "sqlite+aiosqlite:///shop.db"
    # Какую витрину заливать в пустую БД: shop | plain
    seed: str = "shop"
    # Валюта витрины: USD | EUR | GBP | RUB | KZT
    currency: str = "USD"

    # --- Mini App ---
    # Публичный HTTPS-адрес витрины: туннель при разработке, домен в проде.
    # Пусто — бот работает как раньше, кнопка витрины не показывается: Telegram
    # принимает в web_app только https и локальный адрес отвергает.
    webapp_url: str = ""
    web_host: str = "127.0.0.1"
    web_port: int = 8080

    @property
    def has_webapp(self) -> bool:
        return self.webapp_url.startswith("https://")

    @property
    def admins(self) -> set[int]:
        return {int(x) for x in self.admin_ids.replace(" ", "").split(",") if x}


settings = Settings()
