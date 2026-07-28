"""Обращения к VK API для статистики сообществ.

Резолв групп по ссылке/имени взят из автопостера. Плюс два «читающих» вызова
для аналитики:

- ``fetch_members_count`` — текущее число подписчиков (groups.getById). Динамику
  VK за прошлое не отдаёт, поэтому её мы копим сами (снимки в БД).
- ``fetch_stats`` — охваты/посещаемость (stats.get). Работает ТОЛЬКО если владелец
  токена — администратор сообщества; иначе VK вернёт ошибку доступа.

VKError несёт код и этап, чтобы отличать временные сбои от фатальных.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.199"

# Коды ошибок VK, при которых имеет смысл повторить запрос.
VK_RETRYABLE_ERROR_CODES = {1, 6, 9, 10}  # неизвестная / too many / flood / internal


class VKError(RuntimeError):
    """Ошибка обращения к VK с кодом и этапом.

    network=True помечает сетевой сбой (а не ответ VK с error_code) — такие
    ошибки тоже имеет смысл повторять.
    """

    def __init__(self, code: int | None, message: str, *, stage: str | None = None,
                 network: bool = False):
        self.code = code
        self.stage = stage
        self.network = network
        super().__init__(message)


def _call(method: str, params: dict, *, stage: str) -> dict:
    """Единый вызов VK API. Бросает VKError на сетевой сбой или ответ с error."""
    url = f"https://api.vk.com/method/{method}"
    body = {"v": VK_API_VERSION, **params}
    try:
        resp = requests.post(url, data=body, timeout=30).json()
    except requests.exceptions.RequestException as exc:
        raise VKError(None, f"сетевая ошибка: {exc}", stage=stage, network=True) from exc
    if "error" in resp:
        e = resp["error"]
        raise VKError(e.get("error_code"),
                      f"VK {e.get('error_code')}: {e.get('error_msg')}", stage=stage)
    return resp.get("response")


# ─── Резолв группы по ссылке/имени ────────────────────────────────────────────

def resolve_screen_name(vk_token: str, screen_name: str) -> dict | None:
    """utils.resolveScreenName: короткое имя -> {type, object_id}. None при сбое."""
    try:
        resp = requests.get(
            "https://api.vk.com/method/utils.resolveScreenName",
            params={"access_token": vk_token, "v": VK_API_VERSION, "screen_name": screen_name},
            timeout=15,
        ).json()
    except Exception:
        logger.exception("Ошибка resolveScreenName")
        return None
    if "error" in resp:
        logger.info("resolveScreenName error: %s", resp["error"])
        return None
    return resp.get("response") or None


def fetch_group_name(vk_token: str, group_id: int) -> str | None:
    """Название группы через groups.getById. None — если не удалось."""
    try:
        resp = requests.get(
            "https://api.vk.com/method/groups.getById",
            params={"access_token": vk_token, "v": VK_API_VERSION, "group_id": group_id},
            timeout=15,
        ).json()
    except Exception:
        logger.exception("Ошибка groups.getById")
        return None
    if "error" in resp:
        logger.info("groups.getById error: %s", resp["error"])
        return None
    response = resp.get("response")
    try:
        # 5.199 отдаёт {"groups": [...]}; старые версии — просто список.
        if isinstance(response, dict):
            return response["groups"][0]["name"]
        if isinstance(response, list):
            return response[0]["name"]
    except (KeyError, IndexError, TypeError):
        pass
    return None


# ─── Число подписчиков ────────────────────────────────────────────────────────

def fetch_members_count(vk_token: str, group_id: int) -> int:
    """Текущее число подписчиков сообщества (groups.getById, fields=members_count)."""
    response = _call(
        "groups.getById",
        {"access_token": vk_token, "group_id": abs(int(group_id)), "fields": "members_count"},
        stage="VK groups.getById",
    )
    try:
        if isinstance(response, dict):
            group = response["groups"][0]
        else:
            group = response[0]
        return int(group["members_count"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise VKError(None, "В ответе нет members_count (нет доступа к сообществу?)",
                      stage="VK groups.getById") from exc


# ─── Статистика сообщества (только для админов) ────────────────────────────────

def fetch_stats(vk_token: str, group_id: int, *, days: int = 30) -> list[dict]:
    """Дневная статистика сообщества за период (stats.get).

    Возвращает список записей по дням, каждая — с полями вида
    ``{"date": epoch_start, "reach": int, "reach_subscribers": int,
       "visitors": int, "views": int, "subscribed": int, "unsubscribed": int}``.
    Отсутствующие у VK поля просто опускаются. Требует прав администратора
    сообщества — иначе VKError (код 15/100/203 и т.п.).
    """
    now = int(time.time())
    response = _call(
        "stats.get",
        {
            "access_token": vk_token,
            "group_id": abs(int(group_id)),
            "timestamp_from": now - days * 86400,
            "timestamp_to": now,
            "interval": "day",
            "intervals_count": days,
            "extended": 1,
        },
        stage="VK stats.get",
    )
    result: list[dict] = []
    for period in response or []:
        day = {"date": period.get("period_from")}
        reach = period.get("reach") or {}
        if isinstance(reach, dict):
            if "reach" in reach:
                day["reach"] = reach["reach"]
            if "reach_subscribers" in reach:
                day["reach_subscribers"] = reach["reach_subscribers"]
        visitors = period.get("visitors") or {}
        if isinstance(visitors, dict):
            if "visitors" in visitors:
                day["visitors"] = visitors["visitors"]
            if "views" in visitors:
                day["views"] = visitors["views"]
        activity = period.get("activity") or {}
        if isinstance(activity, dict):
            if "subscribed" in activity:
                day["subscribed"] = activity["subscribed"]
            if "unsubscribed" in activity:
                day["unsubscribed"] = activity["unsubscribed"]
        result.append(day)
    return result


def fetch_stats_safe(vk_token: str, group_id: int, *, days: int = 30) -> list[dict] | None:
    """Как fetch_stats, но глушит ошибки (нет прав/сеть) и возвращает None.

    Удобно для отчёта: расширенная статистика — «бонус», её отсутствие не должно
    ломать основной отчёт по подписчикам.
    """
    try:
        return fetch_stats(vk_token, group_id, days=days)
    except VKError as exc:
        logger.info("stats.get недоступен для группы %s: %s", group_id, exc)
        return None
