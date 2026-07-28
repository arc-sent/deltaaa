"""Общая настройка тестов.

Тесты не ходят в сеть и не трогают реальную БД: DATA_DIR перенаправляется во
временную папку ДО первого импорта db (иначе db.DB_PATH зафиксируется на боевом
пути). Перед каждым тестом база пересоздаётся с нуля.
"""

import os
import tempfile
import pathlib

# Должно выполниться раньше, чем любой тест сделает `import db`.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="delta_tests_")
os.environ["DATA_DIR"] = _TEST_DATA_DIR

import pytest

import db  # noqa: E402  (импорт после установки DATA_DIR — намеренно)


@pytest.fixture(autouse=True)
def fresh_db():
    """Чистая база перед каждым тестом."""
    for f in pathlib.Path(_TEST_DATA_DIR).glob("stats.db*"):
        f.unlink()
    db.init_db()
    yield


@pytest.fixture
def user_group():
    """Создать пользователя и группу. Вернуть их идентификаторы."""
    telegram_id = 1001
    db.ensure_user(telegram_id)
    group_row_id = db.add_group(telegram_id, vk_group_id=555, name="Тестовая группа")
    return {"telegram_id": telegram_id, "group_row_id": group_row_id, "vk_group_id": 555}


class FakeResponse:
    """Заглушка ответа requests: возвращает заранее заданный JSON."""

    def __init__(self, payload, *, raise_exc=None):
        self._payload = payload
        self._raise_exc = raise_exc

    def json(self):
        if self._raise_exc:
            raise self._raise_exc
        return self._payload

    def raise_for_status(self):
        pass
