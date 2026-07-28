"""Тесты слоя БД: группы, снимки подписчиков, настройки дайджеста, логи ошибок."""

import time

import pytest

import db


# ─── Группы ───────────────────────────────────────────────────────────────────

def test_add_group_returns_id_and_is_idempotent(user_group):
    tid = user_group["telegram_id"]
    # повторное добавление той же группы не создаёт дубль, а обновляет имя
    same = db.add_group(tid, 555, "Новое имя")
    assert same == user_group["group_row_id"]
    groups = db.get_groups(tid)
    assert len(groups) == 1
    assert groups[0]["name"] == "Новое имя"


def test_rename_and_delete_group(user_group):
    gid = user_group["group_row_id"]
    db.rename_group(gid, "Переименовано")
    assert db.get_group(gid)["name"] == "Переименовано"
    db.delete_group(gid)
    assert db.get_group(gid) is None


def test_get_all_groups_across_users():
    db.ensure_user(1)
    db.ensure_user(2)
    db.add_group(1, 100, "A")
    db.add_group(2, 200, "B")
    assert len(db.get_all_groups()) == 2


# ─── Снимки ───────────────────────────────────────────────────────────────────

def test_add_snapshot_and_latest(user_group):
    gid = user_group["group_row_id"]
    db.add_snapshot(gid, 1000, ts=100)
    db.add_snapshot(gid, 1050, ts=200)
    latest = db.get_latest_snapshot(gid)
    assert latest["members_count"] == 1050
    assert latest["ts"] == 200


def test_add_snapshot_skips_unchanged_value(user_group):
    gid = user_group["group_row_id"]
    db.add_snapshot(gid, 1000, ts=100)
    db.add_snapshot(gid, 1000, ts=200)  # то же значение — не пишем вторую точку
    assert len(db.get_snapshots(gid)) == 1
    db.add_snapshot(gid, 1001, ts=300)  # изменилось — пишем
    assert len(db.get_snapshots(gid)) == 2


def test_get_snapshots_since_and_order(user_group):
    gid = user_group["group_row_id"]
    for i, val in enumerate([10, 20, 30, 40]):
        db.add_snapshot(gid, val, ts=100 * (i + 1))
    snaps = db.get_snapshots(gid, since_ts=250)
    assert [s["members_count"] for s in snaps] == [30, 40]  # только ts >= 250, по возрастанию


def test_get_snapshot_before(user_group):
    gid = user_group["group_row_id"]
    db.add_snapshot(gid, 10, ts=100)
    db.add_snapshot(gid, 20, ts=300)
    before = db.get_snapshot_before(gid, 250)
    assert before["members_count"] == 10  # ближайший ДО момента 250
    assert db.get_snapshot_before(gid, 50) is None  # раньше всех точек


def test_snapshots_cascade_on_group_delete(user_group):
    gid = user_group["group_row_id"]
    db.add_snapshot(gid, 10, ts=100)
    db.delete_group(gid)
    assert db.get_snapshots(gid) == []


# ─── Дайджест ─────────────────────────────────────────────────────────────────

def test_digest_toggle(user_group):
    tid = user_group["telegram_id"]
    assert db.get_digest_enabled(tid) is False
    db.set_digest_enabled(tid, True)
    assert db.get_digest_enabled(tid) is True
    assert tid in db.get_digest_users()
    db.set_digest_enabled(tid, False)
    assert db.get_digest_enabled(tid) is False
    assert db.get_digest_users() == []


# ─── Логи ошибок ──────────────────────────────────────────────────────────────

def test_log_and_read_errors(user_group):
    tid = user_group["telegram_id"]
    db.log_error(tid, stage="сбор", vk_group_id=555, vk_group_name="г",
                 error_code=15, message="нет доступа")
    assert db.count_errors(tid) == 1
    e = db.get_errors(tid)[0]
    assert e["error_code"] == 15
    assert db.get_error(e["id"])["message"] == "нет доступа"


def test_error_message_token_is_sanitized(user_group):
    tid = user_group["telegram_id"]
    db.log_error(tid, message="fail access_token=vk1.a.SECRET123 in url")
    e = db.get_errors(tid)[0]
    assert "SECRET123" not in e["message"]
    assert "access_token=***" in e["message"]


def test_cleanup_old_errors(user_group):
    tid = user_group["telegram_id"]
    db.log_error(tid, message="свежая")
    # состарим запись вручную
    old_ts = int(time.time()) - 10 * 86400
    with db._connect() as conn:
        conn.execute("UPDATE error_logs SET created_at = ? WHERE telegram_id = ?", (old_ts, tid))
    deleted = db.cleanup_old_errors(days=5)
    assert deleted == 1
    assert db.count_errors(tid) == 0


def test_admin_error_aggregation():
    db.ensure_user(1)
    db.ensure_user(2)
    db.log_error(1, message="a")
    db.log_error(1, message="b")
    db.log_error(2, message="c")
    assert db.count_users_with_errors() == 2
    users = {u["telegram_id"]: u["cnt"] for u in db.get_users_with_errors()}
    assert users[1] == 2 and users[2] == 1
