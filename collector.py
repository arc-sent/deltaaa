"""Сбор снимков числа подписчиков по всем отслеживаемым группам.

VK не отдаёт историю members_count, поэтому мы периодически опрашиваем API и
складываем точки в БД — из них потом строятся графики роста/оттока. Функции
блокирующие (requests), поэтому планировщик вызывает их в executor'е.
"""

import logging
import traceback

import db
import vk

logger = logging.getLogger(__name__)


def collect_group(group) -> bool:
    """Снять один снимок по группе. True — успех, False — пропущено/ошибка.

    Токен берём владельца группы. Если токена нет — тихо пропускаем (пользователь
    ещё не настроил). Ошибки VK логируем в error_logs владельца.
    """
    token = db.get_vk_token(group["telegram_id"])
    if not token:
        return False
    try:
        count = vk.fetch_members_count(token, group["vk_group_id"])
    except vk.VKError as exc:
        db.log_error(
            group["telegram_id"],
            stage=exc.stage or "сбор подписчиков",
            vk_group_id=group["vk_group_id"],
            vk_group_name=group["name"],
            error_code=exc.code,
            message=str(exc),
        )
        return False
    except Exception as exc:  # непредвиденное — тоже фиксируем
        db.log_error(
            group["telegram_id"],
            stage="сбор подписчиков",
            vk_group_id=group["vk_group_id"],
            vk_group_name=group["name"],
            message=str(exc),
            traceback=traceback.format_exc(),
        )
        return False
    db.add_snapshot(group["id"], count)
    return True


def collect_all() -> dict:
    """Пройтись по всем группам всех пользователей. Возвращает счётчики для лога."""
    groups = db.get_all_groups()
    ok = 0
    for group in groups:
        if collect_group(group):
            ok += 1
    result = {"total": len(groups), "ok": ok}
    logger.info("Сбор снимков: групп %s, снято %s", result["total"], result["ok"])
    return result
