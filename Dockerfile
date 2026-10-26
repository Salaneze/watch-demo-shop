# Образ витрины вместе с ботом (python -m api) для своего VPS.
# Render его не использует — там своя сборка из render.yaml.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости отдельным слоем: правка кода не перекачивает весь pip.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY api ./api
COPY webapp ./webapp
COPY media/seed ./media/seed

# Не root: дыра в зависимости не получает весь контейнер. media/ пишется
# в рантайме — туда кэшируются фото товаров из Telegram.
RUN useradd --create-home --uid 10001 shop && chown -R shop /app/media
USER shop

EXPOSE 8080
CMD ["python", "-m", "api"]
