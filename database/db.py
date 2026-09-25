"""Соединение с SQLite через aiosqlite.

Особенности реализации:
    * одно долгоживущее соединение в режиме WAL (хорошо для конкурентности);
    * каждая операция защищена :class:`asyncio.Lock`, поэтому запросы
      не перемешиваются между корутинами;
    * при ошибках БД выполняется до 3 автоматических попыток с задержкой,
      при обрыве соединения выполняется переподключение;
    * схема создаётся при старте бота через ``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import aiosqlite

logger = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    """Исключение слоя базы данных после исчерпания всех попыток."""


# ---------------------------------------------------------------------------
# Схема базы данных
# ---------------------------------------------------------------------------
SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS users (
    user_id             INTEGER PRIMARY KEY,
    username            TEXT,
    first_name          TEXT,
    reputation          INTEGER NOT NULL DEFAULT 0,
    total_messages      INTEGER NOT NULL DEFAULT 0,
    is_globally_banned  INTEGER NOT NULL DEFAULT 0,
    is_spammer          INTEGER NOT NULL DEFAULT 0,
    ban_marks           TEXT    NOT NULL DEFAULT '[]',
    created_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id             INTEGER PRIMARY KEY,
    title               TEXT,
    owner_id            INTEGER,
    owner_channel_id    INTEGER,
    settings            TEXT    NOT NULL DEFAULT '{}',
    members_count       INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_users (
    chat_id         INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    messages_count  INTEGER NOT NULL DEFAULT 0,
    warns_count     INTEGER NOT NULL DEFAULT 0,
    is_banned       INTEGER NOT NULL DEFAULT 0,
    is_muted        INTEGER NOT NULL DEFAULT 0,
    mute_until      TEXT,
    ban_until       TEXT,
    is_member       INTEGER NOT NULL DEFAULT 1,
    is_raid_suspect INTEGER NOT NULL DEFAULT 0,
    joined_at       TEXT    NOT NULL,
    last_join_at    TEXT,
    last_message_at TEXT,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS warns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    reason      TEXT,
    issued_by   INTEGER,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS punishments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    type             TEXT    NOT NULL,
    reason           TEXT,
    duration         TEXT,
    duration_seconds INTEGER,
    issued_by        INTEGER,
    created_at       TEXT    NOT NULL,
    expires_at       TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS message_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS complaints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    user_username   TEXT,
    user_first_name TEXT,
    reason          TEXT    NOT NULL,
    description     TEXT    NOT NULL,
    photo_file_id   TEXT,
    status          TEXT    NOT NULL DEFAULT 'open',
    admin_response  TEXT,
    created_at      TEXT    NOT NULL,
    closed_at       TEXT,
    closed_by       INTEGER
);

CREATE TABLE IF NOT EXISTS admin_bans (
    user_id   INTEGER PRIMARY KEY,
    reason    TEXT,
    banned_at TEXT    NOT NULL,
    banned_by INTEGER
);

CREATE TABLE IF NOT EXISTS admin_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT    NOT NULL,
    target_id  INTEGER,
    details    TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_users_user      ON chat_users (user_id);
CREATE INDEX IF NOT EXISTS idx_chat_users_chat      ON chat_users (chat_id);
CREATE INDEX IF NOT EXISTS idx_warns_chat_user      ON warns (chat_id, user_id);
CREATE INDEX IF NOT EXISTS idx_warns_user           ON warns (user_id);
CREATE INDEX IF NOT EXISTS idx_punishments_chat     ON punishments (chat_id, user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_punishments_expires  ON punishments (is_active, expires_at);
CREATE INDEX IF NOT EXISTS idx_message_log_chat     ON message_log (chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_complaints_status    ON complaints (status, created_at);
CREATE INDEX IF NOT EXISTS idx_complaints_user      ON complaints (user_id, status);
CREATE INDEX IF NOT EXISTS idx_admin_log_created    ON admin_log (created_at);
"""

#: Миграции для баз, созданных прошлыми версиями бота:
#: ``(таблица, колонка, определение)``. Применяются, если колонки ещё нет.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("users", "is_spammer", "INTEGER NOT NULL DEFAULT 0"),
    ("chat_users", "is_raid_suspect", "INTEGER NOT NULL DEFAULT 0"),
)

#: Ключи настроек чата, оставшиеся от удалённых режимов (PR).
#: Отдельной таблицы под них не создавалось — чистим json-поле ``chats.settings``.
LEGACY_SETTINGS_KEYS: tuple[str, ...] = (
    "pr_enabled",
    "pr_resources",
    "pr_welcome_text",
    "pr_welcome_entities",
    "pr_welcome_photo",
    "pr_mode",
)


class Database:
    """Асинхронная обёртка над одним соединением SQLite."""

    def __init__(
        self,
        path: Path,
        *,
        retries: int = 3,
        retry_delay: float = 0.5,
        timeout: float = 30.0,
    ) -> None:
        """Создать объект базы данных.

        :param path: путь к файлу SQLite.
        :param retries: количество попыток выполнения одного запроса.
        :param retry_delay: базовая задержка между попытками (в секундах).
        :param timeout: таймаут ожидания блокировки SQLite.
        """
        self._path: Path = Path(path)
        self._retries: int = max(1, int(retries))
        self._retry_delay: float = max(0.0, float(retry_delay))
        self._timeout: float = max(1.0, float(timeout))
        self._connection: Optional[aiosqlite.Connection] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Свойства
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        """Путь к файлу базы данных."""
        return self._path

    @property
    def is_connected(self) -> bool:
        """Открыто ли соединение с базой."""
        return self._connection is not None

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Открыть соединение, включить WAL и подготовить прагмы."""
        async with self._lock:
            await self._open_locked()
        logger.info("База данных открыта: %s", self._path)

    async def _open_locked(self) -> aiosqlite.Connection:
        """Открыть соединение (вызывается под уже взятым локом)."""
        if self._connection is not None:
            return self._connection
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(str(self._path), timeout=self._timeout)
        connection.row_factory = aiosqlite.Row
        # Прагмы: WAL для параллельного чтения, busy_timeout против «database is locked».
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
        await connection.commit()
        self._connection = connection
        return connection

    async def close(self) -> None:
        """Аккуратно закрыть соединение (используется при выключении)."""
        async with self._lock:
            connection = self._connection
            self._connection = None
            if connection is None:
                return
            try:
                await connection.commit()
            except (sqlite3.Error, OSError) as exc:
                logger.error("Не удалось закоммитить при закрытии БД: %s", exc)
            try:
                await connection.close()
            except (sqlite3.Error, OSError) as exc:
                logger.error("Не удалось закрыть БД: %s", exc)
        logger.info("Соединение с базой данных закрыто.")

    async def init_schema(self) -> None:
        """Создать таблицы, индексы и дозаполнить колонки старых баз."""
        await self.executescript(SCHEMA_SQL)
        await self._apply_migrations()
        await self._cleanup_legacy_settings()
        logger.info("Схема базы данных проверена и готова к работе.")

    async def _apply_migrations(self) -> None:
        """Добавить колонки, которых нет в базах прежних версий бота."""
        columns: dict[str, set[str]] = {}
        for table, column, definition in MIGRATIONS:
            if table not in columns:
                columns[table] = await self._table_columns(table)
            if column in columns[table]:
                continue
            try:
                await self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except DatabaseError as exc:
                logger.error(
                    "Не удалось добавить колонку %s.%s: %s", table, column, exc
                )
                continue
            columns[table].add(column)
            logger.info("Миграция применена: %s.%s", table, column)

    async def _cleanup_legacy_settings(self) -> None:
        """Убрать из настроек чатов ключи уже удалённых режимов.

        PR режим жил в json-поле ``chats.settings``, отдельной таблицы под
        него не было, поэтому старые ключи (``pr_enabled``, ``pr_resources``,
        ``pr_welcome_text`` и связанные с ними) удаляются точечно. Ошибки
        чистки не мешают работе бота — они только логируются.
        """
        try:
            rows = await self.fetch_all("SELECT chat_id, settings FROM chats")
        except DatabaseError as exc:
            logger.error("Не удалось прочитать настройки чатов для чистки: %s", exc)
            return

        for row in rows:
            raw = row["settings"]
            if not raw:
                continue
            try:
                settings = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(settings, dict):
                continue

            removed = [key for key in LEGACY_SETTINGS_KEYS if key in settings]
            if not removed:
                continue
            for key in removed:
                settings.pop(key, None)

            try:
                await self.execute(
                    "UPDATE chats SET settings = ? WHERE chat_id = ?",
                    (json.dumps(settings, ensure_ascii=False), int(row["chat_id"])),
                )
            except DatabaseError as exc:
                logger.error(
                    "Не удалось почистить настройки чата %s: %s", row["chat_id"], exc
                )
                continue
            logger.info(
                "Из настроек чата %s удалены устаревшие ключи: %s",
                row["chat_id"],
                ", ".join(removed),
            )

    async def _table_columns(self, table: str) -> set[str]:
        """Список колонок таблицы (для проверки миграций).

        :param table: имя таблицы (значение из констант модуля).
        """
        rows = await self.fetch_all(f"PRAGMA table_info({table})")
        return {str(row["name"]) for row in rows}

    # ------------------------------------------------------------------
    # Низкоуровневые операции с retry
    # ------------------------------------------------------------------
    async def _run_with_retry(self, operation: str, action: Any, *args: Any) -> Any:
        """Выполнить действие с повторами при ошибках БД.

        :param operation: описание операции для логов.
        :param action: корутина-фабрика, принимающая соединение.
        :param args: аргументы фабрики.
        :returns: результат фабрики.
        :raises DatabaseError: если все попытки исчерпаны.
        """
        last_error: Optional[BaseException] = None
        for attempt in range(1, self._retries + 1):
            try:
                async with self._lock:
                    connection = await self._open_locked()
                    return await action(connection, *args)
            except (sqlite3.Error, aiosqlite.Error, OSError) as exc:
                last_error = exc
                logger.error(
                    "Ошибка БД при операции «%s» (попытка %d/%d): %s",
                    operation,
                    attempt,
                    self._retries,
                    exc,
                    exc_info=True,
                )
                await self._drop_connection_locked()
                if attempt < self._retries:
                    await asyncio.sleep(self._retry_delay * attempt)
        raise DatabaseError(f"Операция «{operation}» не удалась: {last_error}") from last_error

    async def _drop_connection_locked(self) -> None:
        """Закрыть «сломанное» соединение, чтобы следующая попытка переоткрыла его."""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            await connection.close()
        except (sqlite3.Error, OSError):
            logger.debug("Соединение с БД закрыто после ошибки.", exc_info=True)

    # ------------------------------------------------------------------
    # Запросы
    # ------------------------------------------------------------------
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Выполнить запрос без возврата строк (INSERT/UPDATE/DELETE)."""

        async def action(connection: aiosqlite.Connection) -> None:
            await connection.execute(sql, params)
            await connection.commit()

        await self._run_with_retry("execute", action)

    async def execute_many(self, sql: str, params: Iterable[Sequence[Any]] = ()) -> None:
        """Выполнить запрос пакетно (executemany)."""
        rows = [tuple(row) for row in params]

        async def action(connection: aiosqlite.Connection) -> None:
            await connection.executemany(sql, rows)
            await connection.commit()

        await self._run_with_retry("execute_many", action)

    async def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Выполнить INSERT и вернуть ``lastrowid``."""

        async def action(connection: aiosqlite.Connection) -> int:
            cursor = await connection.execute(sql, params)
            await connection.commit()
            return int(cursor.lastrowid or 0)

        return int(await self._run_with_retry("insert", action))

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[aiosqlite.Row]:
        """Получить одну строку результата или ``None``."""

        async def action(connection: aiosqlite.Connection) -> Optional[aiosqlite.Row]:
            cursor = await connection.execute(sql, params)
            row = await cursor.fetchone()
            await cursor.close()
            return row

        return await self._run_with_retry("fetch_one", action)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        """Получить все строки результата."""

        async def action(connection: aiosqlite.Connection) -> list[aiosqlite.Row]:
            cursor = await connection.execute(sql, params)
            rows = await cursor.fetchall()
            await cursor.close()
            return list(rows)

        return await self._run_with_retry("fetch_all", action)

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
        """Получить скалярное значение первого столбца первой строки."""
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    async def executescript(self, script: str) -> None:
        """Выполнить SQL-скрипт целиком (используется для создания схемы)."""

        async def action(connection: aiosqlite.Connection) -> None:
            await connection.executescript(script)
            await connection.commit()

        await self._run_with_retry("executescript", action)

