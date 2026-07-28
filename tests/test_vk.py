"""Тесты слоя VK API. Сеть замокана — реальных запросов нет."""

import requests
import pytest

import vk
from conftest import FakeResponse


# ─── members_count ────────────────────────────────────────────────────────────

def test_fetch_members_count_ok(monkeypatch):
    payload = {"response": {"groups": [{"id": 555, "members_count": 12345}]}}
    monkeypatch.setattr(vk.requests, "post", lambda *a, **k: FakeResponse(payload))
    assert vk.fetch_members_count("tok", 555) == 12345


def test_fetch_members_count_legacy_list_shape(monkeypatch):
    # Старые версии VK отдают просто список, а не {"groups": [...]}
    payload = {"response": [{"id": 555, "members_count": 777}]}
    monkeypatch.setattr(vk.requests, "post", lambda *a, **k: FakeResponse(payload))
    assert vk.fetch_members_count("tok", 555) == 777


def test_fetch_members_count_vk_error(monkeypatch):
    payload = {"error": {"error_code": 15, "error_msg": "Access denied"}}
    monkeypatch.setattr(vk.requests, "post", lambda *a, **k: FakeResponse(payload))
    with pytest.raises(vk.VKError) as exc:
        vk.fetch_members_count("tok", 555)
    assert exc.value.code == 15


def test_call_network_error_marks_network(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectTimeout("timeout")
    monkeypatch.setattr(vk.requests, "post", boom)
    with pytest.raises(vk.VKError) as exc:
        vk.fetch_members_count("tok", 555)
    assert exc.value.network is True


# ─── stats.get ────────────────────────────────────────────────────────────────

def _stats_payload():
    return {"response": [
        {
            "period_from": 1700000000,
            "reach": {"reach": 500, "reach_subscribers": 300},
            "visitors": {"visitors": 100, "views": 250},
            "activity": {"subscribed": 12, "unsubscribed": 3},
        },
        {
            "period_from": 1700086400,
            "reach": {"reach": 600},
            "visitors": {"views": 400},
        },
    ]}


def test_fetch_stats_parses_fields(monkeypatch):
    monkeypatch.setattr(vk.requests, "post", lambda *a, **k: FakeResponse(_stats_payload()))
    stats = vk.fetch_stats("tok", 555, days=2)
    assert len(stats) == 2
    assert stats[0]["reach"] == 500
    assert stats[0]["reach_subscribers"] == 300
    assert stats[0]["views"] == 250
    assert stats[0]["subscribed"] == 12
    # во второй записи часть полей отсутствует — они просто опущены
    assert "visitors" not in stats[1]
    assert stats[1]["views"] == 400


def test_fetch_stats_safe_swallows_error(monkeypatch):
    payload = {"error": {"error_code": 15, "error_msg": "no rights"}}
    monkeypatch.setattr(vk.requests, "post", lambda *a, **k: FakeResponse(payload))
    assert vk.fetch_stats_safe("tok", 555, days=7) is None  # ошибка -> None, не исключение


# ─── Резолв групп ─────────────────────────────────────────────────────────────

def test_resolve_screen_name_ok(monkeypatch):
    payload = {"response": {"type": "group", "object_id": 42}}
    monkeypatch.setattr(vk.requests, "get", lambda *a, **k: FakeResponse(payload))
    obj = vk.resolve_screen_name("tok", "durov")
    assert obj["object_id"] == 42


def test_resolve_screen_name_error_returns_none(monkeypatch):
    payload = {"error": {"error_code": 1, "error_msg": "x"}}
    monkeypatch.setattr(vk.requests, "get", lambda *a, **k: FakeResponse(payload))
    assert vk.resolve_screen_name("tok", "durov") is None


def test_fetch_group_name(monkeypatch):
    payload = {"response": {"groups": [{"name": "Моё сообщество"}]}}
    monkeypatch.setattr(vk.requests, "get", lambda *a, **k: FakeResponse(payload))
    assert vk.fetch_group_name("tok", 555) == "Моё сообщество"
