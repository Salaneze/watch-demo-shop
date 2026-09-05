import hashlib
import hmac
import os

from pydantic import AliasChoices, Field
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

    # Стоимость доставки — в той же валюте, что и товары, поэтому меняется
    # вместе с ней: 490 это вменяемая цена курьера в рублях и абсурдная
    # в долларах, а витрина про такое молчать не станет.
    delivery_courier: int = 9
    delivery_post: int = 15

    # --- Mini App ---
    # Публичный HTTPS-адрес витрины: туннель при разработке, домен в проде.
    # Пусто — бот работает как раньше, кнопка витрины не показывается: Telegram
    # принимает в web_app только https и локальный адрес отвергает.
    # На хостинге можно не задавать: адрес возьмётся из RENDER_EXTERNAL_URL,
    # который платформа подставляет сама — так домен не приходится дублировать
    # руками и он не расходится с реальным.
    webapp_url: str = ""
    web_host: str = "127.0.0.1"
    # PORT — то, чем хостинги сообщают процессу, какой порт слушать. Читаем оба
    # имени: локально WEB_PORT из .env, на платформе PORT из окружения.
    web_port: int = Field(default=8080, validation_alias=AliasChoices("WEB_PORT", "PORT"))

    # Webhook вместо long-polling. На бесплатных тарифах контейнер засыпает без
    # входящих HTTP-запросов, и спящий polling просто перестаёт забирать апдейты;
    # входящий webhook от Telegram будит процесс сам.
    use_webhook: bool = False

    # Приложение за обратным прокси: только тогда можно верить заголовкам
    # X-Forwarded-For / CF-Connecting-IP. Без прокси заголовок подставит кто
    # угодно и обойдёт ограничитель частоты, меняя значение на каждый запрос.
    trust_proxy: bool = False

    @property
    def public_url(self) -> str:
        """Внешний адрес витрины без хвостового слеша."""
        return (self.webapp_url or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")

    @property
    def has_webapp(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def webhook_path(self) -> str:
        """Секретный путь вебхука, выведенный из токена бота.

        Отдельной переменной окружения не заводим: значение обязано быть
        стабильным между перезапусками (иначе Telegram шлёт апдейты на мёртвый
        адрес) и неугадываемым снаружи. Вывод из токена даёт и то, и другое.
        """
        digest = hmac.new(
            self.bot_token.encode(), b"webhook-path", hashlib.sha256
        ).hexdigest()
        return f"/tg/{digest[:32]}"

    @property
    def webhook_secret(self) -> str:
        """Значение заголовка X-Telegram-Bot-Api-Secret-Token.

        Вторая проверка вдобавок к секретному пути: путь может утечь в логи
        прокси, заголовок туда не попадает.
        """
        return hmac.new(
            self.bot_token.encode(), b"webhook-secret", hashlib.sha256
        ).hexdigest()

    @property
    def admins(self) -> set[int]:
        return {int(x) for x in self.admin_ids.replace(" ", "").split(",") if x}


settings = Settings()
