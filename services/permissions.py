"""Проверка прав: анонимные админы, владелец чата, обычные пользователи.

Основные правила проекта:
    * команды модерации выполняют только администраторы чата, включая
      анонимных (пост от имени канала или группы);
    * бот никогда не наказывает владельца чата и других администраторов;
    * владелец чата определяется автоматически через ``getChatAdministrators``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMember, Message

import config
from ..config import ADMIN_CACHE_TTL, MAX_CHATS_FOR_OWNER_SYNC, SUPERADMIN_IDS
from ..database import queries
from ..database.db import Database
from ..database.models import ChatInfo

logger = logging.getLogger(__name__)

#: ID служебного аккаунта, от имени которого пишут анонимные админы групп.
GROUP_ANONYMOUS_BOT_ID: int = 1087968824

#: Статусы, означающие, что участник является администратором.
ADMIN_STATUSES: frozenset[str] = frozenset({"administrator", "creator"})

#: Статусы, означающие, что участник находится в чате.
PRESENT_STATUSES: frozenset[str] = frozenset(
    {"creator", "administrator", "member", "restricted"}
)

#: Статусы, означающие, что участника нет в чате.
ABSENT_STATUSES: frozenset[str] = frozenset({"left", "kicked"})

#: Кэш администраторов чата: ``chat_id -> (набор id, время замера)``.
_admin_cache: dict[int, tuple[set[int], float]] = {}

#: Кэш идентификатора самого бота (``getMe`` вызывается один раз на процесс).
_bot_id_cache: Optional[int] = None


def clear_admin_cache(chat_id: Optional[int] = None) -> None:
    """Сбросить кэш админов: одного чата или целиком.

    Нужна команде ``.синк``: если в чате сменились администраторы, кэш надо
    выбросить, иначе бот ещё до пяти минут будет считать по-старому.

    :param chat_id: чат, для которого чистим кэш; ``None`` — весь кэш.
    """
    if chat_id is None:
        _admin_cache.clear()
        logger.info("Кэш админов очищен полностью.")
        return
    _admin_cache.pop(int(chat_id), None)
    logger.info("Кэш админов чата %s очищен.", chat_id)


def cached_admin_chats() -> list[int]:
    """Список чатов, по которым сейчас есть кэш админов (для логов)."""
    return list(_admin_cache)


async def get_bot_id(bot: Bot) -> Optional[int]:
    """Идентификатор самого бота — чтобы никогда не отмечать себя.

    Значение кэшируется на процесс: ``getMe`` не нужен на каждый вызов.

    :param bot: экземпляр бота.
    """
    global _bot_id_cache
    if _bot_id_cache is not None:
        return _bot_id_cache
    try:
        me = await bot.get_me()
    except Exception:  # noqa: BLE001 - без ID просто не исключаем себя
        logger.error("Не удалось получить данные бота (getMe)", exc_info=True)
        return None
    _bot_id_cache = int(me.id)
    return _bot_id_cache


async def get_chat_admin_ids(bot: Bot, chat_id: int) -> set[int]:
    """Идентификаторы всех админов чата (включая владельца).

    Анонимные администраторы и боты в набор не попадают: у анонимных нет
    обычного профиля для упоминания, а ботов отмечать не нужно. Результат
    кэшируется на :data:`config.ADMIN_CACHE_TTL` секунд (по умолчанию 5 минут).

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата (группы или супергруппы).
    :returns: множество ``user_id`` админов; пустое, если получить не удалось.
    """
    chat_id = int(chat_id)
    now = time.time()
    cached = _admin_cache.get(chat_id)
    if cached is not None and now - cached[1] < ADMIN_CACHE_TTL:
        return cached[0]

    try:
        administrators = await bot.get_chat_administrators(chat_id=chat_id)
    except TelegramAPIError as exc:
        logger.warning("Не удалось получить админов чата %s: %s", chat_id, exc)
        return set()
    except Exception:  # noqa: BLE001 - приватность важна, но падать нельзя
        logger.error("Неожиданная ошибка админов чата %s", chat_id, exc_info=True)
        return set()

    ids: set[int] = set()
    for admin in administrators:
        user = getattr(admin, "user", None)
        if user is None or getattr(admin, "is_anonymous", False):
            continue
        if getattr(user, "is_bot", False):
            continue
        user_id = getattr(user, "id", None)
        if user_id:
            ids.add(int(user_id))
    _admin_cache[chat_id] = (ids, now)
    logger.info("Кэш админов чата %s обновлён: %s админов.", chat_id, len(ids))
    return ids


async def filter_out_admins(bot: Bot, chat_id: int, user_ids: Iterable[int]) -> list[int]:
    """Убрать из списка всех администраторов чата.

    Используется везде, где формируются публичные упоминания: имена админов
    и владельца чата не раскрываются никогда.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    :param user_ids: исходный список идентификаторов.
    """
    admin_ids = await get_chat_admin_ids(bot, chat_id)
    if not admin_ids:
        return [int(user_id) for user_id in user_ids]
    return [int(user_id) for user_id in user_ids if int(user_id) not in admin_ids]


async def get_chat_owner_id(bot: Bot, db: Database, chat_id: int) -> Optional[int]:
    """Идентификатор владельца чата.

    Сначала смотрим базу (владелец сохраняется при добавлении бота), и лишь
    если он неизвестен — уточняем через Telegram и сохраняем результат.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    """
    chat: Optional[ChatInfo] = None
    try:
        chat = await queries.get_chat(db, chat_id)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось прочитать чат %s", chat_id, exc_info=True)
    if chat is not None and chat.owner_id:
        return int(chat.owner_id)

    try:
        owner_id, _ = await sync_chat_owner(bot, db, chat_id)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось определить владельца чата %s", chat_id, exc_info=True)
        return None
    return int(owner_id) if owner_id else None


async def get_mention_exclusions(
    bot: Bot,
    db: Database,
    chat_id: int,
    *,
    sender_id: Optional[int] = None,
    include_admins: bool = False,
    extra: Iterable[int] = (),
) -> set[int]:
    """Собрать набор тех, кого нельзя упоминать в этом чате.

    Всегда исключаются:
        * сам бот (он не должен отмечать себя никогда);
        * владелец чата (его имя публично не раскрывается);
        * автор действия (``sender_id``);
        * все остальные администраторы, если ``include_admins=True``.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param sender_id: автор вызова — не попадает в список упоминаний.
    :param include_admins: добавлять ли в исключения остальных админов.
    :param extra: дополнительные id, которые нужно исключить.
    """
    excluded: set[int] = {int(user_id) for user_id in extra if user_id}
    bot_id = await get_bot_id(bot)
    if bot_id:
        excluded.add(bot_id)
    if sender_id:
        excluded.add(int(sender_id))
    owner_id = await get_chat_owner_id(bot, db, chat_id)
    if owner_id:
        excluded.add(owner_id)
    if include_admins:
        excluded |= await get_chat_admin_ids(bot, chat_id)
    return excluded



@dataclass(slots=True)
class ModerationRights:
    """Результат проверки прав автора сообщения на модерацию."""

    allowed: bool
    actor_id: Optional[int] = None
    actor_label: str = "неизвестный"
    is_anonymous: bool = False
    is_superadmin: bool = False
    reason: str = ""


async def get_chat_member(bot: Bot, chat_id: int, user_id: int) -> Optional[ChatMember]:
    """Получить данные участника чата, не падая на ошибках Telegram.

    :returns: объект участника или ``None``, если получить данные не удалось.
    """
    try:
        return await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramAPIError as exc:
        logger.info("getChatMember(%s, %s) не выполнен: %s", chat_id, user_id, exc)
        return None
    except Exception:  # noqa: BLE001 - бот не должен падать из-за сети
        logger.error("Неожиданная ошибка getChatMember(%s, %s)", chat_id, user_id, exc_info=True)
        return None


async def get_member_status(bot: Bot, chat_id: int, user_id: int) -> Optional[str]:
    """Вернуть строковый статус участника чата или ``None``."""
    member = await get_chat_member(bot, chat_id, user_id)
    return member.status if member is not None else None


async def is_chat_administrator(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверить, является ли пользователь администратором или владельцем чата."""
    if user_id in SUPERADMIN_IDS:
        return True
    status = await get_member_status(bot, chat_id, user_id)
    return status in ADMIN_STATUSES


async def is_anonymous_admin_sender(
    bot: Bot,
    chat_id: int,
    message: Optional[Message],
) -> bool:
    """Пишет ли сообщение анонимный админ чата (от имени чата или канала).

    Анонимные администраторы не имеют обычного профиля: ``from_user`` у них
    либо служебный бот, либо отсутствует, а авторство указано в
    ``message.sender_chat``.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    :param message: сообщение с командой; ``None`` — анонимность не проверить.
    """
    sender_chat = getattr(message, "sender_chat", None) if message is not None else None
    if sender_chat is None:
        return False
    try:
        if int(sender_chat.id) == int(chat_id):
            return True
    except (TypeError, ValueError):
        return False
    if getattr(sender_chat, "type", None) != "channel":
        return False
    member = await get_chat_member(bot, chat_id, sender_chat.id)
    return member is not None and member.status in ADMIN_STATUSES


async def is_admin_or_anon(
    bot: Bot,
    chat_id: int,
    user_id: Optional[int],
    message: Optional[Message] = None,
) -> bool:
    """Админ чата, анонимный админ (пост от чата/канала) или супер-админ бота.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    :param user_id: идентификатор автора действия (может быть ``None``).
    :param message: сообщение с командой — нужно, чтобы распознать анонимного
        администратора.
    """
    if user_id is not None and int(user_id) in SUPERADMIN_IDS:
        return True
    if await is_anonymous_admin_sender(bot, chat_id, message):
        return True
    if user_id is None:
        return False
    try:
        author = int(user_id)
    except (TypeError, ValueError):
        return False
    if author == GROUP_ANONYMOUS_BOT_ID:
        # Служебный аккаунт, от имени которого пишут анонимные админы групп.
        return True
    return await is_chat_administrator(bot, chat_id, author)


def disabled_mod_commands(settings: Optional[Mapping[str, Any]]) -> set[str]:
    """Набор модер-команд, выключенных для админов в этом чате.

    Учитываются оба источника: новый список ``disabled_mod_commands`` и
    старый словарь ``commands`` (там ``False`` означает «команда выключена»).

    :param settings: настройки чата (или ``None``).
    """
    resolved = settings or {}
    names = set()
    raw = resolved.get(config.DISABLED_MOD_COMMANDS_KEY)
    if isinstance(raw, (list, tuple, set, frozenset)):
        names |= {str(item).strip().lower() for item in raw if str(item or "").strip()}
    commands = resolved.get("commands")
    if isinstance(commands, Mapping):
        names |= {
            str(command).strip().lower()
            for command, enabled in commands.items()
            if not enabled
        }
    return {name for name in names if name}


async def check_mod_permission(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: Optional[int],
    command_name: str,
    settings: Optional[dict[str, Any]] = None,
    *,
    message: Optional[Message] = None,
    actor_is_admin: Optional[bool] = None,
) -> tuple[bool, str]:
    """Может ли пользователь вызвать команду модерации и что ему ответить.

    Порядок проверок важен:

        1. владелец чата (и супер-админы бота) — всегда ``(True, "")``:
           список отключённых команд его не ограничивает;
        2. команда выключена для админов → ``(False, причина)``: ответ
           показывается, иначе админ не поймёт, почему команда молчит;
        3. автор не админ и не анонимный админ → ``(False, "")``: обычным
           участникам бот на модер-команды не отвечает вовсе.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param user_id: идентификатор автора команды (``None`` — автор скрыт).
    :param command_name: имя команды без префикса (например ``"бан"``).
    :param settings: настройки чата, если они уже прочитаны.
    :param message: сообщение с командой (для распознавания анонимных админов).
    :param actor_is_admin: готовый результат проверки прав админа (нужен для
        анонимных админов); ``None`` — проверить права здесь же.
    :returns: пара ``(можно_использовать, причина_отказа)``: пустая причина
        означает «молча игнорировать».
    """
    if user_id is not None and int(user_id) in SUPERADMIN_IDS:
        return True, ""

    try:
        chat = await queries.get_chat(db, chat_id)
    except Exception:  # noqa: BLE001 - без чата проверяем остальные условия
        logger.error("Не удалось прочитать чат %s", chat_id, exc_info=True)
        chat = None

    # 1. Владелец чата не ограничен настройками команд.
    if await is_chat_owner_of(bot, db, chat, user_id):
        return True, ""

    resolved = settings
    if resolved is None:
        try:
            resolved = await queries.get_chat_settings(db, chat_id)
        except Exception:  # noqa: BLE001
            logger.error("Не удалось прочитать настройки чата %s", chat_id, exc_info=True)
            resolved = None

    # 2. Команда выключена для админов — отвечаем причиной отказа.
    name = str(command_name or "").strip().lower()
    if name and name in disabled_mod_commands(resolved):
        return False, config.DISABLED_COMMAND_MESSAGE

    # 3. Права администратора (анонимные админы учтены отдельно).
    if actor_is_admin is not None:
        allowed = bool(actor_is_admin)
    else:
        allowed = await is_admin_or_anon(bot, chat_id, user_id, message)
    if not allowed:
        return False, ""

    return True, ""


async def is_bot_administrator(bot: Bot, chat_id: int) -> bool:
    """Проверить, есть ли у самого бота права администратора в чате."""
    try:
        me = await bot.get_me()
    except Exception:  # noqa: BLE001
        logger.error("Не удалось получить данные бота", exc_info=True)
        return False
    return await is_chat_administrator(bot, chat_id, me.id)


async def can_restrict_members(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверить право администратора ограничивать участников."""
    member = await get_chat_member(bot, chat_id, user_id)
    if member is None:
        return False
    if member.status == "creator":
        return True
    if member.status != "administrator":
        return False
    return bool(getattr(member, "can_restrict_members", True))


def is_chat_owner(chat: Optional[ChatInfo], user_id: Optional[int]) -> bool:
    """Проверить, является ли пользователь владельцем чата по данным БД."""
    if chat is None or user_id is None:
        return False
    return chat.owner_id is not None and chat.owner_id == user_id



async def check_moderation_rights(bot: Bot, message: Message) -> ModerationRights:
    """Определить, имеет ли автор сообщения право на модерацию.

    Поддерживаются три сценария:
        1. анонимный админ группы (``sender_chat`` совпадает с чатом);
        2. анонимный админ-канал (``sender_chat.type == "channel"``);
        3. обычный админ/владелец чата либо супер-админ бота из конфига.

    :param bot: экземпляр бота.
    :param message: сообщение с командой модерации.
    :returns: :class:`ModerationRights` с результатом проверки.
    """
    chat_id = message.chat.id
    sender_chat = message.sender_chat
    from_user = message.from_user

    # --- Анонимный админ группы: пишет от имени самого чата -----------------
    if sender_chat is not None and sender_chat.id == chat_id:
        return ModerationRights(
            allowed=True,
            actor_id=None,
            actor_label="анонимный админ чата",
            is_anonymous=True,
        )

    # --- Анонимный админ-канал: убеждаемся, что канал реально админ чата -----
    if sender_chat is not None and sender_chat.type == "channel":
        member = await get_chat_member(bot, chat_id, sender_chat.id)
        if member is not None and member.status in ADMIN_STATUSES:
            return ModerationRights(
                allowed=True,
                actor_id=sender_chat.id,
                actor_label=sender_chat.title or f"канал {sender_chat.id}",
                is_anonymous=True,
            )
        return ModerationRights(
            allowed=False,
            actor_id=sender_chat.id,
            actor_label=sender_chat.title or f"канал {sender_chat.id}",
            is_anonymous=True,
            reason="канал не является администратором чата",
        )

    if from_user is None or from_user.is_bot:
        return ModerationRights(
            allowed=False,
            actor_label="неизвестный отправитель",
            reason="отправитель не является администратором",
        )

    actor_label = from_user.full_name or str(from_user.id)

    # --- Супер-админ бота из .env ------------------------------------------
    if from_user.id in SUPERADMIN_IDS:
        return ModerationRights(
            allowed=True,
            actor_id=from_user.id,
            actor_label=actor_label,
            is_superadmin=True,
        )

    # --- Обычный администратор чата ----------------------------------------
    if await can_restrict_members(bot, chat_id, from_user.id):
        return ModerationRights(
            allowed=True,
            actor_id=from_user.id,
            actor_label=actor_label,
        )

    return ModerationRights(
        allowed=False,
        actor_id=from_user.id,
        actor_label=actor_label,
        reason="нет прав на ограничение участников",
    )


async def is_protected_target(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: Optional[int],
) -> bool:
    """Проверить, защищён ли пользователь от наказаний.

    Защищены: сам бот, супер-админы из конфига, владелец чата по данным БД
    и любые администраторы чата.

    :returns: ``True``, если наказывать пользователя нельзя.
    """
    if user_id is None:
        return True
    if user_id in SUPERADMIN_IDS:
        return True

    try:
        me = await bot.get_me()
        if user_id == me.id:
            return True
    except Exception:  # noqa: BLE001
        logger.error("Не удалось получить данные бота", exc_info=True)

    try:
        chat = await queries.get_chat(db, chat_id)
        if chat is not None and chat.owner_id == user_id:
            return True
    except Exception:  # noqa: BLE001
        logger.error("Не удалось проверить владельца чата %s", chat_id, exc_info=True)

    status = await get_member_status(bot, chat_id, user_id)
    return status in ADMIN_STATUSES


async def detect_chat_owner(bot: Bot, chat_id: int) -> tuple[Optional[int], Optional[int]]:
    """Определить владельца чата через ``getChatAdministrators``.

    :returns: кортеж ``(owner_id, owner_channel_id)``. Для анонимного владельца
        в ``owner_channel_id`` записывается идентификатор чата/канала, от имени
        которого он выступает.
    """
    owner_id: Optional[int] = None
    owner_channel_id: Optional[int] = None
    try:
        administrators = await bot.get_chat_administrators(chat_id=chat_id)
    except TelegramAPIError as exc:
        logger.error("Не удалось получить админов чата %s: %s", chat_id, exc)
        return None, None
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка при получении админов чата %s", chat_id, exc_info=True)
        return None, None

    for administrator in administrators:
        if administrator.status != "creator":
            continue
        user = getattr(administrator, "user", None)
        is_anonymous = bool(getattr(administrator, "is_anonymous", False))
        if user is None or user.is_bot or user.id == GROUP_ANONYMOUS_BOT_ID:
            owner_channel_id = chat_id
            continue
        owner_id = user.id
        if is_anonymous:
            # Владелец скрыт — запоминаем публичную «личину» чата/канала.
            owner_channel_id = chat_id
    return owner_id, owner_channel_id


async def sync_chat_owner(bot: Bot, db: Database, chat_id: int) -> tuple[Optional[int], Optional[int]]:
    """Найти владельца чата и сохранить результат в базу данных.

    Владелец дополнительно связывается с чатом в ``chat_users``: так он
    попадает в список «Мои чаты», даже если ни разу не писал в группе.

    :returns: кортеж ``(owner_id, owner_channel_id)``.
    """
    owner_id, owner_channel_id = await detect_chat_owner(bot, chat_id)
    try:
        await queries.set_chat_owner(db, chat_id, owner_id, owner_channel_id)
        if await queries.link_chat_owner(db, chat_id, owner_id):
            logger.info("Владелец чата %s определён: %s", chat_id, owner_id)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось сохранить владельца чата %s", chat_id, exc_info=True)
    return owner_id, owner_channel_id


async def is_chat_owner_of(
    bot: Bot,
    db: Database,
    chat: Optional[ChatInfo],
    user_id: int,
) -> bool:
    """Проверить, что пользователь — владелец чата.

    Если владелец ещё не сохранён в базе, он уточняется у Telegram
    (:func:`sync_chat_owner`) и результат сохраняется.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat: информация о чате из базы.
    :param user_id: проверяемый пользователь.
    """
    if user_id in SUPERADMIN_IDS:
        return True
    if chat is None:
        return False
    if chat.owner_id is not None:
        return chat.owner_id == user_id
    try:
        owner_id, _ = await sync_chat_owner(bot, db, chat.chat_id)
        if owner_id is not None:
            return owner_id == user_id
        member = await get_chat_member(bot, chat.chat_id, user_id)
        return member is not None and member.status == "creator"
    except Exception:  # noqa: BLE001
        logger.error("Не удалось проверить владельца чата %s", chat.chat_id, exc_info=True)
        return False


async def can_use_mod_command(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    command_name: str,
    settings: Optional[dict[str, Any]] = None,
    *,
    actor_is_admin: Optional[bool] = None,
) -> bool:
    """Может ли пользователь вызвать команду модерации в этом чате.

    Тонкая обёртка над :func:`check_mod_permission`: нужна там, где причина
    отказа не важна.

    Правила:
        * супер-админы и владелец чата — всегда, даже если команда выключена;
        * остальным нужна включённая команда в настройках чата
          (``settings["commands"][имя] = False`` или имя в списке
          ``disabled_mod_commands`` — команда выключена);
        * и права администратора чата.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param user_id: идентификатор пользователя.
    :param command_name: имя команды без префикса (например ``"бан"``).
    :param settings: настройки чата, если они уже прочитаны.
    :param actor_is_admin: готовый результат проверки прав (нужен для
        анонимных админов); ``None`` — проверить права здесь же.
    """
    allowed, _ = await check_mod_permission(
        bot,
        db,
        chat_id,
        user_id,
        command_name,
        settings,
        actor_is_admin=actor_is_admin,
    )
    return allowed


async def sync_user_chats(
    bot: Bot,
    db: Database,
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> list[int]:
    """Принудительно связать пользователя со всеми чатами, где он есть.

    Вызывается при ``/start`` в личке: владелец мог добавить бота в чат, но
    не написать там ни слова — тогда записи в ``chat_users`` нет и чат
    не показывался в «Моих чатах».

    Алгоритм по каждому известному боту чату:
        * если в базе владелец уже известен — связка создаётся локально;
        * иначе спрашиваем Telegram (:func:`get_chat_member`) и при статусе
          ``creator`` записываем владельца, а при любом другом «присутствующем»
          статусе просто создаём связку участника.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param user_id: идентификатор пользователя.
    :param username: юзернейм из Telegram (если известен).
    :param first_name: имя из Telegram (если известно).
    :returns: список ``chat_id``, связь с которыми создана или обновлена.
    """
    linked: list[int] = []
    try:
        await queries.ensure_user(db, user_id, username, first_name)
        chat_ids = await queries.get_known_chat_ids(db)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось получить список чатов для %s", user_id, exc_info=True)
        return linked

    api_calls = 0
    for chat_id in chat_ids:
        try:
            chat = await queries.get_chat(db, chat_id)
            if chat is None:
                continue

            # Владелец известен из базы — Telegram не беспокоим.
            if chat.owner_id == user_id:
                if await queries.link_chat_owner(db, chat_id, user_id):
                    linked.append(chat_id)
                continue

            # Связка уже есть — чат и так виден в «Моих чатах».
            if await queries.get_chat_user(db, chat_id, user_id) is not None:
                continue

            if api_calls >= MAX_CHATS_FOR_OWNER_SYNC:
                logger.info(
                    "Синхронизация %s прервана: проверено %s чатов из %s.",
                    user_id,
                    api_calls,
                    len(chat_ids),
                )
                break

            member = await get_chat_member(bot, chat_id, user_id)
            api_calls += 1
            if member is None or member.status not in PRESENT_STATUSES:
                continue

            if member.status == "creator":
                await queries.set_chat_owner(db, chat_id, user_id, None)
                await queries.link_chat_owner(db, chat_id, user_id)
                linked.append(chat_id)
                logger.info("Владелец чата %s определён при /start: %s", chat_id, user_id)
            else:
                await queries.set_member_presence(db, chat_id, user_id, True)
                linked.append(chat_id)
                logger.info("Пользователь %s отмечен участником чата %s.", user_id, chat_id)
        except Exception:  # noqa: BLE001 - один чат не должен ломать синхронизацию
            logger.error(
                "Не удалось связать пользователя %s с чатом %s", user_id, chat_id, exc_info=True
            )

    if linked:
        logger.info("/start: пользователь %s связан с чатами %s", user_id, linked)
    return linked


def extract_actor_id(rights: ModerationRights) -> Optional[int]:
    """Вернуть ID автора действия для журнала наказаний.

    Для анонимных администраторов возвращается ``None``.
    """
    return None if rights.is_anonymous else rights.actor_id

