FROM python:3.10-slim

WORKDIR /app

# Офлайн-установка: колёса заранее скачаны на хосте в wheels/ (Linux, py3.10).
# --no-index — не ходим в сеть вообще, ставим только из /wheels. Быстро и без
# зависимости от медленного канала Docker-VM до PyPI.
# Пересобрать wheels/ при смене requirements:
#   pip download -r requirements.txt -d wheels --only-binary=:all: \
#     --python-version 3.10 --platform manylinux2014_x86_64 \
#     --platform manylinux_2_28_x86_64
COPY requirements.txt .
COPY wheels /wheels
RUN pip install --no-index --find-links=/wheels -r requirements.txt

COPY bot.py db.py vk.py reports.py scheduler.py collector.py crypto.py ./

# data/ — том с БД, ключом шифрования и состоянием бота (переживает рестарты).
ENV DATA_DIR=/app/data
# matplotlib пишет кеш шрифтов сюда; в контейнере HOME недоступен на запись.
ENV MPLCONFIGDIR=/tmp/matplotlib
# Дайджест и графики считаем по московскому времени.
ENV TZ=Europe/Moscow

VOLUME ["/app/data"]

CMD ["python", "bot.py"]
