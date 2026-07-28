"""SQLite-хранилище сервиса статистики VK-сообществ.

Модель данных:

    users        — телеграм-пользователь и его VK-токен (шифруется)
    vk_groups    — отслеживаемые VK-группы пользователя
    snapshots    — снимки числа подписчиков во времени (по ним строим динамику;
                   VK не отдаёт историю members_count, поэтому копим сами)
    settings     — настройки пользователя (например, время ежедневного дайджеста)
    error_logs   — журнал ошибок для /errors и админ-панели
"""

import os
import re
import sqlite3
import time

import crypto

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "stats.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                vk_token    TEXT
            );

            CREATE TABLE IF NOT EXISTS vk_groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                vk_group_id INTEGER NOT NULL,
                name        TEXT NOT NULL,
                added_at    INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                UNIQUE (telegram_id, vk_group_id)
            );

            -- Снимок числа подписчиков группы в момент ts. По этим точкам строим
            -- график роста/оттока и приросты за период.
            CREATE TABLE IF NOT EXISTS snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                group_row_id  INTEGER NOT NULL,   -- ссылается на vk_groups.id
                ts            INTEGER NOT NULL,    -- unixtime снятия
                members_count INTEGER NOT NULL,
                FOREIGN KEY (group_row_id) REFERENCES vk_groups(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_snap_group_ts
                ON snapshots (group_row_id, ts);

            CREATE TABLE IF NOT EXISTS settings (
                telegram_id   INTEGER PRIMARY KEY,
                digest_enabled INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                created_at    INTEGER NOT NULL,
                stage         TEXT,
                vk_group_id   INTEGER,
                vk_group_name TEXT,
                error_code    INTEGER,
                message       TEXT,
                traceback     TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_error_logs_user
                ON error_logs (telegram_id, created_at DESC);
            """
        )

        # Одноразовая миграция: зашифровать уже хранящиеся открытым текстом токены.
        # Идемпотентно: encrypt() не трогает уже зашифрованные (префикс enc:).
        for row in conn.execute(
            "SELECT telegram_id, vk_token FROM users WHERE vk_token IS NOT NULL"
        ).fetchall():
            if not crypto.is_encrypted(row["vk_token"]):
                enc = crypto.encrypt(row["vk_token"])
                if enc != row["vk_token"]:
                    conn.execute(
                        "UPDATE users SET vk_token = ? WHERE telegram_id = ?",
                        (enc, row["telegram_id"]),
                    )


# ─── Пользователи ─────────────────────────────────────────────────────────────

def ensure_user(telegram_id: int) -> None:
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))


def get_vk_token(telegram_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT vk_token FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    if not row:
        return None
    return crypto.decrypt(row["vk_token"])


def set_vk_token(telegram_id: int, token: str) -> None:
    stored = crypto.encrypt(token)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, vk_token) VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET vk_token = excluded.vk_token
            """,
            (telegram_id, stored),
        )


def clear_vk_token(telegram_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET vk_token = NULL WHERE telegram_id = ?", (telegram_id,))


def get_all_users() -> list[int]:
    with _connect() as conn:
        rows = conn.execute("SELECT telegram_id FROM users").fetchall()
    return [r["telegram_id"] for r in rows]


# ─── Группы VK ────────────────────────────────────────────────────────────────

def get_groups(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM vk_groups WHERE telegram_id = ? ORDER BY id", (telegram_id,)
        ).fetchall()


def get_group(group_row_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM vk_groups WHERE id = ?", (group_row_id,)).fetchone()


def get_all_groups() -> list[sqlite3.Row]:
    """Все группы всех пользователей (для планового сбора снимков)."""
    with _connect() as conn:
        return conn.execute("SELECT * FROM vk_groups ORDER BY telegram_id, id").fetchall()


def add_group(telegram_id: int, vk_group_id: int, name: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO vk_groups (telegram_id, vk_group_id, name, added_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id, vk_group_id) DO UPDATE SET name = excluded.name
            """,
            (telegram_id, vk_group_id, name, int(time.time())),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM vk_groups WHERE telegram_id = ? AND vk_group_id = ?",
            (telegram_id, vk_group_id),
        ).fetchone()
        return row["id"]


def rename_group(group_row_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE vk_groups SET name = ? WHERE id = ?", (name, group_row_id))


def delete_group(group_row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM vk_groups WHERE id = ?", (group_row_id,))


# ─── Снимки подписчиков ───────────────────────────────────────────────────────

def add_snapshot(group_row_id: int, members_count: int, ts: int | None = None) -> None:
    """Записать снимок. Пропускает дубль, если последнее значение не изменилось
    (нет смысла копить одинаковые точки — график всё равно строится по изменениям)."""
    ts = ts if ts is not None else int(time.time())
    with _connect() as conn:
        last = conn.execute(
            "SELECT members_count FROM snapshots WHERE group_row_id = ? "
            "ORDER BY ts DESC LIMIT 1",
            (group_row_id,),
        ).fetchone()
        if last and last["members_count"] == members_count:
            return
        conn.execute(
            "INSERT INTO snapshots (group_row_id, ts, members_count) VALUES (?, ?, ?)",
            (group_row_id, ts, members_count),
        )


def get_snapshots(group_row_id: int, since_ts: int | None = None) -> list[sqlite3.Row]:
    """Снимки группы по возрастанию времени (опционально начиная с since_ts)."""
    with _connect() as conn:
        if since_ts is None:
            return conn.execute(
                "SELECT ts, members_count FROM snapshots WHERE group_row_id = ? ORDER BY ts",
                (group_row_id,),
            ).fetchall()
        return conn.execute(
            "SELECT ts, members_count FROM snapshots "
            "WHERE group_row_id = ? AND ts >= ? ORDER BY ts",
            (group_row_id, since_ts),
        ).fetchall()


def get_latest_snapshot(group_row_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT ts, members_count FROM snapshots WHERE group_row_id = ? "
            "ORDER BY ts DESC LIMIT 1",
            (group_row_id,),
        ).fetchone()


def get_snapshot_before(group_row_id: int, ts: int) -> sqlite3.Row | None:
    """Ближайший снимок ДО момента ts (для вычисления прироста за период)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT ts, members_count FROM snapshots WHERE group_row_id = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (group_row_id, ts),
        ).fetchone()


# ─── Настройки (дайджест) ─────────────────────────────────────────────────────

def get_digest_enabled(telegram_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT digest_enabled FROM settings WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return bool(row and row["digest_enabled"])


def set_digest_enabled(telegram_id: int, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (telegram_id, digest_enabled) VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET digest_enabled = excluded.digest_enabled
            """,
            (telegram_id, 1 if enabled else 0),
        )


def get_digest_users() -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT telegram_id FROM settings WHERE digest_enabled = 1"
        ).fetchall()
    return [r["telegram_id"] for r in rows]


# ─── Логи ошибок ──────────────────────────────────────────────────────────────

# Маскировка VK-токенов перед записью (в тексте ошибки/URL может проскочить токен).
_TOKEN_PATTERNS = [
    (re.compile(r"access_token=[^&\s\"'}]+"), "access_token=***"),
    (re.compile(r"vk1\.a\.[A-Za-z0-9._\-]+"), "vk1.a.***"),
]


def _sanitize(text: str | None) -> str | None:
    if not text:
        return text
    for pattern, repl in _TOKEN_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def log_error(
    telegram_id: int,
    *,
    stage: str | None = None,
    vk_group_id: int | None = None,
    vk_group_name: str | None = None,
    error_code: int | None = None,
    message: str | None = None,
    traceback: str | None = None,
) -> None:
    """Best-effort: при сбое БД не роняет обработку."""
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO error_logs
                    (telegram_id, created_at, stage, vk_group_id, vk_group_name,
                     error_code, message, traceback)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_id, int(time.time()), stage, vk_group_id, vk_group_name,
                    error_code, _sanitize(message), _sanitize(traceback),
                ),
            )
    except Exception:
        pass


def get_errors(telegram_id: int, limit: int = 8, offset: int = 0) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT * FROM error_logs WHERE telegram_id = ?
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            (telegram_id, limit, offset),
        ).fetchall()


def count_errors(telegram_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM error_logs WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row["c"] if row else 0


def get_error(error_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM error_logs WHERE id = ?", (error_id,)).fetchone()


def get_users_with_errors(limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT telegram_id, COUNT(*) AS cnt, MAX(created_at) AS last_at
            FROM error_logs
            GROUP BY telegram_id
            ORDER BY last_at DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


def count_users_with_errors() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT telegram_id) AS c FROM error_logs"
        ).fetchone()
    return row["c"] if row else 0


def cleanup_old_errors(days: int) -> int:
    cutoff = int(time.time()) - days * 86400
    with _connect() as conn:
        cur = conn.execute("DELETE FROM error_logs WHERE created_at < ?", (cutoff,))
        return cur.rowcount
