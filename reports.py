"""Построение отчётов по подписчикам: текст для сообщения, PNG-графики и PDF.

Основной источник — снимки members_count из БД (динамика роста/оттока). Если у
пользователя есть права админа, отчёт дополняется охватами из stats.get.

Все графики строятся в памяти (backend Agg) и возвращаются как bytes, чтобы бот
слал их напрямую в Telegram (send_photo / send_document) без временных файлов.
"""

import io
import logging
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")  # без дисплея — рендер в файл/буфер
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.dates import DateFormatter
import pytz

import db
import vk

logger = logging.getLogger(__name__)

MSK = pytz.timezone("Europe/Moscow")

# Понятные подписи периодов для кнопок/заголовков.
PERIODS = {7: "7 дней", 30: "30 дней", 90: "90 дней"}


# ─── Подготовка рядов данных ──────────────────────────────────────────────────

def _daily_series(snapshots) -> list[tuple[datetime, int]]:
    """Свести снимки к одной точке на день (последнее значение за день, МСК)."""
    by_day: dict[str, tuple[datetime, int]] = {}
    for row in snapshots:
        dt = datetime.fromtimestamp(row["ts"], tz=MSK)
        key = dt.strftime("%Y-%m-%d")
        # снимки идут по возрастанию ts, поэтому последний перезапишет — то что нужно
        by_day[key] = (dt.replace(hour=0, minute=0, second=0, microsecond=0), row["members_count"])
    return [by_day[k] for k in sorted(by_day)]


def group_summary(group_row_id: int, days: int) -> dict:
    """Сводка по группе за период: текущее, прирост, %, средний темп, лучший/худший день.

    baseline берётся из ближайшего снимка ДО начала периода (если есть) — так
    прирост считается честно, даже если внутри периода мало точек.
    """
    now = int(datetime.now(tz=MSK).timestamp())
    since = now - days * 86400
    snaps = db.get_snapshots(group_row_id, since_ts=since)
    latest = db.get_latest_snapshot(group_row_id)
    baseline = db.get_snapshot_before(group_row_id, since)

    summary = {
        "days": days,
        "current": latest["members_count"] if latest else None,
        "start": None,
        "delta": None,
        "pct": None,
        "avg_per_day": None,
        "best": None,   # (date, delta)
        "worst": None,  # (date, delta)
        "series": _daily_series(snaps),
        "points": len(snaps),
    }
    if latest is None:
        return summary

    start_val = baseline["members_count"] if baseline else (
        snaps[0]["members_count"] if snaps else None
    )
    summary["start"] = start_val
    if start_val is not None:
        delta = latest["members_count"] - start_val
        summary["delta"] = delta
        summary["pct"] = (delta / start_val * 100) if start_val else None
        summary["avg_per_day"] = delta / days

    # Лучший/худший день по дневному приросту (нужно ≥2 дневных точек).
    series = summary["series"]
    if baseline:
        series = [(datetime.fromtimestamp(baseline["ts"], tz=MSK), baseline["members_count"])] + series
    if len(series) >= 2:
        diffs = [(series[i][0], series[i][1] - series[i - 1][1]) for i in range(1, len(series))]
        summary["best"] = max(diffs, key=lambda d: d[1])
        summary["worst"] = min(diffs, key=lambda d: d[1])
    return summary


# ─── Текстовый отчёт (в сообщение Telegram) ───────────────────────────────────

def _fmt_signed(n: int | None) -> str:
    if n is None:
        return "—"
    return f"+{n}" if n > 0 else str(n)


def build_text_report(group, days: int, vk_token: str | None = None) -> str:
    """Короткий отчёт по одной группе — для сообщения в чат."""
    s = group_summary(group["id"], days)
    title = f"📈 {group['name']}\nОтчёт за {PERIODS.get(days, f'{days} дн.')}\n"

    if s["current"] is None:
        return (title + "\n📭 Пока нет данных. Снимки собираются автоматически — "
                "загляни позже (первые точки появятся в течение часа).")

    lines = [
        title,
        f"👥 Подписчиков сейчас: {s['current']}",
    ]
    if s["delta"] is not None:
        pct = f" ({s['pct']:+.1f}%)" if s["pct"] is not None else ""
        arrow = "🟢" if s["delta"] > 0 else ("🔴" if s["delta"] < 0 else "⚪")
        lines.append(f"{arrow} Изменение за период: {_fmt_signed(s['delta'])}{pct}")
        lines.append(f"📅 Было в начале: {s['start']}")
    if s["avg_per_day"] is not None:
        lines.append(f"📊 Средний темп: {s['avg_per_day']:+.1f}/день")
    if s["best"]:
        lines.append(f"🚀 Лучший день: {s['best'][0].strftime('%d.%m')} ({_fmt_signed(s['best'][1])})")
    if s["worst"] and s["worst"][1] < 0:
        lines.append(f"📉 Отток: {s['worst'][0].strftime('%d.%m')} ({_fmt_signed(s['worst'][1])})")

    # Бонус: охваты из stats.get (только для админов группы).
    if vk_token:
        stats = vk.fetch_stats_safe(vk_token, group["vk_group_id"], days=min(days, 30))
        if stats:
            reach = [d["reach"] for d in stats if "reach" in d]
            views = [d["views"] for d in stats if "views" in d]
            if reach:
                lines.append(f"👁 Средний охват/день: {int(sum(reach) / len(reach))}")
            if views:
                lines.append(f"🔎 Средние просмотры/день: {int(sum(views) / len(views))}")

    lines.append("\n📄 Нажми кнопку ниже — пришлю подробный PDF с графиками.")
    return "\n".join(lines)


def build_comparison_text(groups, days: int) -> str:
    """Сравнение нескольких групп — таблицей в сообщении."""
    lines = [f"📊 Сравнение групп за {PERIODS.get(days, f'{days} дн.')}\n"]
    rows = []
    for g in groups:
        s = group_summary(g["id"], days)
        rows.append((g["name"], s))
    # сортируем по приросту (кто вырос сильнее — выше)
    rows.sort(key=lambda r: (r[1]["delta"] is None, -(r[1]["delta"] or 0)))
    for name, s in rows:
        if s["current"] is None:
            lines.append(f"• {name}: нет данных")
            continue
        pct = f" ({s['pct']:+.1f}%)" if s["pct"] is not None else ""
        lines.append(f"• {name}: {s['current']} чел., {_fmt_signed(s['delta'])}{pct}")
    lines.append("\n📄 Кнопка ниже — PDF со сравнительным графиком.")
    return "\n".join(lines)


# ─── Графики ──────────────────────────────────────────────────────────────────

def _plot_growth(ax, summary, label=None):
    series = summary["series"]
    if not series:
        ax.text(0.5, 0.5, "нет данных", ha="center", va="center", transform=ax.transAxes)
        return
    xs = [d for d, _ in series]
    ys = [v for _, v in series]
    ax.plot(xs, ys, marker="o", markersize=3, linewidth=1.8, label=label)
    ax.xaxis.set_major_formatter(DateFormatter("%d.%m"))
    ax.grid(True, alpha=0.3)


def growth_chart_png(group, days: int) -> bytes:
    s = group_summary(group["id"], days)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    _plot_growth(ax, s)
    ax.set_title(f"{group['name']} — подписчики за {PERIODS.get(days, f'{days} дн.')}")
    ax.set_ylabel("Подписчиков")
    fig.autofmt_xdate()
    fig.tight_layout()
    return _fig_to_png(fig)


def comparison_chart_png(groups, days: int) -> bytes:
    fig, ax = plt.subplots(figsize=(9, 4.5))
    any_data = False
    for g in groups:
        s = group_summary(g["id"], days)
        if s["series"]:
            any_data = True
        _plot_growth(ax, s, label=g["name"])
    if not any_data:
        ax.text(0.5, 0.5, "нет данных", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(f"Сравнение групп за {PERIODS.get(days, f'{days} дн.')}")
    ax.set_ylabel("Подписчиков")
    ax.legend(fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    return _fig_to_png(fig)


def _fig_to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ─── PDF-отчёты ───────────────────────────────────────────────────────────────

def _text_page(pdf, title: str, body_lines: list[str]):
    fig = plt.figure(figsize=(8.27, 11.69))  # A4
    fig.text(0.08, 0.94, title, fontsize=18, weight="bold", va="top")
    fig.text(0.08, 0.87, "\n".join(body_lines), fontsize=12, va="top", family="monospace")
    fig.text(0.08, 0.05, f"Сформировано {datetime.now(tz=MSK).strftime('%d.%m.%Y %H:%M')} МСК",
             fontsize=8, color="gray")
    pdf.savefig(fig)
    plt.close(fig)


def build_group_pdf(group, days: int, vk_token: str | None = None) -> bytes:
    """Подробный PDF по одной группе: сводка, график роста, дневные приросты,
    охваты (если доступны через stats.get)."""
    s = group_summary(group["id"], days)
    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        # Стр. 1 — сводка текстом
        body = [f"Период: {PERIODS.get(days, f'{days} дн.')}", ""]
        if s["current"] is None:
            body.append("Пока нет собранных данных по подписчикам.")
        else:
            body += [
                f"Подписчиков сейчас : {s['current']}",
                f"Было в начале      : {s['start'] if s['start'] is not None else '—'}",
                f"Изменение          : {_fmt_signed(s['delta'])}"
                + (f"  ({s['pct']:+.1f}%)" if s["pct"] is not None else ""),
                f"Средний темп       : "
                + (f"{s['avg_per_day']:+.1f}/день" if s["avg_per_day"] is not None else "—"),
            ]
            if s["best"]:
                body.append(f"Лучший день        : {s['best'][0].strftime('%d.%m.%Y')} "
                            f"({_fmt_signed(s['best'][1])})")
            if s["worst"]:
                body.append(f"Худший день        : {s['worst'][0].strftime('%d.%m.%Y')} "
                            f"({_fmt_signed(s['worst'][1])})")
            body.append(f"Точек данных       : {s['points']}")
        _text_page(pdf, f"Отчёт: {group['name']}", body)

        # Стр. 2 — график роста
        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        _plot_growth(ax, s)
        ax.set_title(f"Динамика подписчиков — {group['name']}")
        ax.set_ylabel("Подписчиков")
        fig.autofmt_xdate()
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # Стр. 3 — дневные приросты (бар)
        series = s["series"]
        if len(series) >= 2:
            days_x = [series[i][0] for i in range(1, len(series))]
            diffs = [series[i][1] - series[i - 1][1] for i in range(1, len(series))]
            colors = ["#2e9e5b" if d >= 0 else "#c0392b" for d in diffs]
            fig, ax = plt.subplots(figsize=(11.69, 8.27))
            ax.bar(days_x, diffs, color=colors, width=0.8)
            ax.axhline(0, color="black", linewidth=0.8)
            ax.xaxis.set_major_formatter(DateFormatter("%d.%m"))
            ax.set_title(f"Дневной прирост/отток — {group['name']}")
            ax.set_ylabel("Изменение за день")
            ax.grid(True, alpha=0.3)
            fig.autofmt_xdate()
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)

        # Стр. 4 — охваты из stats.get (если есть права)
        if vk_token:
            stats = vk.fetch_stats_safe(vk_token, group["vk_group_id"], days=min(days, 30))
            if stats:
                _reach_page(pdf, group, stats)
    buf.seek(0)
    return buf.read()


def _reach_page(pdf, group, stats):
    xs = [datetime.fromtimestamp(d["date"], tz=MSK) for d in stats if d.get("date")]
    reach = [d.get("reach") for d in stats if d.get("date")]
    views = [d.get("views") for d in stats if d.get("date")]
    if not any(v is not None for v in reach) and not any(v is not None for v in views):
        return
    fig, ax = plt.subplots(figsize=(11.69, 8.27))
    if any(v is not None for v in reach):
        ax.plot(xs, [v or 0 for v in reach], marker="o", markersize=3, label="Охват")
    if any(v is not None for v in views):
        ax.plot(xs, [v or 0 for v in views], marker="s", markersize=3, label="Просмотры")
    ax.xaxis.set_major_formatter(DateFormatter("%d.%m"))
    ax.set_title(f"Охваты и просмотры — {group['name']}")
    ax.set_ylabel("Пользователей/день")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


def build_comparison_pdf(groups, days: int) -> bytes:
    """PDF со сравнением групп: сводная таблица + общий график."""
    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        rows = [(g, group_summary(g["id"], days)) for g in groups]
        rows.sort(key=lambda r: (r[1]["delta"] is None, -(r[1]["delta"] or 0)))
        body = [f"Период: {PERIODS.get(days, f'{days} дн.')}", ""]
        body.append(f"{'Группа':<24}{'Сейчас':>10}{'Δ':>10}{'%':>9}")
        body.append("-" * 53)
        for g, s in rows:
            if s["current"] is None:
                body.append(f"{g['name'][:22]:<24}{'нет данных':>29}")
            else:
                pct = f"{s['pct']:+.1f}" if s["pct"] is not None else "—"
                body.append(f"{g['name'][:22]:<24}{s['current']:>10}"
                            f"{_fmt_signed(s['delta']):>10}{pct:>9}")
        _text_page(pdf, "Сравнение групп", body)

        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        for g, s in rows:
            _plot_growth(ax, s, label=g["name"])
        ax.set_title(f"Сравнение динамики подписчиков — {PERIODS.get(days, f'{days} дн.')}")
        ax.set_ylabel("Подписчиков")
        ax.legend(fontsize=9)
        fig.autofmt_xdate()
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)
    buf.seek(0)
    return buf.read()
