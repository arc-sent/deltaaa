"""Фоновые задачи: периодический сбор снимков и ежедневный дайджест.

Всё крутится на job_queue из python-telegram-bot (тот же паттерн, что в
автопостере). Блокирующие вызовы VK/matplotlib выносим в executor, чтобы не
подвешивать event loop бота.
"""

import asyncio
import logging
import os
from datetime import time as dtime, timedelta

import pytz

import collector
import db
import reports

logger = logging.getLogger(__name__)

MSK = pytz.timezone("Europe/Moscow")

COLLECT_INTERVAL_MINUTES = int(os.getenv("COLLECT_INTERVAL_MINUTES", "60"))
DIGEST_HOUR = os.getenv("DIGEST_HOUR", "10")
DIGEST_MINUTE = os.getenv("DIGEST_MINUTE", "0")
DIGEST_PERIOD_DAYS = 7  # какой период показывать в утреннем дайджесте


# ─── Задачи ───────────────────────────────────────────────────────────────────

async def _collect_job(context) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, collector.collect_all)


async def _digest_job(context) -> None:
    """Утренний дайджест: краткий текстовый отчёт по каждой группе пользователя."""
    loop = asyncio.get_running_loop()
    for telegram_id in db.get_digest_users():
        groups = db.get_groups(telegram_id)
        if not groups:
            continue
        token = db.get_vk_token(telegram_id)
        try:
            if len(groups) == 1:
                text = await loop.run_in_executor(
                    None, reports.build_text_report, groups[0], DIGEST_PERIOD_DAYS, token
                )
            else:
                text = await loop.run_in_executor(
                    None, reports.build_comparison_text, list(groups), DIGEST_PERIOD_DAYS
                )
            await context.bot.send_message(telegram_id, "☀️ Ежедневный дайджест\n\n" + text)
        except Exception:
            logger.exception("Не удалось отправить дайджест пользователю %s", telegram_id)


# ─── Регистрация ──────────────────────────────────────────────────────────────

def register_jobs(app) -> None:
    """Повесить периодический сбор и (если задано время) ежедневный дайджест."""
    app.job_queue.run_repeating(
        _collect_job,
        interval=timedelta(minutes=COLLECT_INTERVAL_MINUTES),
        first=timedelta(seconds=10),  # первый сбор почти сразу после старта
        name="collect_snapshots",
    )

    if DIGEST_HOUR.strip() != "":
        try:
            hour, minute = int(DIGEST_HOUR), int(DIGEST_MINUTE or 0)
            app.job_queue.run_daily(
                _digest_job,
                time=dtime(hour=hour, minute=minute, tzinfo=MSK),
                name="daily_digest",
            )
            logger.info("Ежедневный дайджест в %02d:%02d МСК", hour, minute)
        except ValueError:
            logger.warning("Некорректное время дайджеста DIGEST_HOUR/MINUTE — рассылка выключена")


async def collect_now(app) -> None:
    """Разовый сбор при старте (post_init), чтобы данные появились сразу."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, collector.collect_all)
