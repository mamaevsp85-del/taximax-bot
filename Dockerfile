FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_FILE=/data/taxi_data.json

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data

EXPOSE 8080

# Route to Mini-App
# ВАЖНО: `api_server.py` импортирует функции minimax-сервера (maximus_server.py)
# и обслуживает /api/state и /api/act сам — отдельный процесс maximus_server.py
# для работы Mini-App НЕ нужен (запуск обоих допустим, но требует биндиться на
# разные порты: api_server 8080, maximus 8765).
CMD ["sh", "-c", "python main.py & python api_server.py"]
