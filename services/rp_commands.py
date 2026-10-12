"""Реестр пользовательских РП-команд владельца чата.

Встроенные РП-команды живут в :mod:`handlers.roleplay` и не меняются. А вот
свои команды владелец может добавлять в каждом чате. Чтобы фильтр РП-команд
(который не имеет доступа к базе) мог их распознать, список хранится в
памяти процесса и обновляется при правке и на старте бота.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)


class RpCommandStore:
    """Кэш пользовательских РП-команд: ``chat_id → {имя: запись}``."""

    def __init__(self) -> None:
        """Создать пустой реестр."""
        self._commands: dict[int, dict[str, dict[str, Any]]] = {}

    def set_all(self, chat_id: int, raw: Iterable[Mapping[str, Any]]) -> None:
        """Заменить список команд одного чата.

        :param chat_id: идентификатор чата.
        :param raw: записи ``{"name", "emoji", "action", "nsfw"}``.
        """
        mapping: dict[str, dict[str, Any]] = {}
        for item in raw:
            name = str(item.get("name") or "").strip().lower()
            action = str(item.get("action") or "").strip()
            if not name or not action:
                continue
            mapping[name] = {
                "name": name,
                "emoji": str(item.get("emoji") or ""),
                "action": action,
                "nsfw": bool(item.get("nsfw")),
            }
        if mapping:
            self._commands[int(chat_id)] = mapping
        else:
            self._commands.pop(int(chat_id), None)

    def all_for(self, chat_id: int) -> dict[str, dict[str, Any]]:
        """Все команды чата (копия не делается — только чтение)."""
        return self._commands.get(int(chat_id), {})

    def get(self, chat_id: int, name: str) -> Optional[dict[str, Any]]:
        """Найти одну команду чата по имени."""
        return self._commands.get(int(chat_id), {}).get(str(name).strip().lower())

    def has(self, chat_id: int, name: str) -> bool:
        """Есть ли такая команда у чата."""
        return self.get(chat_id, name) is not None

    def names(self, chat_id: int) -> set[str]:
        """Множество имён команд чата."""
        return set(self._commands.get(int(chat_id), {}))

    def count(self, chat_id: int) -> int:
        """Сколько своих команд у чата."""
        return len(self._commands.get(int(chat_id), {}))

    def clear(self) -> None:
        """Очистить реестр целиком (например, перед перезагрузкой)."""
        self._commands.clear()


rp_command_store: Final[RpCommandStore] = RpCommandStore()
