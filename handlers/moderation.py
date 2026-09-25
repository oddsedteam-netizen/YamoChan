"""Команды модерации в группах и супергруппах.

Команда работает с любым префиксом (``.``, ``/``, ``!``) и совсем без него::

    .команда [реплай | @username | user_id] [время] [причина]
    /команда ...
    !команда ...
    команда ...

Правила проекта:
    * реагируем только на администраторов чата — включая анонимных
      (посты от имени канала или самого чата);
    * владельца чата, других админов и самого бота наказывать нельзя;
    * время и причина необязательны, у ``.варн`` времени нет вовсе;
    * команда, выключенная в настройках чата, работает только у владельца:
      остальным админам бот отвечает причиной отказа, а обычным участникам
      не отвечает вовсе.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import Message

import config
from ..database import queries
from ..database.db import Database
from ..services import antiraid as antiraid_service
from ..services import permissions, profile as profile_service, punishment, time_parser
from ..utils import command_filter, error_handler, telegram

logger = logging.getLogger(__name__)

#: Роутер команд модерации.
router: Final[Router] = Router(name="moderation")

#: Групповые типы чатов.
GROUP_CHAT_TYPES: Final[set[str]] = {ChatType.GROUP, ChatType.SUPERGROUP}

#: Префиксы, по которым подсказываем про неизвестную команду (``.``, ``/``, ``!``).
UNKNOWN_COMMAND_PREFIXES: Final[tuple[str, ...]] = command_filter.COMMAND_PREFIXES

#: Команды, которые в группе обрабатывает кто-то другой: на них нельзя
#: отвечать «не знаю», иначе до своего хендлера они не дойдут.
KNOWN_GROUP_COMMANDS: Final[frozenset[str]] = frozenset(
    set(config.MODERATION_COMMANDS) | {config.COMMAND_SYNC}
)


class UnknownCommandFilter(BaseFilter):
    """Фильтр: команда с префиксом, которой бот не знает.

    Фильтр, а не проверка внутри хендлера: aiogram останавливается на первом
    подходящем хендлере, поэтому «неизвестность» нужно решать именно здесь —
    иначе ``.синк`` и другие известные команды до своих хендлеров не дойдут.
    """

    async def __call__(self, message: Message) -> bool:
        """Проверить, что это неизвестная команда с префиксом."""
        raw = (message.text or "").strip()
        if not raw or raw[0] not in UNKNOWN_COMMAND_PREFIXES:
            return False
        parsed = parse_command(raw)
        if parsed is None or parsed.name in KNOWN_GROUP_COMMANDS:
            return False
        return True


@dataclass(slots=True)
class CommandTarget:
    """Цель модерации: пользователь или анонимный канал."""

    user_id: int
    name: str
    username: Optional[str] = None


@dataclass(slots=True)
class ParsedCommand:
    """Разобранная команда модерации."""

    name: str
    args: list[str]


def parse_command(text: Optional[str]) -> Optional[ParsedCommand]:
    """Разобрать текст сообщения в команду модерации.

    Команда распознаётся с префиксом ``.``, ``/``, ``!`` и без префикса,
    поэтому ``.бан 1д``, ``/бан``, ``!бан`` и просто ``бан`` равнозначны.

    :param text: например ``".бан 1д спам"`` или ``"бан 1д спам"``.
    :returns: :class:`ParsedCommand` или ``None``, если это не команда.
    """
    command = command_filter.split_command(text)
    if command is None:
        return None
    name, tail = command
    return ParsedCommand(name=name, args=[part for part in tail.split() if part])


class ModerationCommandFilter(BaseFilter):
    """Фильтр: сообщение является известной командой модерации.

    Команда ловится с любым префиксом (``.``, ``/``, ``!``) и без него —
    за это отвечает :class:`yamochan.utils.command_filter.FlexCommand`.
    """

    def __init__(self) -> None:
        """Собрать фильтр по списку команд модерации."""
        self._flex: command_filter.FlexCommand = command_filter.FlexCommand(
            *config.MODERATION_COMMANDS
        )

    async def __call__(self, message: Message) -> Any:
        """Проверить сообщение и передать в хендлер разобранную команду."""
        if not await self._flex(message):
            return False
        parsed = parse_command(message.text)
        if parsed is None or parsed.name not in config.MODERATION_COMMANDS:
            return False
        return {"command": parsed}


async def _stored_name(db: Database, user_id: int, fallback: str) -> str:
    """Взять сохранённое имя пользователя, иначе использовать запасное.

    :param db: соединение с базой.
    :param user_id: идентификатор пользователя.
    :param fallback: имя из Telegram.
    """
    try:
        stored = await queries.get_user(db, user_id)
        if stored is not None and stored.first_name:
            return stored.first_name
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("получении имени пользователя", exc)
    return fallback


async def resolve_target(
    message: Message,
    chat: Any,
    args: list[str],
    db: Database,
    bot: Bot,
) -> tuple[Optional[CommandTarget], list[str], Optional[str]]:
    """Определить, к кому применяется команда.

    Порядок поиска: реплай → ``@username`` → числовой ID.

    :param message: сообщение с командой.
    :param chat: объект чата из апдейта.
    :param args: аргументы команды.
    :param db: соединение с базой.
    :param bot: экземпляр бота (для запроса данных участника).
    :returns: ``(цель, оставшиеся_аргументы, текст_ошибки)``.
    """
    reply = message.reply_to_message
    if reply is not None:
        reply_user = reply.from_user
        if reply_user is not None and not reply_user.is_bot:
            name = await _stored_name(db, reply_user.id, reply_user.full_name or str(reply_user.id))
            target = CommandTarget(reply_user.id, name, reply_user.username)
            return target, list(args), None
        sender_chat = reply.sender_chat
        if sender_chat is not None and sender_chat.id != chat.id:
            name = sender_chat.title or f"канал {sender_chat.id}"
            return CommandTarget(sender_chat.id, name), list(args), None
        return None, list(args), config.NO_TARGET_MESSAGE

    if not args:
        return None, [], config.NO_TARGET_MESSAGE

    token = args[0].strip()
    remaining = list(args[1:])

    if token.startswith("@"):
        stored = await queries.get_user_by_username(db, token)
        if stored is None:
            return (
                None,
                remaining,
                "Не знаю такого пользователя~\n"
                "Пусть он напишет пару сообщений, или сделай реплай~ 🌸",
            )
        return CommandTarget(stored.user_id, stored.display_name, stored.username), remaining, None

    if token.lstrip("-").isdigit():
        user_id = int(token)
        name = await _stored_name(db, user_id, f"пользователь {user_id}")
        username: Optional[str] = None
        member = await permissions.get_chat_member(bot, chat.id, user_id)
        if member is not None and getattr(member, "user", None) is not None:
            name = member.user.full_name or name
            username = member.user.username
        return CommandTarget(user_id, name, username), remaining, None

    # Первый аргумент — не цель: значит цель не указана.
    return None, list(args), config.NO_TARGET_MESSAGE


def _reason(tokens: list[str]) -> Optional[str]:
    """Собрать причину из оставшихся слов (или ``None``)."""
    text = " ".join(tokens).strip()
    return text or None


async def _handle_info(
    bot: Bot,
    db: Database,
    message: Message,
    args: list[str],
) -> Optional[str]:
    """Собрать карточку участника для команды ``.инфо``.

    :returns: готовый текст ответа или текст ошибки.
    """
    target, _, error = await resolve_target(message, message.chat, args, db, bot)
    if target is None:
        return error or config.NO_TARGET_MESSAGE

    chat_id = message.chat.id
    profile = await queries.get_user(db, target.user_id)
    chat_user = await queries.get_chat_user(db, chat_id, target.user_id)
    active = await queries.get_active_punishments(db, chat_id, target.user_id)
    return profile_service.build_user_info_text(
        profile,
        chat_user,
        active,
        target.name,
        target.username,
    )


async def _handle_open(bot: Bot, db: Database, message: Message) -> str:
    """Открыть чат после антирейда или антиспама и снять защиту.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param message: сообщение с командой.
    """
    chat_id = message.chat.id
    opened = await antiraid_service.lift_protection(bot, db, chat_id)
    logger.info("Команда «.открыть» в чате %s: %s", chat_id, opened)
    if not opened:
        return "Не получилось открыть чат~ Проверь мои права админа 🙏"
    return profile_service.build_chat_unlocked_text()


async def _dispatch(
    bot: Bot,
    db: Database,
    message: Message,
    command: ParsedCommand,
    rights: permissions.ModerationRights,
) -> Optional[str]:
    """Выполнить нужную команду модерации и вернуть текст ответа.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param message: сообщение с командой.
    :param command: разобранная команда.
    :param rights: результат проверки прав автора.
    """
    name = command.name
    chat_id = message.chat.id

    if name == config.COMMAND_INFO:
        return await _handle_info(bot, db, message, command.args)

    if name == config.COMMAND_OPEN:
        return await _handle_open(bot, db, message)

    target, remaining, error = await resolve_target(
        message, message.chat, command.args, db, bot
    )
    if target is None:
        return error or config.NO_TARGET_MESSAGE

    # Владельца чата, админов и себя наказывать нельзя.
    if await permissions.is_protected_target(bot, db, chat_id, target.user_id):
        return config.PROTECTED_TARGET_MESSAGE

    actor_id = permissions.extract_actor_id(rights)
    logger.info(
        "Команда «.%s» от %s против %s в чате %s",
        name,
        rights.actor_label,
        target.user_id,
        chat_id,
    )

    if name == config.COMMAND_BAN:
        seconds, _, reason_tokens = time_parser.extract_duration(remaining)
        result = await punishment.ban_user(
            bot,
            db,
            chat_id,
            target.user_id,
            target.name,
            seconds,
            _reason(reason_tokens),
            actor_id,
        )
    elif name == config.COMMAND_UNBAN:
        result = await punishment.unban_user(bot, db, chat_id, target.user_id, target.name)
    elif name == config.COMMAND_MUTE:
        seconds, _, reason_tokens = time_parser.extract_duration(remaining)
        result = await punishment.mute_user(
            bot,
            db,
            chat_id,
            target.user_id,
            target.name,
            seconds,
            _reason(reason_tokens),
            actor_id,
        )
    elif name == config.COMMAND_UNMUTE:
        result = await punishment.unmute_user(bot, db, chat_id, target.user_id, target.name)
    elif name == config.COMMAND_WARN:
        result = await punishment.warn_user(
            bot,
            db,
            chat_id,
            target.user_id,
            target.name,
            _reason(remaining),
            actor_id,
        )
    elif name == config.COMMAND_UNWARN:
        result = await punishment.unwarn_user(db, chat_id, target.user_id, target.name)
    else:
        # Осталась только команда «.кик».
        result = await punishment.kick_user(
            bot,
            db,
            chat_id,
            target.user_id,
            target.name,
            _reason(remaining),
            actor_id,
        )
    return result.message


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), ModerationCommandFilter())
async def handle_moderation_command(
    message: Message,
    command: ParsedCommand,
    db: Database,
    bot: Bot,
) -> None:
    """Обработать команду модерации в группе.

    :param message: сообщение с командой.
    :param command: разобранная команда (прокидывается фильтром).
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    """
    chat_id = message.chat.id
    try:
        # 1. Права и настройки: модер-команды доступны только админам чата
        #    (включая анонимных). Обычным участникам бот не отвечает вовсе —
        #    пустая причина отказа означает молчание.
        rights = await permissions.check_moderation_rights(bot, message)
        settings = await queries.get_chat_settings(db, chat_id)
        actor_id = message.from_user.id if message.from_user else 0
        allowed, denial = await permissions.check_mod_permission(
            bot,
            db,
            chat_id,
            actor_id,
            command.name,
            settings,
            message=message,
            actor_is_admin=rights.allowed,
        )
        if not allowed:
            if denial:
                # Команда выключена для админов: владелец ей пользоваться
                # всё равно может, остальным объясняем причину отказа.
                await message.reply(denial)
            else:
                logger.info(
                    "Молча проигнорирована команда «.%s» от %s: %s",
                    command.name,
                    rights.actor_label,
                    rights.reason or "нет прав на модерацию",
                )
            return

        # 2. Учитываем чат, владельца и активность автора.
        # Числа участников в апдейте сообщения нет (в aiogram 3.x поля
        # members_count у Chat нет) — счётчик обновляется при входах/выходах
        # участников и при добавлении бота.
        await queries.ensure_chat(
            db,
            chat_id,
            message.chat.title,
            telegram.resolve_members_count(message.chat),
        )
        if message.from_user is not None and not message.from_user.is_bot:
            await queries.ensure_user(
                db,
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
            )
        chat_info = await queries.get_chat(db, chat_id)
        if chat_info is None or chat_info.owner_id is None:
            await permissions.sync_chat_owner(bot, db, chat_id)
        elif await queries.get_chat_user(db, chat_id, chat_info.owner_id) is None:
            # Владелец известен, но связи с chat_users нет — создаём её,
            # чтобы чат появился у него в «Моих чатах».
            await queries.link_chat_owner(db, chat_id, chat_info.owner_id)

        # 3. Без прав админа бот не сможет наказать.
        if not await permissions.is_bot_administrator(bot, chat_id):
            await message.reply(
                "Мне не хватает прав администратора~ 🙏\n"
                "Дай мне право ограничивать участников, и я всё сделаю!"
            )
            return

        # 4. Выполняем команду и отвечаем от лица YamoChan.
        reply_text = await _dispatch(bot, db, message, command, rights)
        if reply_text:
            await message.reply(reply_text)
    except Exception as exc:  # noqa: BLE001 - бот не должен падать
        error_handler.log_exception(f"команде «.{command.name}»", exc)
        await error_handler.notify_user_softly(message)


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), UnknownCommandFilter())
async def unknown_command(message: Message, bot: Bot) -> None:
    """Подсказать, что за команда неизвестна (только для админов)."""
    try:
        parsed = parse_command(message.text)
        if parsed is None:
            return
        author_id = message.from_user.id if message.from_user else 0
        if not author_id:
            return
        if not await permissions.is_chat_administrator(bot, message.chat.id, author_id):
            return
        await message.reply(
            f"Команды «.{parsed.name}» я не знаю~ 🤔\n"
            "Посмотри список в /start у меня в личке!"
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("подсказке о неизвестной команде", exc)


class SyncCommandFilter(BaseFilter):
    """Фильтр: служебная команда синхронизации.

    Работают ``.синк``, ``/синк``, ``!синк`` и просто ``синк``.
    """

    async def __call__(self, message: Message) -> bool:
        """Проверить, что сообщение — команда синхронизации."""
        parsed = parse_command(message.text)
        return parsed is not None and parsed.name == config.COMMAND_SYNC


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), SyncCommandFilter())
async def cmd_sync(message: Message, db: Database, bot: Bot) -> None:
    """Сбросить кэш админов и перепроверить владельца (``.синк``, ``/синк``).

    Нужна, когда в чате сменились администраторы: список админов кэшируется
    на :data:`config.ADMIN_CACHE_TTL` секунд, а после синхронизации бот сразу
    считает права правильно и никого лишнего не отмечает в вызовах.

    Доступна администраторам чата и супер-админам бота (и владельцу).
    """
    try:
        user = message.from_user
        if user is None:
            return

        chat_id = message.chat.id
        settings = await queries.get_chat_settings(db, chat_id)
        rights = await permissions.check_moderation_rights(bot, message)
        allowed, denial = await permissions.check_mod_permission(
            bot,
            db,
            chat_id,
            user.id,
            config.COMMAND_SYNC,
            settings,
            message=message,
            actor_is_admin=rights.allowed,
        )
        if not allowed:
            if denial:
                await message.reply(denial)
            else:
                logger.info(
                    "Молча проигнорирована команда «.%s» от %s.",
                    config.COMMAND_SYNC,
                    rights.actor_label,
                )
            return

        permissions.clear_admin_cache(chat_id)
        admin_ids = await permissions.get_chat_admin_ids(bot, chat_id)
        owner_id, _ = await permissions.sync_chat_owner(bot, db, chat_id)
        logger.info(
            "Синхронизация чата %s выполнена (%s): админов %s, владелец %s.",
            chat_id,
            user.id,
            len(admin_ids),
            "определён" if owner_id else "не определён",
        )

        # Никаких имён и ID админов в публичном ответе — только факты.
        await message.reply(
            "✅ <b>Синхронизация готова!</b>\n\n"
            f"👑 Админов в чате: {len(admin_ids)}\n"
            f"🪪 Владелец: {'определён ✅' if owner_id else 'не определён ❌'}\n\n"
            "🕶 Я не отмечаю владельца, админов, автора вызова и себя~"
        )
    except Exception as exc:  # noqa: BLE001 - команда не должна ронять бота
        error_handler.log_exception("команде синхронизации", exc)
        await error_handler.notify_user_softly(message)