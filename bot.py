"""Сервис статистики VK-групп: Telegram-бот для отслеживания подписчиков.

Пользователь задаёт VK-токен и добавляет группы. Бот раз в час снимает число
подписчиков (scheduler + collector) и по запросу выдаёт отчёт:
  • краткий — текстом прямо в сообщении + график;
  • подробный — PDF по кнопке (сводка, динамика, дневные приросты, охваты).

Интерфейс повторяет автопостер: меню на Reply-кнопках, управление на inline-
кнопках, пошаговый ввод через ConversationHandler, журнал ошибок с /errors.
"""

import os
import re
import asyncio
import logging
from io import BytesIO
from datetime import datetime, timedelta

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters,
    ContextTypes, PicklePersistence,
)
from dotenv import load_dotenv

import db
import scheduler
import reports
from vk import resolve_screen_name, fetch_group_name

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MOSCOW_TZ = pytz.timezone("Europe/Moscow")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
ERROR_RETENTION_DAYS = int(os.getenv("ERROR_RETENTION_DAYS", "5"))
ERROR_CLEANUP_INTERVAL_DAYS = int(os.getenv("ERROR_CLEANUP_INTERVAL_DAYS", "5"))
ERRORS_PAGE_SIZE = 8
DEFAULT_PERIOD = 30

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Состояния разговоров ─────────────────────────────────────────────────────
TOKEN_WAIT = 10
G_ADD_ID, G_ADD_CONFIRM, G_ADD_NAME, G_RENAME = range(20, 24)

# ─── Постоянное меню ──────────────────────────────────────────────────────────
BTN_TOKEN = "🔑 Токен"
BTN_GROUPS = "👥 Группы"
BTN_REPORT = "📈 Отчёт"
BTN_COMPARE = "📊 Сравнение"
BTN_DIGEST = "📅 Дайджест"
MENU_BUTTON_TEXTS = [BTN_TOKEN, BTN_GROUPS, BTN_REPORT, BTN_COMPARE, BTN_DIGEST]


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_TOKEN, BTN_GROUPS], [BTN_REPORT, BTN_COMPARE], [BTN_DIGEST]],
        resize_keyboard=True,
    )


def _mask_token(token: str) -> str:
    if len(token) <= 12:
        return "•" * len(token)
    return f"{token[:6]}…{token[-4:]}"


# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Я Δ Delta — слежу за подписчиками твоих VK-групп и строю отчёты.\n\n"
        "Как настроить:\n"
        f"1. {BTN_TOKEN} — задай VK токен (нужны права админа групп для охватов).\n"
        f"2. {BTN_GROUPS} — добавь группы, за которыми следить.\n"
        f"3. {BTN_REPORT} — смотри отчёт по группе: текст + график, а по кнопке — PDF.\n"
        f"4. {BTN_COMPARE} — сравнивай несколько групп на одном графике.\n"
        f"5. {BTN_DIGEST} — включи ежедневную сводку по утрам.\n\n"
        "Число подписчиков я снимаю автоматически раз в час — динамика копится сама.",
        reply_markup=main_keyboard(),
    )


# ─── Меню ─────────────────────────────────────────────────────────────────────

async def main_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    telegram_id = update.effective_user.id
    db.ensure_user(telegram_id)
    if text == BTN_TOKEN:
        await show_token_status(update, context)
    elif text == BTN_GROUPS:
        await update.message.reply_text("Твои группы VK:", reply_markup=groups_kb(telegram_id))
    elif text == BTN_REPORT:
        await update.message.reply_text("Выбери группу для отчёта:", reply_markup=report_list_kb(telegram_id))
    elif text == BTN_COMPARE:
        context.user_data["cmp_selected"] = set()
        await update.message.reply_text(
            "Отметь группы для сравнения, затем нажми «Построить»:",
            reply_markup=compare_kb(telegram_id, set()),
        )
    elif text == BTN_DIGEST:
        await show_digest(update, context)


# ─── Токен ────────────────────────────────────────────────────────────────────

def token_kb(has_token: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        "✏️ Изменить токен" if has_token else "➕ Задать токен", callback_data="settoken_change"
    )]]
    if has_token:
        rows.append([InlineKeyboardButton("🗑 Удалить токен", callback_data="settoken_delete")])
    return InlineKeyboardMarkup(rows)


async def show_token_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = db.get_vk_token(update.effective_user.id)
    msg = (f"🔑 Токен задан: {_mask_token(token)}\n(показан частично)"
           if token else "❌ Токен не задан.")
    await update.message.reply_text(msg, reply_markup=token_kb(bool(token)))


SETTOKEN_PROMPT = (
    "Пришли свой VK токен.\n\n"
    "Как получить через Kate Mobile:\n"
    "1. Открой в браузере:\n"
    "https://oauth.vk.com/authorize?client_id=2685278&scope=1073737727&redirect_uri=https://oauth.vk.com/blank.html&display=page&response_type=token\n"
    "2. Войди и разреши доступ.\n"
    "3. Скопируй access_token из адресной строки.\n\n"
    "⚠️ Для охватов (stats.get) токен должен принадлежать администратору групп."
)


async def cmd_settoken(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(SETTOKEN_PROMPT)
    return TOKEN_WAIT


async def settoken_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(SETTOKEN_PROMPT)
    return TOKEN_WAIT


async def handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token = update.message.text.strip()
    if token in MENU_BUTTON_TEXTS:
        await update.message.reply_text("Ввод токена отменён.", reply_markup=main_keyboard())
        return ConversationHandler.END
    is_new = token.startswith("vk1.a.") and len(token) >= 26
    is_old = len(token) >= 85 and not any(ch.isspace() for ch in token)
    if not (is_new or is_old):
        await update.message.reply_text(
            "❌ Это не похоже на VK токен. Пришли правильный или /cancel."
        )
        return TOKEN_WAIT
    db.set_vk_token(update.effective_user.id, token)
    await update.message.reply_text("✅ Токен сохранён.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def handle_token_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Токен удалён")
    db.clear_vk_token(update.effective_user.id)
    await query.edit_message_text("🗑 Токен удалён.", reply_markup=token_kb(False))


# ─── Группы ───────────────────────────────────────────────────────────────────

_VK_HOST_RE = re.compile(r"(?:https?://)?(?:m\.|www\.)?(?:vk\.com|vkontakte\.ru)/", re.IGNORECASE)


def _extract_screen_name(text: str) -> str:
    text = _VK_HOST_RE.sub("", text.strip())
    text = text.split("?")[0].split("#")[0].strip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def resolve_vk_group(vk_token: str | None, text: str) -> tuple[int | None, str | None, str | None]:
    raw = _extract_screen_name(text)
    if not raw:
        return None, None, "Пустая ссылка. Пришли ссылку на сообщество VK."
    m = re.match(r"(?:video|wall|clip|photo)-(\d+)", raw, re.IGNORECASE)
    if m:
        gid = int(m.group(1))
        return gid, fetch_group_name(vk_token, gid) if vk_token else None, None
    m = re.match(r"(?:club|public|event)(\d+)$", raw, re.IGNORECASE)
    if m:
        gid = int(m.group(1))
        return gid, fetch_group_name(vk_token, gid) if vk_token else None, None
    if re.fullmatch(r"-?\d+", raw):
        gid = abs(int(raw))
        return gid, fetch_group_name(vk_token, gid) if vk_token else None, None
    if not vk_token:
        return None, None, (
            f"Чтобы добавить группу по короткой ссылке, сначала задай VK токен ({BTN_TOKEN}). "
            "Либо пришли ссылку вида vk.com/club123."
        )
    obj = resolve_screen_name(vk_token, raw)
    if not obj:
        return None, None, "Не удалось найти сообщество по этой ссылке. Проверь её."
    if obj.get("type") not in ("group", "page"):
        return None, None, "Это не сообщество. Пришли ссылку именно на группу/паблик VK."
    gid = int(obj["object_id"])
    return gid, fetch_group_name(vk_token, gid), None


def groups_kb(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for g in db.get_groups(telegram_id):
        latest = db.get_latest_snapshot(g["id"])
        cnt = f" · {latest['members_count']}" if latest else ""
        rows.append([InlineKeyboardButton(f"{g['name']} (id {g['vk_group_id']}){cnt}", callback_data="noop")])
        rows.append([
            InlineKeyboardButton("✏️ Переименовать", callback_data=f"g_rename_{g['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"g_del_{g['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Добавить группу", callback_data="g_add")])
    return InlineKeyboardMarkup(rows)


async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db.ensure_user(update.effective_user.id)
    await update.message.reply_text("Твои группы VK:", reply_markup=groups_kb(update.effective_user.id))
    return ConversationHandler.END


async def groups_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data
    if data == "noop":
        await query.answer()
        return ConversationHandler.END
    if data == "g_add":
        await query.answer()
        await query.edit_message_text(
            "Пришли ссылку на сообщество VK (ID определю сам):\n"
            "• vk.com/club123456\n• vk.com/public123456\n• vk.com/my_group_name"
        )
        return G_ADD_ID
    if data.startswith("g_del_"):
        await query.answer("Удалено")
        db.delete_group(int(data.rsplit("_", 1)[1]))
        await query.edit_message_text("Твои группы VK:", reply_markup=groups_kb(update.effective_user.id))
        return ConversationHandler.END
    if data.startswith("g_rename_"):
        await query.answer()
        context.user_data["rename_group_id"] = int(data.rsplit("_", 1)[1])
        await query.edit_message_text("Введи новое название группы:")
        return G_RENAME
    await query.answer()
    return ConversationHandler.END


async def groups_add_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    vk_token = db.get_vk_token(update.effective_user.id)
    loop = asyncio.get_running_loop()
    group_id, name, error = await loop.run_in_executor(None, resolve_vk_group, vk_token, update.message.text)
    if error:
        await update.message.reply_text(error + "\n\nПопробуй ещё раз или /cancel.")
        return G_ADD_ID
    context.user_data["pending_group_id"] = group_id
    if name:
        context.user_data["pending_group_name"] = name
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Сохранить", callback_data="g_confirmname")],
            [InlineKeyboardButton("✏️ Задать своё имя", callback_data="g_manualname")],
        ])
        await update.message.reply_text(f"Нашёл: «{name}» (id {group_id}). Сохранить?", reply_markup=kb)
        return G_ADD_CONFIRM
    await update.message.reply_text(f"Сообщество найдено (id {group_id}). Введи название вручную:")
    return G_ADD_NAME


async def _save_group_and_snapshot(telegram_id: int, vk_group_id: int, name: str):
    """Сохранить группу и сразу снять первый снимок подписчиков (если есть токен)."""
    group_row_id = db.add_group(telegram_id, vk_group_id, name)
    token = db.get_vk_token(telegram_id)
    if token:
        try:
            import vk as _vk
            count = await asyncio.get_running_loop().run_in_executor(
                None, _vk.fetch_members_count, token, vk_group_id
            )
            db.add_snapshot(group_row_id, count)
            return count
        except Exception:
            logger.info("Не удалось снять первый снимок для группы %s", vk_group_id)
    return None


async def groups_add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    if query.data == "g_confirmname":
        count = await _save_group_and_snapshot(
            telegram_id, context.user_data["pending_group_id"],
            context.user_data["pending_group_name"],
        )
        extra = f"\nПодписчиков сейчас: {count}." if count is not None else ""
        await query.edit_message_text(f"✅ Группа добавлена.{extra}\n\nТвои группы VK:",
                                      reply_markup=groups_kb(telegram_id))
        return ConversationHandler.END
    await query.edit_message_text("Введи название группы вручную:")
    return G_ADD_NAME


async def groups_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    count = await _save_group_and_snapshot(
        telegram_id, context.user_data["pending_group_id"], update.message.text.strip()
    )
    extra = f"\nПодписчиков сейчас: {count}." if count is not None else ""
    await update.message.reply_text(f"✅ Группа добавлена.{extra}\n\nТвои группы VK:",
                                    reply_markup=groups_kb(telegram_id))
    return ConversationHandler.END


async def groups_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    db.rename_group(context.user_data["rename_group_id"], update.message.text.strip())
    await update.message.reply_text("✅ Переименовано.\n\nТвои группы VK:",
                                    reply_markup=groups_kb(telegram_id))
    return ConversationHandler.END


# ─── Отчёт по группе ──────────────────────────────────────────────────────────

def report_list_kb(telegram_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(g["name"], callback_data=f"rep_g_{g['id']}_{DEFAULT_PERIOD}")]
            for g in db.get_groups(telegram_id)]
    if not rows:
        rows = [[InlineKeyboardButton("Сначала добавь группу 👥", callback_data="noop")]]
    return InlineKeyboardMarkup(rows)


def report_kb(group_row_id: int, days: int) -> InlineKeyboardMarkup:
    period_row = [
        InlineKeyboardButton(("• " if d == days else "") + label,
                             callback_data=f"rep_g_{group_row_id}_{d}")
        for d, label in reports.PERIODS.items()
    ]
    return InlineKeyboardMarkup([
        period_row,
        [InlineKeyboardButton("📄 Подробный PDF", callback_data=f"rep_pdf_{group_row_id}_{days}")],
        [InlineKeyboardButton("⬅️ К списку групп", callback_data="rep_list")],
    ])


async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    telegram_id = update.effective_user.id
    loop = asyncio.get_running_loop()

    if data == "rep_list":
        await query.answer()
        await query.edit_message_text("Выбери группу для отчёта:", reply_markup=report_list_kb(telegram_id))
        return

    if data.startswith("rep_g_"):
        _, _, gid, days = data.split("_")
        gid, days = int(gid), int(days)
        group = db.get_group(gid)
        if not group or group["telegram_id"] != telegram_id:
            await query.answer("Группа не найдена", show_alert=True)
            return
        await query.answer("Считаю…")
        token = db.get_vk_token(telegram_id)
        text = await loop.run_in_executor(None, reports.build_text_report, group, days, token)
        await query.edit_message_text(text, reply_markup=report_kb(gid, days))
        # График — отдельным сообщением-картинкой.
        try:
            png = await loop.run_in_executor(None, reports.growth_chart_png, group, days)
            bio = BytesIO(png)
            bio.name = "growth.png"
            await query.message.reply_photo(photo=bio)
        except Exception:
            logger.exception("Не удалось построить график")
        return

    if data.startswith("rep_pdf_"):
        _, _, gid, days = data.split("_")
        gid, days = int(gid), int(days)
        group = db.get_group(gid)
        if not group or group["telegram_id"] != telegram_id:
            await query.answer("Группа не найдена", show_alert=True)
            return
        await query.answer("Готовлю PDF…")
        token = db.get_vk_token(telegram_id)
        try:
            pdf = await loop.run_in_executor(None, reports.build_group_pdf, group, days, token)
            bio = BytesIO(pdf)
            bio.name = f"report_{group['vk_group_id']}_{days}d.pdf"
            await query.message.reply_document(document=bio, filename=bio.name,
                                               caption=f"📄 Подробный отчёт: {group['name']}")
        except Exception:
            logger.exception("Не удалось построить PDF")
            await query.message.reply_text("❌ Не удалось сформировать PDF. Загляни в /errors.")
        return

    await query.answer()


# ─── Сравнение групп ──────────────────────────────────────────────────────────

def compare_kb(telegram_id: int, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for g in db.get_groups(telegram_id):
        mark = "☑️" if g["id"] in selected else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {g['name']}", callback_data=f"cmp_t_{g['id']}")])
    if not rows:
        rows = [[InlineKeyboardButton("Сначала добавь группы 👥", callback_data="noop")]]
    period_row = [InlineKeyboardButton(label, callback_data=f"cmp_go_{d}")
                  for d, label in reports.PERIODS.items()]
    rows.append(period_row)
    return InlineKeyboardMarkup(rows)


async def compare_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    telegram_id = update.effective_user.id
    selected: set = context.user_data.setdefault("cmp_selected", set())
    loop = asyncio.get_running_loop()

    if data.startswith("cmp_t_"):
        gid = int(data.rsplit("_", 1)[1])
        selected.symmetric_difference_update({gid})  # toggle
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=compare_kb(telegram_id, selected))
        return

    if data.startswith("cmp_go_"):
        days = int(data.rsplit("_", 1)[1])
        groups = [db.get_group(gid) for gid in selected]
        groups = [g for g in groups if g and g["telegram_id"] == telegram_id]
        if len(groups) < 2:
            await query.answer("Отметь минимум 2 группы", show_alert=True)
            return
        await query.answer("Строю сравнение…")
        text = await loop.run_in_executor(None, reports.build_comparison_text, groups, days)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "📄 Подробный PDF", callback_data=f"cmp_pdf_{days}")]])
        await query.edit_message_text(text, reply_markup=kb)
        try:
            png = await loop.run_in_executor(None, reports.comparison_chart_png, groups, days)
            bio = BytesIO(png)
            bio.name = "compare.png"
            await query.message.reply_photo(photo=bio)
        except Exception:
            logger.exception("Не удалось построить график сравнения")
        return

    if data.startswith("cmp_pdf_"):
        days = int(data.rsplit("_", 1)[1])
        groups = [db.get_group(gid) for gid in selected]
        groups = [g for g in groups if g and g["telegram_id"] == telegram_id]
        if len(groups) < 2:
            await query.answer("Выбор сброшен, отметь группы заново", show_alert=True)
            return
        await query.answer("Готовлю PDF…")
        try:
            pdf = await loop.run_in_executor(None, reports.build_comparison_pdf, groups, days)
            bio = BytesIO(pdf)
            bio.name = f"compare_{days}d.pdf"
            await query.message.reply_document(document=bio, filename=bio.name,
                                               caption="📄 Сравнение групп")
        except Exception:
            logger.exception("Не удалось построить PDF сравнения")
            await query.message.reply_text("❌ Не удалось сформировать PDF. Загляни в /errors.")
        return

    await query.answer()


# ─── Дайджест ─────────────────────────────────────────────────────────────────

def digest_kb(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "⛔ Выключить" if enabled else "✅ Включить",
        callback_data="dig_off" if enabled else "dig_on",
    )]])


async def show_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    enabled = db.get_digest_enabled(update.effective_user.id)
    when = f"{scheduler.DIGEST_HOUR}:{int(scheduler.DIGEST_MINUTE or 0):02d}"
    status = "включён 🟢" if enabled else "выключен ⚪"
    await update.message.reply_text(
        f"📅 Ежедневный дайджест: {status}\n"
        f"Присылаю сводку по подписчикам каждое утро в {when} МСК.",
        reply_markup=digest_kb(enabled),
    )


async def digest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    enabled = query.data == "dig_on"
    db.set_digest_enabled(update.effective_user.id, enabled)
    await query.answer("Готово")
    when = f"{scheduler.DIGEST_HOUR}:{int(scheduler.DIGEST_MINUTE or 0):02d}"
    status = "включён 🟢" if enabled else "выключен ⚪"
    await query.edit_message_text(
        f"📅 Ежедневный дайджест: {status}\n"
        f"Присылаю сводку по подписчикам каждое утро в {when} МСК.",
        reply_markup=digest_kb(enabled),
    )


# ─── Ошибки / админ-панель ────────────────────────────────────────────────────

def _is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def _fmt_ts(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")


def _err_btn_label(e) -> str:
    ts = datetime.fromtimestamp(e["created_at"], tz=MOSCOW_TZ).strftime("%d.%m %H:%M")
    code = f"VK{e['error_code']}" if e["error_code"] is not None else (e["stage"] or "ошибка")
    return f"{ts} · {code}"[:60]


def _kb(rows):
    return InlineKeyboardMarkup(rows) if rows else None


def _pager_rows(page: int, total: int, prefix: str):
    pages = (total + ERRORS_PAGE_SIZE - 1) // ERRORS_PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}_{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}_{page + 1}"))
    return [nav] if nav else []


def _own_list_view(telegram_id: int, page: int):
    total = db.count_errors(telegram_id)
    errors = db.get_errors(telegram_id, ERRORS_PAGE_SIZE, page * ERRORS_PAGE_SIZE)
    rows = [[InlineKeyboardButton(_err_btn_label(e), callback_data=f"err_v_{e['id']}")] for e in errors]
    rows += _pager_rows(page, total, "err_self")
    return f"📋 Твои ошибки: {total}", _kb(rows)


def _admin_users_view(page: int):
    total = db.count_users_with_errors()
    if not total:
        return "🛠 Админ-панель ошибок\n\n✅ Ошибок пока нет.", None
    users = db.get_users_with_errors(ERRORS_PAGE_SIZE, page * ERRORS_PAGE_SIZE)
    rows = [[InlineKeyboardButton(
        f"👤 {u['telegram_id']} · {u['cnt']} ошиб. · {_fmt_ts(u['last_at'])}",
        callback_data=f"err_u_{u['telegram_id']}_0")] for u in users]
    rows += _pager_rows(page, total, "err_au")
    return f"🛠 Админ-панель ошибок\nПользователей с ошибками: {total}", _kb(rows)


def _admin_user_errors_view(target_id: int, page: int):
    total = db.count_errors(target_id)
    errors = db.get_errors(target_id, ERRORS_PAGE_SIZE, page * ERRORS_PAGE_SIZE)
    rows = [[InlineKeyboardButton(_err_btn_label(e), callback_data=f"err_v_{e['id']}")] for e in errors]
    rows += _pager_rows(page, total, f"err_u_{target_id}")
    rows.append([InlineKeyboardButton("⬅️ К пользователям", callback_data="err_au_0")])
    return f"👤 Пользователь {target_id}\nОшибок: {total}", InlineKeyboardMarkup(rows)


def _detail_view(e, viewer_is_admin: bool):
    grp = e["vk_group_name"] or "—"
    if e["vk_group_id"]:
        grp += f" (id {e['vk_group_id']})"
    code = f"VK {e['error_code']}" if e["error_code"] is not None else "—"
    text = (
        f"🆔 Ошибка #{e['id']}\n🕒 {_fmt_ts(e['created_at'])} МСК\n"
        f"📍 Этап: {e['stage'] or '—'}\n"
        f"👥 Группа: {grp}\n🔢 Код: {code}\n\n💬 {e['message'] or '—'}"
    )
    tb = e["traceback"]
    if tb:
        budget = 3500 - len(text)
        if budget > 200:
            snippet = tb if len(tb) <= budget else "…(обрезано)…\n" + tb[-budget:]
            text += f"\n\n🧩 Traceback:\n{snippet}"
    if len(text) > 4096:
        text = text[:4000] + "\n…(обрезано)"
    rows = [[InlineKeyboardButton("📄 Полный traceback файлом", callback_data=f"err_tb_{e['id']}")]]
    rows.append([InlineKeyboardButton(
        "⬅️ Назад", callback_data=f"err_u_{e['telegram_id']}_0" if viewer_is_admin else "err_self_0")])
    return text, InlineKeyboardMarkup(rows)


async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db.ensure_user(uid)
    if _is_admin(uid):
        text, kb = _admin_users_view(0)
        await update.message.reply_text(text, reply_markup=kb)
        return
    if db.count_errors(uid) == 0:
        await update.message.reply_text("✅ У тебя нет залогированных ошибок.")
        return
    text, kb = _own_list_view(uid, 0)
    await update.message.reply_text(text, reply_markup=kb)


async def errors_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = update.effective_user.id
    is_admin = _is_admin(uid)
    parts = query.data.split("_")
    kind = parts[1]

    if kind == "self":
        await query.answer()
        text, kb = _own_list_view(uid, int(parts[2]))
        await query.edit_message_text(text, reply_markup=kb)
    elif kind == "au":
        if not is_admin:
            await query.answer("Недостаточно прав", show_alert=True); return
        await query.answer()
        text, kb = _admin_users_view(int(parts[2]))
        await query.edit_message_text(text, reply_markup=kb)
    elif kind == "u":
        if not is_admin:
            await query.answer("Недостаточно прав", show_alert=True); return
        await query.answer()
        text, kb = _admin_user_errors_view(int(parts[2]), int(parts[3]))
        await query.edit_message_text(text, reply_markup=kb)
    elif kind == "v":
        e = db.get_error(int(parts[2]))
        if not e:
            await query.answer("Не найдено", show_alert=True); return
        if not is_admin and e["telegram_id"] != uid:
            await query.answer("Недостаточно прав", show_alert=True); return
        await query.answer()
        text, kb = _detail_view(e, is_admin)
        await query.edit_message_text(text, reply_markup=kb)
    elif kind == "tb":
        e = db.get_error(int(parts[2]))
        if not e:
            await query.answer("Не найдено", show_alert=True); return
        if not is_admin and e["telegram_id"] != uid:
            await query.answer("Недостаточно прав", show_alert=True); return
        await query.answer()
        content = e["traceback"] or e["message"] or "—"
        bio = BytesIO(content.encode("utf-8"))
        bio.name = f"error_{e['id']}.txt"
        await query.message.reply_document(document=bio, filename=f"error_{e['id']}.txt")


async def _cleanup_errors_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    loop = asyncio.get_running_loop()
    deleted = await loop.run_in_executor(None, db.cleanup_old_errors, ERROR_RETENTION_DAYS)
    if deleted:
        logger.info("Очистка логов ошибок: удалено %s записей", deleted)


# ─── /cancel ──────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ─── post_init ────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    await scheduler.collect_now(app)


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("Не задан TELEGRAM_TOKEN в .env")

    db.init_db()

    persistence = PicklePersistence(filepath=os.path.join(db.DATA_DIR, "bot_state.pickle"))
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .post_init(_post_init)
        .concurrent_updates(True)
        .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0).pool_timeout(30.0)
        .get_updates_connect_timeout(30.0).get_updates_read_timeout(30.0)
        .build()
    )

    token_conv = ConversationHandler(
        entry_points=[
            CommandHandler("settoken", cmd_settoken),
            CallbackQueryHandler(settoken_from_button, pattern=r"^settoken_change$"),
        ],
        states={TOKEN_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=300, name="token_conv", persistent=False, allow_reentry=True,
    )

    groups_conv = ConversationHandler(
        entry_points=[
            CommandHandler("groups", cmd_groups),
            CallbackQueryHandler(groups_button, pattern=r"^(g_add|g_del_|g_rename_|noop$)"),
        ],
        states={
            G_ADD_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, groups_add_id)],
            G_ADD_CONFIRM: [CallbackQueryHandler(groups_add_confirm, pattern=r"^g_(confirmname|manualname)$")],
            G_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, groups_add_name)],
            G_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, groups_rename)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=300, name="groups_conv", persistent=False, allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", lambda u, c: u.message.reply_text(
        "Выбери группу для отчёта:", reply_markup=report_list_kb(u.effective_user.id))))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("admin", cmd_errors))
    app.add_handler(CallbackQueryHandler(errors_callback, pattern=r"^err_"))
    # Кнопки меню — до диалогов.
    app.add_handler(MessageHandler(filters.Text(MENU_BUTTON_TEXTS), main_menu_button))
    app.add_handler(CallbackQueryHandler(handle_token_delete, pattern=r"^settoken_delete$"))
    app.add_handler(token_conv)
    app.add_handler(groups_conv)
    # Отчёты / сравнение / дайджест (без ввода текста).
    app.add_handler(CallbackQueryHandler(report_callback, pattern=r"^rep_"))
    app.add_handler(CallbackQueryHandler(compare_callback, pattern=r"^cmp_"))
    app.add_handler(CallbackQueryHandler(digest_callback, pattern=r"^dig_"))

    scheduler.register_jobs(app)
    app.job_queue.run_repeating(
        _cleanup_errors_job, interval=timedelta(days=ERROR_CLEANUP_INTERVAL_DAYS),
        first=timedelta(minutes=1), name="cleanup_errors",
    )

    logger.info("Δ Delta — сервис статистики запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
