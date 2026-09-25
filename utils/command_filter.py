"""Гибкое распознавание команд: ``.``, ``/``, ``!`` или без префикса.

Модуль отвечает за единое поведение команд бота:

    * :class:`FlexCommand` — фильтр aiogram: команда ловится с любым
      префиксом (``.``, ``/``, ``!``) и вовсе без него, поэтому «бан»
      реплаем работает так же, как «.бан»;
    * :class:`RPCommand` — фильтр только для ролевых команд: они требуют
      префикс (``.обнять``, ``/обнять``, ``!обнять``), иначе обычная речь
      вроде «я обнял её» срабатывала бы как команда;
    * :func:`split_command` — общий разбор текста на имя команды и аргументы;
    * :func:`is_command_message` — проверка «это команда, а не активность»
      для счётчиков сообщений и антиспама.

Все функции безопасны: пустой текст, подписи к фото и упоминание бота
(``.бан@YamoChanBot``) обрабатываются без ошибок.
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Optional

from aiogram.filters import BaseFilter
from aiogram.types import Message

import config

#: Префиксы, с которых может начинаться команда.
COMMAND_PREFIXES: Final[tuple[str, ...]] = (".", "/", "!")

#: Команды, которые и без префикса считаются командами, а не активностью.
KNOWN_COMMANDS: Final[frozenset[str]] = frozenset(
    set(config.MODERATION_COMMANDS)
    | set(config.CALL_COMMANDS)
    | set(config.ADMIN_COMMANDS)
    | {
        config.COMMAND_SYNC,
        config.COMMAND_RULES,
        config.COMMAND_GREETING,
        "rules",
        "greeting",
    }
)


def split_command(
    text: Optional[str],
    *,
    require_prefix: bool = False,
) -> Optional[tuple[str, str]]:
    """Разобрать текст на имя команды и аргументы.

    Префикс (``.``, ``/``, ``!``) необязателен: ``".бан 1д"``, ``"/бан"``,
    ``"!бан"`` и ``"бан"`` дают одно и то же имя команды. Упоминание бота
    (``@YamoChanBot``) от имени отбрасывается.

    :param text: текст или подпись сообщения.
    :param require_prefix: ``True`` — команда без префикса командой не
        считается (режим ролевых команд).
    :returns: пара ``(имя, аргументы)`` или ``None``, если это не команда.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    if raw[0] in COMMAND_PREFIXES:
        body = raw[1:].lstrip()
    elif require_prefix:
        return None
    else:
        body = raw

    if not body:
        return None

    head, _, tail = body.partition(" ")
    name = head.split("@", 1)[0].strip().lower()
    if not name:
        return None
    return name, tail.strip()


def is_command_message(text: Optional[str]) -> bool:
    """Похоже ли сообщение на команду бота, а не на обычную активность.

    Используется счётчиками и антиспамом: «бан» без префикса — это команда,
    а не сообщение, за которое стоит наказывать.

    :param text: текст или подпись сообщения.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    if raw[0] in COMMAND_PREFIXES:
        return True
    parsed = split_command(raw)
    return parsed is not None and parsed[0] in KNOWN_COMMANDS


class FlexCommand(BaseFilter):
    """Фильтр: команда с любым префиксом (``.``, ``/``, ``!``) или без него.

    ``FlexCommand("бан", "ban")`` поймает ``.бан``, ``/бан``, ``!бан``,
    ``бан``, ``.ban``, ``/ban``, ``!ban`` и ``ban``. В хендлер передаются
    аргументы после команды::

        @router.message(FlexCommand("бан", "ban"))
        async def cmd_ban(message: Message, command_args: str, ...) -> None:
            ...

    :param commands: имена команд (регистр не важен).
    """

    #: Префиксы команды.
    PREFIXES: Final[tuple[str, ...]] = COMMAND_PREFIXES

    def __init__(self, *commands: str) -> None:
        """Запомнить имена команд."""
        self.commands: tuple[str, ...] = tuple(
            command.strip().lower() for command in commands if command.strip()
        )

    async def __call__(self, message: Message) -> Any:
        """Проверить сообщение и передать в хендлер аргументы команды."""
        text = message.text or message.caption or ""
        parsed = split_command(text)
        if parsed is None:
            return False
        name, args = parsed
        if name not in self.commands:
            return False
        return {"command_args": args}


class RPCommand(BaseFilter):
    """Фильтр: ролевая команда только с префиксом.

    Без префикса обычная речь («я обнял её») командой не считается, поэтому
    RP-команды работают лишь как ``.обнять``, ``/обнять`` или ``!обнять``.

    :param commands_dict: словарь известных RP-команд.
    """

    #: Префиксы команды.
    PREFIXES: Final[tuple[str, ...]] = COMMAND_PREFIXES

    def __init__(self, commands_dict: Mapping[str, Any]) -> None:
        """Запомнить имена известных RP-команд."""
        self.commands: frozenset[str] = frozenset(
            str(command).lower() for command in commands_dict
        )

    async def __call__(self, message: Message) -> Any:
        """Проверить сообщение и передать в хендлер имя и аргументы команды."""
        parsed = split_command(message.text, require_prefix=True)
        if parsed is None:
            return False
        name, args = parsed
        if name not in self.commands:
            return False
        return {"rp_command": name, "rp_args": args}
