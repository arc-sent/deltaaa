FROM python:3.10-slim

WORKDIR /app

# Сначала зависимости — слой кешируется, пока requirements не менялись.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py db.py vk.py reports.py scheduler.py collector.py crypto.py ./

# data/ — том с БД, ключом шифрования и состоянием бота (переживает рестарты).
ENV DATA_DIR=/app/data
# matplotlib пишет кеш шрифтов сюда; в контейнере HOME недоступен на запись.
ENV MPLCONFIGDIR=/tmp/matplotlib
# Дайджест и графики считаем по московскому времени.
ENV TZ=Europe/Moscow

VOLUME ["/app/data"]

CMD ["python", "bot.py"]
