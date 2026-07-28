"""Тесты построения отчётов: сводка, текст, графики (PNG) и PDF.

Данные снимков закладываем относительно «сейчас» в МСК, чтобы попадать в
периоды 7/30/90 дней. Сеть не трогаем: во всех вызовах vk_token=None, поэтому
stats.get (охваты) не запрашивается.
"""

from datetime import datetime

import db
import reports

DAY = 86400


def _now() -> int:
    return int(datetime.now(tz=reports.MSK).timestamp())


def _seed(gid):
    """Базовая точка до периода + рост внутри последних 30 дней."""
    now = _now()
    db.add_snapshot(gid, 1000, ts=now - 40 * DAY)  # baseline (до 30-дневного окна)
    db.add_snapshot(gid, 1100, ts=now - 25 * DAY)
    db.add_snapshot(gid, 1150, ts=now - 10 * DAY)
    db.add_snapshot(gid, 1200, ts=now - 1 * DAY)


# ─── Сводка ───────────────────────────────────────────────────────────────────

def test_group_summary_uses_baseline_before_period(user_group):
    gid = user_group["group_row_id"]
    _seed(gid)
    s = reports.group_summary(gid, 30)
    assert s["current"] == 1200
    assert s["start"] == 1000        # baseline из точки ДО начала периода
    assert s["delta"] == 200
    assert round(s["pct"], 1) == 20.0
    assert s["best"] is not None and s["best"][1] > 0


def test_group_summary_empty(user_group):
    s = reports.group_summary(user_group["group_row_id"], 30)
    assert s["current"] is None
    assert s["delta"] is None
    assert s["series"] == []


def test_group_summary_negative_growth(user_group):
    gid = user_group["group_row_id"]
    now = _now()
    db.add_snapshot(gid, 500, ts=now - 20 * DAY)
    db.add_snapshot(gid, 460, ts=now - 2 * DAY)
    s = reports.group_summary(gid, 30)
    assert s["delta"] == -40
    assert s["worst"][1] < 0


# ─── Текстовый отчёт ──────────────────────────────────────────────────────────

def test_build_text_report_contains_key_numbers(user_group):
    gid = user_group["group_row_id"]
    _seed(gid)
    group = db.get_group(gid)
    text = reports.build_text_report(group, 30)
    assert "Тестовая группа" in text
    assert "1200" in text          # текущее число подписчиков
    assert "+200" in text          # прирост за период
    assert "PDF" in text           # подсказка про подробный отчёт


def test_build_text_report_no_data(user_group):
    group = db.get_group(user_group["group_row_id"])
    text = reports.build_text_report(group, 30)
    assert "нет данных" in text.lower()


def test_build_comparison_text_sorts_by_growth():
    db.ensure_user(7)
    g1 = db.add_group(7, 111, "Медленная")
    g2 = db.add_group(7, 222, "Быстрая")
    now = _now()
    db.add_snapshot(g1, 1000, ts=now - 20 * DAY)
    db.add_snapshot(g1, 1010, ts=now - 1 * DAY)   # +10
    db.add_snapshot(g2, 1000, ts=now - 20 * DAY)
    db.add_snapshot(g2, 1200, ts=now - 1 * DAY)   # +200
    groups = db.get_groups(7)
    text = reports.build_comparison_text(list(groups), 30)
    # «Быстрая» должна идти выше «Медленной» (сортировка по приросту)
    assert text.index("Быстрая") < text.index("Медленная")


# ─── Графики и PDF ────────────────────────────────────────────────────────────

def test_growth_chart_png(user_group):
    gid = user_group["group_row_id"]
    _seed(gid)
    png = reports.growth_chart_png(db.get_group(gid), 30)
    assert png[:4] == b"\x89PNG"


def test_growth_chart_png_empty_still_renders(user_group):
    # даже без данных график должен построиться (с надписью «нет данных»)
    png = reports.growth_chart_png(db.get_group(user_group["group_row_id"]), 30)
    assert png[:4] == b"\x89PNG"


def test_build_group_pdf(user_group):
    gid = user_group["group_row_id"]
    _seed(gid)
    pdf = reports.build_group_pdf(db.get_group(gid), 30)
    assert pdf[:4] == b"%PDF"


def test_build_comparison_pdf_and_png():
    db.ensure_user(8)
    g1 = db.add_group(8, 111, "A")
    g2 = db.add_group(8, 222, "B")
    now = _now()
    for gid, base in ((g1, 1000), (g2, 2000)):
        db.add_snapshot(gid, base, ts=now - 20 * DAY)
        db.add_snapshot(gid, base + 50, ts=now - 1 * DAY)
    groups = list(db.get_groups(8))
    assert reports.build_comparison_pdf(groups, 30)[:4] == b"%PDF"
    assert reports.comparison_chart_png(groups, 30)[:4] == b"\x89PNG"
