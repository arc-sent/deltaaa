"""Тесты сборщика снимков: успех пишет точку, ошибка логируется, без токена — пропуск."""

import collector
import db
import vk


def test_collect_group_writes_snapshot(user_group, monkeypatch):
    tid = user_group["telegram_id"]
    db.set_vk_token(tid, "vk1.a.TESTTOKEN")
    monkeypatch.setattr(collector.vk, "fetch_members_count", lambda token, gid: 4242)

    group = db.get_all_groups()[0]
    assert collector.collect_group(group) is True
    latest = db.get_latest_snapshot(user_group["group_row_id"])
    assert latest["members_count"] == 4242


def test_collect_group_skips_without_token(user_group, monkeypatch):
    called = {"n": 0}

    def spy(token, gid):
        called["n"] += 1
        return 1

    monkeypatch.setattr(collector.vk, "fetch_members_count", spy)
    group = db.get_all_groups()[0]
    assert collector.collect_group(group) is False  # токена нет — тихий пропуск
    assert called["n"] == 0
    assert db.get_snapshots(user_group["group_row_id"]) == []


def test_collect_group_logs_vk_error(user_group, monkeypatch):
    tid = user_group["telegram_id"]
    db.set_vk_token(tid, "vk1.a.TESTTOKEN")

    def raise_vk(token, gid):
        raise vk.VKError(15, "Access denied", stage="VK groups.getById")

    monkeypatch.setattr(collector.vk, "fetch_members_count", raise_vk)
    group = db.get_all_groups()[0]
    assert collector.collect_group(group) is False
    assert db.count_errors(tid) == 1
    e = db.get_errors(tid)[0]
    assert e["error_code"] == 15
    assert e["vk_group_id"] == user_group["vk_group_id"]


def test_collect_all_counts(monkeypatch):
    db.ensure_user(1)
    db.ensure_user(2)
    db.set_vk_token(1, "vk1.a.A")
    # у пользователя 2 токена нет — его группа пропустится
    db.add_group(1, 100, "A")
    db.add_group(2, 200, "B")
    monkeypatch.setattr(collector.vk, "fetch_members_count", lambda token, gid: 10)
    result = collector.collect_all()
    assert result["total"] == 2
    assert result["ok"] == 1  # снят только пользователь с токеном
