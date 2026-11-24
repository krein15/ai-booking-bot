FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Сертификаты Минцифры для соединения с GigaChat (только для этого приложения)
COPY scripts/get_certs.sh scripts/get_certs.sh
RUN sh scripts/get_certs.sh /app/certs/russian_trusted_ca.pem

COPY app app
COPY config config
COPY static static
COPY scripts scripts

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app/data
USER bot

ENV GIGACHAT_CA_BUNDLE_FILE=/app/certs/russian_trusted_ca.pem \
    DATABASE_PATH=/app/data/bot.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
