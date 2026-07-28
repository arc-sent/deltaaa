FROM python:3.10-slim

WORKDIR /app

# Онлайн-установка из PyPI. Слой кешируется, пока requirements не менялись.
# --mount=type=cache — колёса переживают пересборку (не качаем заново).
# --timeout/--retries — если PyPI подвис, pip не висит вечно, а повторяет.
#
# Если сеть Docker до PyPI очень медленная (актуально для Docker Desktop на
# Windows), можно перейти на офлайн-колёса: заранее на хосте выполнить
#   pip download -r requirements.txt -d wheels --only-binary=:all: \
#     --python-version 3.10 --platform manylinux2014_x86_64 \
#     --platform manylinux_2_28_x86_64
# и заменить строки ниже на:  COPY wheels /wheels
#                             RUN pip install --no-index --find-links=/wheels -r requirements.txt
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
