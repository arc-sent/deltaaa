FROM python:3.10-slim

WORKDIR /app

# Сначала зависимости — слой кешируется, пока requirements не менялись.
# --mount=type=cache: скачанные колёса переживают пересборку (не качаем заново).
# --timeout/--retries: если PyPI подвис, pip не висит вечно, а падает и повторяет.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 60 --retries 5 -r requirements.txt

COPY bot.py db.py vk.py reports.py scheduler.py collector.py crypto.py ./

# data/ — том с БД, ключом шифрования и состоянием бота (переживает рестарты).
ENV DATA_DIR=/app/data
# matplotlib пишет кеш шрифтов сюда; в контейнере HOME недоступен на запись.
ENV MPLCONFIGDIR=/tmp/matplotlib
# Дайджест и графики считаем по московскому времени.
ENV TZ=Europe/Moscow

VOLUME ["/app/data"]

CMD ["python", "bot.py"]
