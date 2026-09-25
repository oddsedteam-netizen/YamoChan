"""Все CRUD-операции YamoChan.

Каждая функция принимает первым аргументом объект
:class:`yamochan.database.db.Database` и не хранит состояние: это делает слой
запросов удобным для повторного использования и тестирования.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Final, Mapping, Optional, Sequence

import config
from database import models
from database.db import Database
from database.models import (
    AdminBan,
    AdminLogEntry,
    ChatInfo,
    ChatUser,
    Complaint,
    Punishment,
    UserProfile,
    Warn,
    dump_json,
    to_iso,
    utcnow,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Пользователи (users)
# ---------------------------------------------------------------------------
async def ensure_user(
    db: Database,
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> UserProfile:
    """Создать профиль пользователя, если его ещё нет, и обновить имена.

    :returns: актуальный профиль пользователя.
    """
    await db.execute(
        """
        INSERT INTO users (user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = COALESCE(excluded.username, users.username),
            first_name = COALESCE(excluded.first_name, users.first_name)
        """,
        (user_id, username, first_name, to_iso(utcnow())),
    )
    profile = await get_user(db, user_id)
    if profile is None:  # pragma: no cover - защита от неожиданной гонки
        profile = UserProfile(user_id=user_id, username=username, first_name=first_name)
    return profile


async def get_user(db: Database, user_id: int) -> Optional[UserProfile]:
    """Получить профиль пользователя по его идентификатору."""
    row = await db.fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return UserProfile.from_row(row) if row is not None else None


async def get_user_by_username(db: Database, username: str) -> Optional[UserProfile]:
    """Найти пользователя по юзернейму (без учёта регистра и символа @)."""
    clean = (username or "").strip().lstrip("@")
    if not clean:
        return None
    row = await db.fetch_one(
        "SELECT * FROM users WHERE username IS NOT NULL AND LOWER(username) = LOWER(?)",
        (clean,),
    )
    return UserProfile.from_row(row) if row is not None else None


async def add_reputation(db: Database, user_id: int, delta: int) -> int:
    """Изменить репутацию пользователя и вернуть новое значение."""
    await db.execute(
        "UPDATE users SET reputation = reputation + ? WHERE user_id = ?",
        (int(delta), user_id),
    )
    value = await db.fetch_value(
        "SELECT reputation FROM users WHERE user_id = ?", (user_id,), default=0
    )
    return int(value)


async def increment_total_messages(db: Database, user_id: int, amount: int = 1) -> None:
    """Увеличить глобальный счётчик сообщений пользователя."""
    await db.execute(
        "UPDATE users SET total_messages = total_messages + ? WHERE user_id = ?",
        (int(amount), user_id),
    )


async def set_global_ban(db: Database, user_id: int, value: bool) -> None:
    """Установить или снять глобальную метку «забанен где-то»."""
    await db.execute(
        "UPDATE users SET is_globally_banned = ? WHERE user_id = ?",
        (1 if value else 0, user_id),
    )


async def set_user_spammer(db: Database, user_id: int, value: bool = True) -> None:
    """Пометить пользователя как спамера (или снять метку).

    :param db: соединение с базой данных.
    :param user_id: идентификатор пользователя.
    :param value: ``True`` — метка ставится, ``False`` — снимается.
    """
    await ensure_user(db, user_id)
    await db.execute(
        "UPDATE users SET is_spammer = ? WHERE user_id = ?",
        (1 if value else 0, user_id),
    )


async def get_ban_marks(db: Database, user_id: int) -> list[int]:
    """Вернуть список чатов, где пользователь когда-либо получал бан."""
    row = await db.fetch_one("SELECT ban_marks FROM users WHERE user_id = ?", (user_id,))
    if row is None:
        return []
    return models.load_json_list(row["ban_marks"])


async def add_ban_mark(db: Database, user_id: int, chat_id: int) -> list[int]:
    """Добавить метку бана в другом чате и вернуть обновлённый список."""
    marks = await get_ban_marks(db, user_id)
    if chat_id not in marks:
        marks.append(chat_id)
    await db.execute(
        "UPDATE users SET ban_marks = ?, is_globally_banned = 1 WHERE user_id = ?",
        (dump_json(marks), user_id),
    )
    return marks


async def remove_ban_mark(db: Database, user_id: int, chat_id: int) -> list[int]:
    """Убрать метку бана конкретного чата и вернуть обновлённый список."""
    marks = [mark for mark in await get_ban_marks(db, user_id) if mark != chat_id]
    await db.execute(
        "UPDATE users SET ban_marks = ?, is_globally_banned = ? WHERE user_id = ?",
        (dump_json(marks), 1 if marks else 0, user_id),
    )
    return marks


# ---------------------------------------------------------------------------
# Чаты (chats)
# ---------------------------------------------------------------------------
async def ensure_chat(
    db: Database,
    chat_id: int,
    title: Optional[str] = None,
    members_count: Optional[int] = None,
) -> ChatInfo:
    """Создать запись о чате, если её нет, и обновить название/количество."""
    await db.execute(
        """
        INSERT INTO chats (chat_id, title, settings, members_count, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title = COALESCE(excluded.title, chats.title),
            members_count = CASE
                WHEN excluded.members_count > 0 THEN excluded.members_count
                ELSE chats.members_count
            END
        """,
        (
            chat_id,
            title,
            dump_json(config.DEFAULT_CHAT_SETTINGS),
            int(members_count or 0),
            to_iso(utcnow()),
        ),
    )
    chat = await get_chat(db, chat_id)
    if chat is None:  # pragma: no cover - защита от неожиданной гонки
        chat = ChatInfo(chat_id=chat_id, title=title, settings=dict(config.DEFAULT_CHAT_SETTINGS))
    return chat


async def get_chat(db: Database, chat_id: int) -> Optional[ChatInfo]:
    """Получить информацию о чате по его идентификатору."""
    row = await db.fetch_one("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    return ChatInfo.from_row(row) if row is not None else None


async def update_chat_title(db: Database, chat_id: int, title: str) -> None:
    """Обновить название чата."""
    await db.execute("UPDATE chats SET title = ? WHERE chat_id = ?", (title, chat_id))


async def update_members_count(db: Database, chat_id: int, members_count: int) -> None:
    """Обновить количество участников чата."""
    await db.execute(
        "UPDATE chats SET members_count = ? WHERE chat_id = ?",
        (int(members_count), chat_id),
    )


async def set_chat_owner(
    db: Database,
    chat_id: int,
    owner_id: Optional[int],
    owner_channel_id: Optional[int] = None,
) -> None:
    """Сохранить владельца чата (``None`` не перетирает уже найденное значение)."""
    await db.execute(
        """
        UPDATE chats SET
            owner_id = COALESCE(?, owner_id),
            owner_channel_id = COALESCE(?, owner_channel_id)
        WHERE chat_id = ?
        """,
        (owner_id, owner_channel_id, chat_id),
    )


async def get_chat_settings(db: Database, chat_id: int) -> dict[str, Any]:
    """Вернуть настройки чата с подстановкой значений по умолчанию."""
    chat = await get_chat(db, chat_id)
    settings: dict[str, Any] = dict(config.DEFAULT_CHAT_SETTINGS)
    stored = chat.settings if chat is not None else {}
    if stored:
        settings.update(stored)
    # Совместимость: старые чаты хранили только ключ "antiraid".
    if "antiraid_enabled" not in (stored or {}):
        settings["antiraid_enabled"] = bool(settings.get("antiraid"))
    settings["antiraid"] = bool(settings.get("antiraid_enabled"))
    # Совместимость: прежний ключ call_mode → call_enabled.
    if "call_enabled" not in (stored or {}):
        settings["call_enabled"] = bool(settings.get("call_mode"))
    settings["call_mode"] = bool(settings.get("call_enabled"))
    commands = dict(config.DEFAULT_CHAT_SETTINGS["commands"])
    stored_commands = settings.get("commands")
    if isinstance(stored_commands, dict):
        for command, enabled in stored_commands.items():
            if command in commands:
                commands[command] = bool(enabled)
    settings["commands"] = commands
    # Отключённые для админов команды: держим список и старый словарь
    # ``commands`` согласованными (``False`` в словаре = команда выключена).
    disabled = normalize_disabled_commands(settings.get(config.DISABLED_MOD_COMMANDS_KEY))
    disabled |= {command for command, enabled in commands.items() if not enabled}
    settings[config.DISABLED_MOD_COMMANDS_KEY] = sorted(disabled)
    return settings


def normalize_disabled_commands(raw: Any) -> set[str]:
    """Привести список отключённых модер-команд к набору имён без мусора.

    :param raw: значение настройки ``disabled_mod_commands`` (обычно список).
    :returns: набор имён команд в нижнем регистре.
    """
    if isinstance(raw, (list, tuple, set, frozenset)):
        items = raw
    else:
        return set()
    return {
        str(item).strip().lower()
        for item in items
        if str(item or "").strip()
    }


async def save_chat_settings(db: Database, chat_id: int, settings: dict[str, Any]) -> None:
    """Полностью перезаписать json-поле настроек чата."""
    await db.execute(
        "UPDATE chats SET settings = ? WHERE chat_id = ?",
        (dump_json(settings), chat_id),
    )


async def update_chat_setting(db: Database, chat_id: int, key: str, value: Any) -> dict[str, Any]:
    """Изменить одну настройку чата и вернуть актуальный словарь настроек."""
    settings = await get_chat_settings(db, chat_id)
    settings[key] = value
    await save_chat_settings(db, chat_id, settings)
    return settings


async def update_chat_command(
    db: Database,
    chat_id: int,
    command: str,
    enabled: bool,
) -> dict[str, Any]:
    """Включить или выключить одну команду модерации в конкретном чате.

    Список ``disabled_mod_commands`` обновляется вместе со словарём
    ``commands``: так команда, выключенная в меню, сразу перестаёт
    работать и у администраторов, а владелец ею пользоваться всё равно
    сможет.
    """
    settings = await get_chat_settings(db, chat_id)
    commands = dict(settings.get("commands") or {})
    commands[command] = bool(enabled)
    settings["commands"] = commands

    disabled = normalize_disabled_commands(settings.get(config.DISABLED_MOD_COMMANDS_KEY))
    if enabled:
        disabled.discard(str(command).strip().lower())
    else:
        disabled.add(str(command).strip().lower())
    settings[config.DISABLED_MOD_COMMANDS_KEY] = sorted(disabled)

    await save_chat_settings(db, chat_id, settings)
    return settings


async def set_disabled_mod_command(
    db: Database,
    chat_id: int,
    command: str,
    disabled: bool,
) -> dict[str, Any]:
    """Добавить или убрать команду модерации в списке отключённых для админов.

    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param command: имя команды без префикса (например ``"бан"``).
    :param disabled: ``True`` — команда выключена для админов.
    :returns: актуальный словарь настроек чата.
    """
    settings = await get_chat_settings(db, chat_id)
    name = str(command or "").strip().lower()
    if not name:
        return settings

    disabled_names = normalize_disabled_commands(
        settings.get(config.DISABLED_MOD_COMMANDS_KEY)
    )
    if disabled:
        disabled_names.add(name)
    else:
        disabled_names.discard(name)
    settings[config.DISABLED_MOD_COMMANDS_KEY] = sorted(disabled_names)

    commands = dict(settings.get("commands") or {})
    if name in commands:
        commands[name] = not disabled
        settings["commands"] = commands

    await save_chat_settings(db, chat_id, settings)
    return settings


# ---------------------------------------------------------------------------
# Участники чатов (chat_users)
# ---------------------------------------------------------------------------
async def ensure_chat_user(db: Database, chat_id: int, user_id: int) -> ChatUser:
    """Создать связку «чат — пользователь», если её ещё нет."""
    await db.execute(
        """
        INSERT INTO chat_users (chat_id, user_id, joined_at, last_message_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO NOTHING
        """,
        (chat_id, user_id, to_iso(utcnow()), to_iso(utcnow())),
    )
    chat_user = await get_chat_user(db, chat_id, user_id)
    if chat_user is None:  # pragma: no cover - защита от неожиданной гонки
        chat_user = ChatUser(chat_id=chat_id, user_id=user_id)
    return chat_user


async def get_chat_user(db: Database, chat_id: int, user_id: int) -> Optional[ChatUser]:
    """Получить связку «чат — пользователь»."""
    row = await db.fetch_one(
        "SELECT * FROM chat_users WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    return ChatUser.from_row(row) if row is not None else None


async def link_chat_owner(db: Database, chat_id: int, owner_id: Optional[int]) -> bool:
    """Связать владельца чата с таблицами ``users`` и ``chat_users``.

    Без записи в ``chat_users`` владелец не находился в списке «Мои чаты»:
    он мог ни разу не написать в группе, а сообщения-команды не считаются.

    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param owner_id: идентификатор владельца. ``None`` (владелец анонимный
        или ещё не определён) — ничего не делаем.
    :returns: ``True``, если владелец определён и связка создана.
    """
    if owner_id is None or int(owner_id) <= 0:
        return False

    owner = int(owner_id)
    await ensure_user(db, owner)
    await ensure_chat_user(db, chat_id, owner)
    await db.execute(
        "UPDATE chat_users SET is_member = 1 WHERE chat_id = ? AND user_id = ?",
        (chat_id, owner),
    )
    logger.info("Владелец %s связан с чатом %s как участник.", owner, chat_id)
    return True


async def mark_raid_suspects(db: Database, chat_id: int, user_ids: Sequence[int]) -> int:
    """Пометить участников как подозрительных при рейде.

    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param user_ids: идентификаторы участников рейда.
    :returns: сколько участников помечено.
    """
    marked = 0
    for user_id in user_ids:
        target = int(user_id)
        await ensure_chat_user(db, chat_id, target)
        await db.execute(
            "UPDATE chat_users SET is_raid_suspect = 1 WHERE chat_id = ? AND user_id = ?",
            (chat_id, target),
        )
        marked += 1
    logger.info("В чате %s помечено подозрительными: %s.", chat_id, marked)
    return marked


async def clear_raid_suspects(
    db: Database,
    chat_id: int,
    user_ids: Optional[Sequence[int]] = None,
) -> None:
    """Снять метки подозрительных у всех участников или у указанных.

    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param user_ids: конкретные участники; ``None`` — снять у всех.
    """
    if user_ids is None:
        await db.execute(
            "UPDATE chat_users SET is_raid_suspect = 0 WHERE chat_id = ?",
            (chat_id,),
        )
        return
    for user_id in user_ids:
        await db.execute(
            "UPDATE chat_users SET is_raid_suspect = 0 WHERE chat_id = ? AND user_id = ?",
            (chat_id, int(user_id)),
        )


async def get_raid_suspects(db: Database, chat_id: int) -> list[int]:
    """Идентификаторы участников, помеченных как подозрительные при рейде."""
    rows = await db.fetch_all(
        "SELECT user_id FROM chat_users WHERE chat_id = ? AND is_raid_suspect = 1",
        (chat_id,),
    )
    return [int(row["user_id"]) for row in rows]


async def get_owner_chats(db: Database, owner_id: int) -> list[ChatInfo]:
    """Чаты, где пользователь — владелец (нужно для кнопок снятия защиты)."""
    rows = await db.fetch_all(
        "SELECT * FROM chats WHERE owner_id = ? ORDER BY title ASC",
        (owner_id,),
    )
    return [ChatInfo.from_row(row) for row in rows]


async def get_active_chat_members(db: Database, chat_id: int) -> list[ChatUser]:
    """Участники чата, которых можно звать: не забанены в этом чате.

    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    """
    rows = await db.fetch_all(
        """
        SELECT * FROM chat_users
         WHERE chat_id = ? AND is_banned = 0
         ORDER BY messages_count DESC
        """,
        (chat_id,),
    )
    return [ChatUser.from_row(row) for row in rows]


async def get_users_by_ids(
    db: Database,
    user_ids: Sequence[int],
) -> dict[int, UserProfile]:
    """Профили пользователей по идентификаторам — одним запросом.

    :param db: соединение с базой данных.
    :param user_ids: идентификаторы пользователей.
    :returns: словарь ``user_id → профиль`` (кого нет в базе — не попадут).
    """
    ids = [int(user_id) for user_id in user_ids]
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = await db.fetch_all(
        f"SELECT * FROM users WHERE user_id IN ({placeholders})",
        tuple(ids),
    )
    return {int(row["user_id"]): UserProfile.from_row(row) for row in rows}


async def set_member_presence(db: Database, chat_id: int, user_id: int, is_member: bool) -> None:
    """Отметить, находится ли участник в чате (данные при этом не удаляются)."""
    await ensure_chat_user(db, chat_id, user_id)
    await db.execute(
        "UPDATE chat_users SET is_member = ? WHERE chat_id = ? AND user_id = ?",
        (1 if is_member else 0, chat_id, user_id),
    )


async def increment_messages(db: Database, chat_id: int, user_id: int) -> None:
    """Увеличить счётчики сообщений: локальный и глобальный."""
    await ensure_chat_user(db, chat_id, user_id)
    await db.execute(
        """
        UPDATE chat_users
           SET messages_count = messages_count + 1,
               last_message_at = ?,
               is_member = 1
         WHERE chat_id = ? AND user_id = ?
        """,
        (to_iso(utcnow()), chat_id, user_id),
    )
    await increment_total_messages(db, user_id)


async def set_user_banned(
    db: Database,
    chat_id: int,
    user_id: int,
    is_banned: bool,
    ban_until: Optional[datetime] = None,
) -> None:
    """Обновить состояние бана пользователя в чате."""
    await ensure_chat_user(db, chat_id, user_id)
    await db.execute(
        """
        UPDATE chat_users
           SET is_banned = ?, ban_until = ?
         WHERE chat_id = ? AND user_id = ?
        """,
        (1 if is_banned else 0, to_iso(ban_until) if is_banned else None, chat_id, user_id),
    )


async def set_user_muted(
    db: Database,
    chat_id: int,
    user_id: int,
    is_muted: bool,
    mute_until: Optional[datetime] = None,
) -> None:
    """Обновить состояние мута пользователя в чате."""
    await ensure_chat_user(db, chat_id, user_id)
    await db.execute(
        """
        UPDATE chat_users
           SET is_muted = ?, mute_until = ?
         WHERE chat_id = ? AND user_id = ?
        """,
        (1 if is_muted else 0, to_iso(mute_until) if is_muted else None, chat_id, user_id),
    )


# ---------------------------------------------------------------------------
# Предупреждения (warns)
# ---------------------------------------------------------------------------
async def add_warn(
    db: Database,
    chat_id: int,
    user_id: int,
    reason: Optional[str],
    issued_by: Optional[int],
) -> int:
    """Добавить предупреждение, вернуть актуальное количество варнов."""
    await ensure_chat_user(db, chat_id, user_id)
    await db.insert(
        """
        INSERT INTO warns (chat_id, user_id, reason, issued_by, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (chat_id, user_id, reason, issued_by, to_iso(utcnow())),
    )
    count = await get_warns_count(db, chat_id, user_id)
    await db.execute(
        "UPDATE chat_users SET warns_count = ? WHERE chat_id = ? AND user_id = ?",
        (count, chat_id, user_id),
    )
    return count


async def get_warns_count(db: Database, chat_id: int, user_id: int) -> int:
    """Количество предупреждений пользователя в конкретном чате."""
    value = await db.fetch_value(
        "SELECT COUNT(*) FROM warns WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    return int(value)


async def get_warns(db: Database, chat_id: int, user_id: int) -> list[Warn]:
    """Список предупреждений пользователя в чате (от старых к новым)."""
    rows = await db.fetch_all(
        "SELECT * FROM warns WHERE chat_id = ? AND user_id = ? ORDER BY id ASC",
        (chat_id, user_id),
    )
    return [Warn.from_row(row) for row in rows]


async def pop_last_warn(db: Database, chat_id: int, user_id: int) -> Optional[Warn]:
    """Удалить последнее предупреждение и вернуть его (или ``None``)."""
    row = await db.fetch_one(
        "SELECT * FROM warns WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id, user_id),
    )
    if row is None:
        return None
    removed = Warn.from_row(row)
    await db.execute("DELETE FROM warns WHERE id = ?", (removed.id,))
    count = await get_warns_count(db, chat_id, user_id)
    await db.execute(
        "UPDATE chat_users SET warns_count = ? WHERE chat_id = ? AND user_id = ?",
        (count, chat_id, user_id),
    )
    return removed


async def clear_warns(db: Database, chat_id: int, user_id: int) -> int:
    """Удалить все предупреждения пользователя в чате, вернуть их количество."""
    count = await get_warns_count(db, chat_id, user_id)
    await db.execute(
        "DELETE FROM warns WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    await db.execute(
        "UPDATE chat_users SET warns_count = 0 WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    return count


async def get_user_warns_by_chat(db: Database, user_id: int) -> list[tuple[int, int]]:
    """Вернуть список ``(chat_id, количество варнов)`` по всем чатам."""
    rows = await db.fetch_all(
        "SELECT chat_id, COUNT(*) AS cnt FROM warns WHERE user_id = ? GROUP BY chat_id",
        (user_id,),
    )
    return [(int(row["chat_id"]), int(row["cnt"])) for row in rows]


# ---------------------------------------------------------------------------
# Наказания (punishments)
# ---------------------------------------------------------------------------
async def add_punishment(
    db: Database,
    chat_id: int,
    user_id: int,
    kind: str,
    reason: Optional[str],
    duration: Optional[str],
    duration_seconds: Optional[int],
    issued_by: Optional[int],
) -> Punishment:
    """Создать запись о наказании и вернуть её модель."""
    now = utcnow()
    expires_at = now + timedelta(seconds=duration_seconds) if duration_seconds else None
    punishment_id = await db.insert(
        """
        INSERT INTO punishments (
            chat_id, user_id, type, reason, duration, duration_seconds,
            issued_by, created_at, expires_at, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            chat_id,
            user_id,
            kind,
            reason,
            duration,
            duration_seconds,
            issued_by,
            to_iso(now),
            to_iso(expires_at) if expires_at else None,
        ),
    )
    return Punishment(
        id=punishment_id,
        chat_id=chat_id,
        user_id=user_id,
        type=kind,
        reason=reason,
        duration=duration,
        duration_seconds=duration_seconds,
        issued_by=issued_by,
        created_at=now,
        expires_at=expires_at,
        is_active=True,
    )


async def get_active_punishment(
    db: Database,
    chat_id: int,
    user_id: int,
    kind: str,
) -> Optional[Punishment]:
    """Получить активное наказание указанного типа (с учётом истечения срока)."""
    row = await db.fetch_one(
        """
        SELECT * FROM punishments
         WHERE chat_id = ? AND user_id = ? AND type = ? AND is_active = 1
         ORDER BY id DESC LIMIT 1
        """,
        (chat_id, user_id, kind),
    )
    if row is None:
        return None
    punishment = Punishment.from_row(row)
    if punishment.expires_at is not None and punishment.expires_at <= utcnow():
        return None
    return punishment


async def get_active_punishments(
    db: Database,
    chat_id: int,
    user_id: int,
) -> list[Punishment]:
    """Список всех активных наказаний пользователя в чате."""
    rows = await db.fetch_all(
        """
        SELECT * FROM punishments
         WHERE chat_id = ? AND user_id = ? AND is_active = 1
         ORDER BY id DESC
        """,
        (chat_id, user_id),
    )
    now = utcnow()
    result: list[Punishment] = []
    for row in rows:
        punishment = Punishment.from_row(row)
        if punishment.expires_at is not None and punishment.expires_at <= now:
            continue
        result.append(punishment)
    return result


async def deactivate_punishments(
    db: Database,
    chat_id: int,
    user_id: int,
    kind: Optional[str] = None,
) -> int:
    """Пометить наказания неактивными, вернуть количество изменённых записей."""
    if kind is None:
        rows = await db.fetch_all(
            """
            SELECT id FROM punishments
             WHERE chat_id = ? AND user_id = ? AND is_active = 1
            """,
            (chat_id, user_id),
        )
    else:
        rows = await db.fetch_all(
            """
            SELECT id FROM punishments
             WHERE chat_id = ? AND user_id = ? AND type = ? AND is_active = 1
            """,
            (chat_id, user_id, kind),
        )
    identifiers = [int(row["id"]) for row in rows]
    if not identifiers:
        return 0
    placeholders = ", ".join("?" for _ in identifiers)
    await db.execute(
        f"UPDATE punishments SET is_active = 0 WHERE id IN ({placeholders})",
        tuple(identifiers),
    )
    return len(identifiers)


async def get_chat_punishment_counts(
    db: Database,
    chat_id: int,
) -> dict[str, int]:
    """Сколько участников чата сейчас в бане и в муте."""
    now_iso = to_iso(utcnow())
    banned = await db.fetch_value(
        """
        SELECT COUNT(DISTINCT user_id) FROM punishments
         WHERE chat_id = ? AND type = 'ban' AND is_active = 1
           AND (expires_at IS NULL OR expires_at > ?)
        """,
        (chat_id, now_iso),
    )
    muted = await db.fetch_value(
        """
        SELECT COUNT(DISTINCT user_id) FROM punishments
         WHERE chat_id = ? AND type = 'mute' AND is_active = 1
           AND (expires_at IS NULL OR expires_at > ?)
        """,
        (chat_id, now_iso),
    )
    warned = await db.fetch_value(
        "SELECT COUNT(DISTINCT user_id) FROM warns WHERE chat_id = ?",
        (chat_id,),
    )
    return {"banned": int(banned), "muted": int(muted), "warned": int(warned)}


async def get_expired_punishments(db: Database, limit: int = 50) -> list[Punishment]:
    """Наказания, срок которых истёк, но запись ещё активна (для авто-снятия)."""
    rows = await db.fetch_all(
        """
        SELECT * FROM punishments
         WHERE is_active = 1 AND expires_at IS NOT NULL AND expires_at <= ?
         ORDER BY expires_at ASC LIMIT ?
        """,
        (to_iso(utcnow()), int(limit)),
    )
    return [Punishment.from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Статистика и активность
# ---------------------------------------------------------------------------
async def log_message(db: Database, chat_id: int, user_id: int) -> None:
    """Записать сообщение в журнал активности (для подсчёта за сутки)."""
    await db.execute(
        "INSERT INTO message_log (chat_id, user_id, created_at) VALUES (?, ?, ?)",
        (chat_id, user_id, to_iso(utcnow())),
    )


async def count_messages_since(db: Database, chat_id: int, since: datetime) -> int:
    """Количество сообщений в чате с указанного момента времени."""
    value = await db.fetch_value(
        "SELECT COUNT(*) FROM message_log WHERE chat_id = ? AND created_at >= ?",
        (chat_id, to_iso(since)),
    )
    return int(value)


async def count_daily_messages(db: Database, chat_id: int) -> int:
    """Количество сообщений в чате за последние сутки."""
    return await count_messages_since(db, chat_id, utcnow() - timedelta(hours=24))


async def cleanup_message_log(db: Database, keep_days: int = 7) -> int:
    """Удалить старые записи журнала активности и вернуть число удалённых."""
    threshold = to_iso(utcnow() - timedelta(days=max(1, int(keep_days))))
    before = await db.fetch_value(
        "SELECT COUNT(*) FROM message_log WHERE created_at < ?",
        (threshold,),
    )
    await db.execute("DELETE FROM message_log WHERE created_at < ?", (threshold,))
    return int(before)


async def get_chat_stats(db: Database, chat_id: int) -> dict[str, int]:
    """Собрать сводную статистику чата для карточки в ЛС."""
    members = await db.fetch_value(
        "SELECT COUNT(*) FROM chat_users WHERE chat_id = ?",
        (chat_id,),
    )
    messages = await db.fetch_value(
        "SELECT COALESCE(SUM(messages_count), 0) FROM chat_users WHERE chat_id = ?",
        (chat_id,),
    )
    punishment_counts = await get_chat_punishment_counts(db, chat_id)
    daily = await count_daily_messages(db, chat_id)
    return {
        "members": int(members),
        "messages": int(messages),
        "daily_messages": int(daily),
        "banned": punishment_counts["banned"],
        "muted": punishment_counts["muted"],
        "warned": punishment_counts["warned"],
    }


#: Столбцы локальной статистики участника, подмешиваемые к выборке чатов.
_CHAT_USER_STATS_SQL: Final[str] = """
               cu.messages_count   AS cu_messages,
               cu.warns_count      AS cu_warns,
               cu.is_banned        AS cu_banned,
               cu.is_muted         AS cu_muted,
               cu.is_member        AS cu_is_member,
               cu.joined_at        AS cu_joined,
               cu.last_message_at  AS cu_last_message
"""


def _chat_pair_from_row(
    row: Mapping[str, Any],
    user_id: int,
) -> tuple[ChatInfo, ChatUser]:
    """Собрать пару «чат + локальная статистика пользователя» из строки выборки.

    :param row: строка результата :func:`get_user_chats`.
    :param user_id: идентификатор пользователя.
    """
    chat = ChatInfo.from_row(row)
    has_stats = row["cu_messages"] is not None or row["cu_joined"] is not None
    chat_user = ChatUser(
        chat_id=chat.chat_id,
        user_id=user_id,
        messages_count=int(row["cu_messages"] or 0),
        warns_count=int(row["cu_warns"] or 0),
        is_banned=bool(row["cu_banned"]),
        is_muted=bool(row["cu_muted"]),
        # Владелец может быть в чате без записи в chat_users — считаем его участником.
        is_member=bool(row["cu_is_member"]) if has_stats else True,
        joined_at=models.from_iso(row["cu_joined"]),
        last_message_at=models.from_iso(row["cu_last_message"]),
    )
    return chat, chat_user


async def get_user_chats(db: Database, user_id: int) -> list[tuple[ChatInfo, ChatUser]]:
    """Все чаты пользователя вместе с его локальной статистикой.

    Возвращаются оба варианта связи с чатом, без дубликатов:
        * пользователь — **владелец** чата (``chats.owner_id = user_id``),
          даже если он ни разу не писал в группе;
        * пользователь — **участник** (есть запись в ``chat_users``).

    :param db: соединение с базой данных.
    :param user_id: идентификатор пользователя.
    """
    owner_rows = await db.fetch_all(
        f"""
        SELECT c.*, {_CHAT_USER_STATS_SQL}
          FROM chats c
          LEFT JOIN chat_users cu
                 ON cu.chat_id = c.chat_id AND cu.user_id = ?
         WHERE c.owner_id = ?
         ORDER BY cu.messages_count DESC, c.title ASC
        """,
        (user_id, user_id),
    )
    member_rows = await db.fetch_all(
        f"""
        SELECT c.*, {_CHAT_USER_STATS_SQL}
          FROM chat_users cu
          JOIN chats c ON c.chat_id = cu.chat_id
         WHERE cu.user_id = ?
           AND (c.owner_id IS NULL OR c.owner_id <> ?)
         ORDER BY cu.messages_count DESC, c.title ASC
        """,
        (user_id, user_id),
    )

    result: list[tuple[ChatInfo, ChatUser]] = []
    seen: set[int] = set()
    for row in (*owner_rows, *member_rows):
        chat_id = int(row["chat_id"])
        if chat_id in seen:  # страховка от дубликатов «владелец + участник»
            continue
        seen.add(chat_id)
        result.append(_chat_pair_from_row(row, user_id))

    logger.info(f"get_user_chats({user_id}): найдено {len(result)} чатов")
    logger.info(
        "get_user_chats(%s): как владелец — %s, как участник — %s.",
        user_id,
        len(owner_rows),
        len(member_rows),
    )
    return result


async def get_chat_members_with_warns(db: Database, chat_id: int) -> list[tuple[int, int]]:
    """Список ``(user_id, количество варнов)`` для участников чата."""
    rows = await db.fetch_all(
        """
        SELECT user_id, COUNT(*) AS cnt FROM warns
         WHERE chat_id = ?
         GROUP BY user_id
         ORDER BY cnt DESC
        """,
        (chat_id,),
    )
    return [(int(row["user_id"]), int(row["cnt"])) for row in rows]


async def get_top_active_users(
    db: Database,
    chat_id: int,
    limit: int = 10,
) -> list[tuple[int, int]]:
    """Топ активных участников чата по количеству сообщений."""
    rows = await db.fetch_all(
        """
        SELECT user_id, messages_count FROM chat_users
         WHERE chat_id = ?
         ORDER BY messages_count DESC
         LIMIT ?
        """,
        (chat_id, int(limit)),
    )
    return [(int(row["user_id"]), int(row["messages_count"])) for row in rows]


async def refresh_all_warns_counters(db: Database, chat_id: int) -> None:
    """Пересчитать счётчики варнов в ``chat_users`` по фактическим записям."""
    rows = await db.fetch_all(
        """
        SELECT user_id, COUNT(*) AS cnt FROM warns
         WHERE chat_id = ?
         GROUP BY user_id
        """,
        (chat_id,),
    )
    await db.execute("UPDATE chat_users SET warns_count = 0 WHERE chat_id = ?", (chat_id,))
    for row in rows:
        await db.execute(
            "UPDATE chat_users SET warns_count = ? WHERE chat_id = ? AND user_id = ?",
            (int(row["cnt"]), chat_id, int(row["user_id"])),
        )


async def get_known_chat_ids(db: Database) -> list[int]:
    """Список всех известных боту чатов (используется фоновыми задачами)."""
    rows = await db.fetch_all("SELECT chat_id FROM chats ORDER BY chat_id ASC")
    return [int(row["chat_id"]) for row in rows]


async def get_ban_mark_chats(db: Database, user_id: int) -> list[ChatInfo]:
    """Информация о чатах, в которых у пользователя есть метки бана."""
    marks = await get_ban_marks(db, user_id)
    chats: list[ChatInfo] = []
    for chat_id in marks:
        chat = await get_chat(db, chat_id)
        if chat is not None:
            chats.append(chat)
    return chats


async def get_user_global_stats(db: Database, user_id: int) -> dict[str, int]:
    """Глобальная статистика пользователя для карточки профиля."""
    profile = await get_user(db, user_id)
    warns_total = await db.fetch_value(
        "SELECT COUNT(*) FROM warns WHERE user_id = ?",
        (user_id,),
    )
    chats_total = await db.fetch_value(
        "SELECT COUNT(*) FROM chat_users WHERE user_id = ?",
        (user_id,),
    )
    return {
        "reputation": int(profile.reputation) if profile else 0,
        "total_messages": int(profile.total_messages) if profile else 0,
        "ban_marks": len(profile.ban_marks) if profile else 0,
        "warns_total": int(warns_total),
        "chats_total": int(chats_total),
    }


async def count_active_punishments_global(db: Database, user_id: int) -> int:
    """Количество активных наказаний пользователя во всех чатах."""
    value = await db.fetch_value(
        """
        SELECT COUNT(*) FROM punishments
         WHERE user_id = ? AND is_active = 1
           AND (expires_at IS NULL OR expires_at > ?)
        """,
        (user_id, to_iso(utcnow())),
    )
    return int(value)

# ---------------------------------------------------------------------------
# Антирейд
# ---------------------------------------------------------------------------
async def count_recent_joins(db: Database, chat_id: int, since: datetime) -> int:
    """Сколько участников присоединилось к чату с указанного момента.

    Используется антирейдом для ограничения потока входов.

    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param since: начало окна подсчёта (UTC).
    """
    value = await db.fetch_value(
        "SELECT COUNT(*) FROM chat_users WHERE chat_id = ? AND joined_at >= ?",
        (chat_id, to_iso(since)),
    )
    return int(value)


async def mark_joined(db: Database, chat_id: int, user_id: int) -> None:
    """Обновить время входа участника (для антирейда и сводок).

    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param user_id: идентификатор пользователя.
    """
    await ensure_chat_user(db, chat_id, user_id)
    await db.execute(
        "UPDATE chat_users SET joined_at = ?, is_member = 1 WHERE chat_id = ? AND user_id = ?",
        (to_iso(utcnow()), chat_id, user_id),
    )


# ---------------------------------------------------------------------------
# Жалобы пользователей (complaints)
# ---------------------------------------------------------------------------
async def create_complaint(
    db: Database,
    user_id: int,
    reason: str,
    description: str,
    *,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    photo_file_id: Optional[str] = None,
) -> int:
    """Создать жалобу и вернуть её номер.

    :param db: соединение с базой данных.
    :param user_id: кто жалуется.
    :param reason: ключ категории жалобы (см. ``config.COMPLAINT_REASONS``).
    :param description: суть жалобы.
    :param username: юзернейм автора из Telegram (если известен).
    :param first_name: имя автора из Telegram (если известно).
    :param photo_file_id: ``file_id`` прикреплённого фото (если есть).
    """
    complaint_id = await db.insert(
        """
        INSERT INTO complaints
            (user_id, user_username, user_first_name, reason, description,
             photo_file_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            first_name,
            reason,
            description,
            photo_file_id,
            config.COMPLAINT_STATUS_OPEN,
            to_iso(utcnow()),
        ),
    )
    logger.info("Создана жалоба #%s от пользователя %s (%s).", complaint_id, user_id, reason)
    return complaint_id


async def get_complaint(db: Database, complaint_id: int) -> Optional[Complaint]:
    """Получить жалобу по её номеру."""
    row = await db.fetch_one("SELECT * FROM complaints WHERE id = ?", (complaint_id,))
    return Complaint.from_row(row) if row is not None else None


async def get_complaints(
    db: Database,
    *,
    status: Optional[str] = None,
    user_id: Optional[int] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[Complaint]:
    """Список жалоб с фильтрами и пагинацией (сначала свежие).

    :param db: соединение с базой данных.
    :param status: фильтр по статусу (``open`` / ``accepted`` / ``rejected``).
    :param user_id: фильтр по автору жалобы.
    :param limit: сколько жалоб вернуть (``None`` — без ограничения).
    :param offset: сколько жалоб пропустить.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(int(user_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    sql = f"SELECT * FROM complaints {where} ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
    rows = await db.fetch_all(sql, tuple(params))
    return [Complaint.from_row(row) for row in rows]


async def count_complaints(
    db: Database,
    *,
    status: Optional[str] = None,
    user_id: Optional[int] = None,
) -> int:
    """Количество жалоб с фильтром (для пагинации и статистики)."""
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(int(user_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    value = await db.fetch_value(f"SELECT COUNT(*) FROM complaints {where}", tuple(params))
    return int(value)


async def get_user_complaint_counts(db: Database, user_id: int) -> dict[str, int]:
    """Статистика жалоб одного пользователя: всего, открытых, принятых, отклонённых."""
    rows = await db.fetch_all(
        "SELECT status, COUNT(*) AS cnt FROM complaints WHERE user_id = ? GROUP BY status",
        (user_id,),
    )
    counts = {
        config.COMPLAINT_STATUS_OPEN: 0,
        config.COMPLAINT_STATUS_ACCEPTED: 0,
        config.COMPLAINT_STATUS_REJECTED: 0,
    }
    for row in rows:
        counts[str(row["status"])] = int(row["cnt"])
    counts["total"] = sum(counts.values())
    return counts


async def close_complaint(
    db: Database,
    complaint_id: int,
    status: str,
    *,
    response: Optional[str] = None,
    closed_by: Optional[int] = None,
) -> bool:
    """Закрыть жалобу: записать решение, ответ и время закрытия.

    :returns: ``True``, если жалоба существовала и была обновлена.
    """
    complaint = await get_complaint(db, complaint_id)
    if complaint is None:
        return False
    reopening = str(status) == config.COMPLAINT_STATUS_OPEN
    await db.execute(
        """
        UPDATE complaints
           SET status = ?, admin_response = ?, closed_at = ?, closed_by = ?
         WHERE id = ?
        """,
        (
            str(status),
            response,
            None if reopening else to_iso(utcnow()),
            None if reopening else closed_by,
            int(complaint_id),
        ),
    )
    logger.info("Жалоба #%s закрыта: статус %s (админ %s).", complaint_id, status, closed_by)
    return True


# ---------------------------------------------------------------------------
# Баны от владельца бота (admin_bans)
# ---------------------------------------------------------------------------
async def add_admin_ban(
    db: Database,
    user_id: int,
    reason: Optional[str],
    banned_by: Optional[int],
) -> None:
    """Забанить пользователя в боте (повторный бан перезаписывает запись)."""
    await db.execute(
        """
        INSERT INTO admin_bans (user_id, reason, banned_at, banned_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            reason = excluded.reason,
            banned_at = excluded.banned_at,
            banned_by = excluded.banned_by
        """,
        (int(user_id), reason, to_iso(utcnow()), banned_by),
    )
    logger.info("Пользователь %s забанен в боте (админ %s).", user_id, banned_by)


async def remove_admin_ban(db: Database, user_id: int) -> bool:
    """Снять бан в боте.

    :returns: ``True``, если пользователь действительно был забанен.
    """
    if await get_admin_ban(db, user_id) is None:
        return False
    await db.execute("DELETE FROM admin_bans WHERE user_id = ?", (int(user_id),))
    logger.info("С пользователя %s снят бан в боте.", user_id)
    return True


async def get_admin_ban(db: Database, user_id: int) -> Optional[AdminBan]:
    """Получить запись о бане в боте (или ``None``)."""
    row = await db.fetch_one("SELECT * FROM admin_bans WHERE user_id = ?", (int(user_id),))
    return AdminBan.from_row(row) if row is not None else None


async def is_admin_banned(db: Database, user_id: int) -> bool:
    """Забанен ли пользователь в боте."""
    value = await db.fetch_value(
        "SELECT COUNT(*) FROM admin_bans WHERE user_id = ?", (int(user_id),)
    )
    return int(value) > 0


async def get_admin_bans(
    db: Database,
    *,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[AdminBan]:
    """Список банов в боте (свежие сверху) с пагинацией."""
    sql = "SELECT * FROM admin_bans ORDER BY banned_at DESC, user_id ASC"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
    rows = await db.fetch_all(sql, tuple(params))
    return [AdminBan.from_row(row) for row in rows]


async def count_admin_bans(db: Database) -> int:
    """Сколько пользователей забанено ботом."""
    value = await db.fetch_value("SELECT COUNT(*) FROM admin_bans")
    return int(value)


# ---------------------------------------------------------------------------
# Журнал действий владельца бота (admin_log)
# ---------------------------------------------------------------------------
async def log_admin_action(
    db: Database,
    action: str,
    target_id: Optional[int] = None,
    details: Optional[str] = None,
) -> None:
    """Записать действие владельца бота в журнал.

    :param db: соединение с базой данных.
    :param action: код действия (``ban_user``, ``detach_chat`` и т. п.).
    :param target_id: идентификатор пользователя или чата.
    :param details: человекочитаемые подробности для отображения в логе.
    """
    try:
        await db.insert(
            "INSERT INTO admin_log (action, target_id, details, created_at) VALUES (?, ?, ?, ?)",
            (str(action), target_id, details, to_iso(utcnow())),
        )
    except Exception:  # noqa: BLE001 - журнал не должен ломать само действие
        logger.error("Не удалось записать действие %r в журнал админа", action, exc_info=True)


async def get_admin_log(
    db: Database,
    *,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[AdminLogEntry]:
    """Последние записи журнала действий (свежие сверху)."""
    sql = "SELECT * FROM admin_log ORDER BY id DESC"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
    rows = await db.fetch_all(sql, tuple(params))
    return [AdminLogEntry.from_row(row) for row in rows]


async def count_admin_log(db: Database) -> int:
    """Сколько всего записей в журнале действий."""
    value = await db.fetch_value("SELECT COUNT(*) FROM admin_log")
    return int(value)


# ---------------------------------------------------------------------------
# Списки и отвязка чатов для админ-панели
# ---------------------------------------------------------------------------
def _user_filter_sql(
    *,
    max_reputation: Optional[int] = None,
    is_spammer: Optional[bool] = None,
    is_globally_banned: Optional[bool] = None,
) -> tuple[str, list[Any]]:
    """Собрать ``WHERE`` для выборки пользователей по «плохим» признакам.

    :param max_reputation: отобрать тех, у кого репутация строго ниже значения.
    :param is_spammer: метка «спамер» (``True`` — только спамеры).
    :param is_globally_banned: метка «глобально забанен».
    :returns: пара ``(условие, параметры)``.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if max_reputation is not None:
        clauses.append("reputation < ?")
        params.append(int(max_reputation))
    if is_spammer is not None:
        clauses.append("is_spammer = ?")
        params.append(1 if is_spammer else 0)
    if is_globally_banned is not None:
        clauses.append("is_globally_banned = ?")
        params.append(1 if is_globally_banned else 0)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


async def get_users_by_filter(
    db: Database,
    *,
    limit: Optional[int] = None,
    offset: int = 0,
    max_reputation: Optional[int] = None,
    is_spammer: Optional[bool] = None,
    is_globally_banned: Optional[bool] = None,
) -> list[UserProfile]:
    """Выборка пользователей по фильтрам админ-панели.

    :param db: соединение с базой данных.
    :param limit: сколько профилей вернуть (``None`` — без ограничения).
    :param offset: сколько профилей пропустить.
    :param max_reputation: репутация строго ниже значения (``None`` — любой).
    :param is_spammer: фильтр по метке спамера.
    :param is_globally_banned: фильтр по метке глобального бана.
    """
    where, params = _user_filter_sql(
        max_reputation=max_reputation,
        is_spammer=is_spammer,
        is_globally_banned=is_globally_banned,
    )
    sql = f"SELECT * FROM users {where} ORDER BY total_messages DESC, user_id ASC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
    rows = await db.fetch_all(sql, tuple(params))
    return [UserProfile.from_row(row) for row in rows]


async def count_users_by_filter(
    db: Database,
    *,
    max_reputation: Optional[int] = None,
    is_spammer: Optional[bool] = None,
    is_globally_banned: Optional[bool] = None,
) -> int:
    """Сколько пользователей подходит под фильтры админ-панели."""
    where, params = _user_filter_sql(
        max_reputation=max_reputation,
        is_spammer=is_spammer,
        is_globally_banned=is_globally_banned,
    )
    value = await db.fetch_value(f"SELECT COUNT(*) FROM users {where}", tuple(params))
    return int(value)


async def count_users(db: Database) -> int:
    """Сколько пользователей есть в базе."""
    value = await db.fetch_value("SELECT COUNT(*) FROM users")
    return int(value)


async def count_chats(db: Database) -> int:
    """Сколько чатов подключено к боту."""
    value = await db.fetch_value("SELECT COUNT(*) FROM chats")
    return int(value)


async def get_chats_page(
    db: Database,
    *,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[ChatInfo]:
    """Страница списка чатов (свежие сверху) для админ-панели."""
    sql = "SELECT * FROM chats ORDER BY created_at DESC, chat_id ASC"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
    rows = await db.fetch_all(sql, tuple(params))
    return [ChatInfo.from_row(row) for row in rows]


async def get_owner_ids(db: Database) -> list[int]:
    """Уникальные идентификаторы владельцев подключённых чатов (для рассылки)."""
    rows = await db.fetch_all(
        """
        SELECT DISTINCT owner_id FROM chats
         WHERE owner_id IS NOT NULL AND owner_id > 0
         ORDER BY owner_id ASC
        """
    )
    return [int(row["owner_id"]) for row in rows]


async def detach_chat(db: Database, chat_id: int) -> None:
    """Отвязать чат от бота: удалить чат и всю его локальную статистику.

    Стираются только данные, привязанные к чату (``chats``, ``chat_users``,
    ``warns``, ``punishments``); глобальные профили пользователей остаются.

    :param db: соединение с базой данных.
    :param chat_id: идентификатор отвязываемого чата.
    """
    await db.execute("DELETE FROM chats WHERE chat_id = ?", (int(chat_id),))
    await db.execute("DELETE FROM chat_users WHERE chat_id = ?", (int(chat_id),))
    await db.execute("DELETE FROM warns WHERE chat_id = ?", (int(chat_id),))
    await db.execute("DELETE FROM punishments WHERE chat_id = ?", (int(chat_id),))
    logger.info("Чат %s отвязан от бота (данные чата удалены).", chat_id)


# ---------------------------------------------------------------------------
# Глобальная статистика админ-панели
# ---------------------------------------------------------------------------
async def get_global_stats(db: Database) -> dict[str, int]:
    """Собрать сводную статистику бота для главного экрана админ-панели."""
    total_users = int(await db.fetch_value("SELECT COUNT(*) FROM users"))
    total_chats = int(await db.fetch_value("SELECT COUNT(*) FROM chats"))
    total_messages = int(
        await db.fetch_value("SELECT COALESCE(SUM(messages_count), 0) FROM chat_users")
    )
    total_bans = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM punishments WHERE type = ?", (config.TYPE_BAN,)
        )
    )
    total_mutes = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM punishments WHERE type = ?", (config.TYPE_MUTE,)
        )
    )
    total_warns = int(await db.fetch_value("SELECT COUNT(*) FROM warns"))
    total_complaints = int(await db.fetch_value("SELECT COUNT(*) FROM complaints"))
    open_complaints = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM complaints WHERE status = ?",
            (config.COMPLAINT_STATUS_OPEN,),
        )
    )
    admin_banned = int(await db.fetch_value("SELECT COUNT(*) FROM admin_bans"))
    return {
        "total_users": total_users,
        "total_chats": total_chats,
        "total_messages": total_messages,
        "total_bans": total_bans,
        "total_mutes": total_mutes,
        "total_warns": total_warns,
        "total_complaints": total_complaints,
        "open_complaints": open_complaints,
        "admin_banned": admin_banned,
    }


async def get_punishment_counts_since(db: Database, since: datetime) -> dict[str, int]:
    """Сколько банов, мутов и киков выдано с указанного момента.

    :param db: соединение с базой данных.
    :param since: начало окна подсчёта (UTC).
    """
    counts = {punishment_type: 0 for punishment_type in config.PUNISHMENT_TYPES}
    rows = await db.fetch_all(
        "SELECT type, COUNT(*) AS cnt FROM punishments WHERE created_at >= ? GROUP BY type",
        (to_iso(since),),
    )
    for row in rows:
        counts[str(row["type"])] = int(row["cnt"])
    return counts


async def count_warns_since(db: Database, since: datetime) -> int:
    """Сколько предупреждений выдано с указанного момента времени."""
    value = await db.fetch_value(
        "SELECT COUNT(*) FROM warns WHERE created_at >= ?", (to_iso(since),)
    )
    return int(value)


async def get_top_chats_since(
    db: Database,
    since: datetime,
    limit: int = 5,
) -> list[tuple[int, int]]:
    """Топ чатов по числу сообщений за период: список ``(chat_id, число)``."""
    rows = await db.fetch_all(
        """
        SELECT chat_id, COUNT(*) AS cnt FROM message_log
         WHERE created_at >= ?
         GROUP BY chat_id
         ORDER BY cnt DESC
         LIMIT ?
        """,
        (to_iso(since), int(limit)),
    )
    return [(int(row["chat_id"]), int(row["cnt"])) for row in rows]


async def get_top_users_by_messages(db: Database, limit: int = 5) -> list[tuple[int, int]]:
    """Топ пользователей по общему числу сообщений: ``(user_id, число)``."""
    rows = await db.fetch_all(
        "SELECT user_id, total_messages FROM users ORDER BY total_messages DESC LIMIT ?",
        (int(limit),),
    )
    return [(int(row["user_id"]), int(row["total_messages"] or 0)) for row in rows]


async def get_daily_message_counts(db: Database, since: datetime) -> dict[str, int]:
    """Сообщения по дням (ключ — ``ГГГГ-ММ-ДД``) начиная с указанного момента."""
    rows = await db.fetch_all(
        """
        SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS cnt FROM message_log
         WHERE created_at >= ?
         GROUP BY day
         ORDER BY day ASC
        """,
        (to_iso(since),),
    )
    return {str(row["day"]): int(row["cnt"]) for row in rows}


# ---------------------------------------------------------------------------
# Быстрые действия админ-панели
# ---------------------------------------------------------------------------
async def cleanup_admin_log(db: Database, keep_days: int = config.ADMIN_LOG_TTL_DAYS) -> int:
    """Удалить старые записи журнала админа.

    :param db: соединение с базой данных.
    :param keep_days: сколько последних дней журнала сохранить.
    :returns: сколько записей удалено.
    """
    threshold = to_iso(utcnow() - timedelta(days=max(1, int(keep_days))))
    removed = await db.fetch_value(
        "SELECT COUNT(*) FROM admin_log WHERE created_at < ?", (threshold,)
    )
    await db.execute("DELETE FROM admin_log WHERE created_at < ?", (threshold,))
    logger.info("Журнал админа очищен: удалено %s записей.", removed)
    return int(removed)


async def recount_warn_counters(db: Database) -> int:
    """Пересчитать счётчики варнов в ``chat_users`` по фактическим записям.

    :returns: сколько счётчиков отличалось от реального числа варнов.
    """
    mismatched = await db.fetch_value(
        """
        SELECT COUNT(*) FROM chat_users cu
         WHERE cu.warns_count <> (
             SELECT COUNT(*) FROM warns w
              WHERE w.chat_id = cu.chat_id AND w.user_id = cu.user_id
         )
        """
    )
    await db.execute(
        """
        UPDATE chat_users
           SET warns_count = (
               SELECT COUNT(*) FROM warns w
                WHERE w.chat_id = chat_users.chat_id
                  AND w.user_id = chat_users.user_id
           )
        """
    )
    logger.info("Счётчики варнов пересчитаны: изменено %s.", mismatched)
    return int(mismatched)


async def count_complaints_by_status(db: Database, user_id: Optional[int] = None) -> dict[str, int]:
    """Статистика жалоб по статусам (всех или одного пользователя).

    :returns: словарь с ключами ``total``, ``open``, ``accepted``, ``rejected``.
    """
    stats: dict[str, int] = {
        "total": 0,
        config.COMPLAINT_STATUS_OPEN: 0,
        config.COMPLAINT_STATUS_ACCEPTED: 0,
        config.COMPLAINT_STATUS_REJECTED: 0,
    }
    rows = await db.fetch_all(
        """
        SELECT status, COUNT(*) AS cnt FROM complaints
         WHERE ? IS NULL OR user_id = ?
         GROUP BY status
        """,
        (user_id, user_id),
    )
    for row in rows:
        stats[str(row["status"])] = int(row["cnt"])
        stats["total"] += int(row["cnt"])
    return stats
