"""Слой работы с базой данных SQLite (aiosqlite).

Модули пакета:
    * :mod:`yamochan.database.models` — dataclass-модели таблиц;
    * :mod:`yamochan.database.db` — соединение, создание таблиц, retry-логика;
    * :mod:`yamochan.database.queries` — все CRUD-операции проекта.
"""

from __future__ import annotations

__all__ = ["db", "models", "queries"]
