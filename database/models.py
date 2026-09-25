"""Dataclass-модели таблиц базы данных YamoChan.

Все даты в проекте — только UTC. В SQLite даты хранятся строками ISO-8601,
поэтому здесь собраны функции сериализации/десериализации и модели с
фабриками :meth:`from_row`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Mapping, Optional, Sequence

#: Модуль экранирования — чистый (только stdlib), поэтому models остаётся
#: автономным: см. комментарий про UTC ниже.
from utils import html_utils

#: Часовой пояс проекта (дублируется из config, чтобы models оставался автономным).
UTC: Final[timezone] = timezone.utc

#: Пустой список меток банов в виде JSON.
EMPTY_MARKS: Final[str] = "[]"

#: Пустые настройки чата в виде JSON.
EMPTY_SETTINGS: Final[str] = "{}"


# ---------------------------------------------------------------------------
# Работа со временем и JSON
# ---------------------------------------------------------------------------
def utcnow() -> datetime:
    """Вернуть текущее время в UTC (timezone-aware)."""
    return datetime.now(tz=UTC)


def to_iso(moment: Optional[datetime]) -> Optional[str]:
    """Преобразовать datetime в ISO-8601 строку (UTC) или ``None``."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def from_iso(raw: Optional[str]) -> Optional[datetime]:
    """Разобрать ISO-8601 строку из БД и вернуть время в UTC.

    Некорректные значения не роняют бота — просто возвращается ``None``.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def future_iso(seconds: Optional[int]) -> Optional[datetime]:
    """Вернуть момент окончания наказания или ``None`` для бессрочного."""
    if seconds is None or seconds <= 0:
        return None
    return utcnow() + timedelta(seconds=int(seconds))


def _load_json_list(raw: Any) -> list[int]:
    """Аккуратно разобрать JSON-список идентификаторов чатов."""
    if isinstance(raw, (list, tuple)):
        data: Sequence[Any] = raw
    else:
        try:
            data = json.loads(raw or EMPTY_MARKS)
        except (TypeError, ValueError):
            return []
    result: list[int] = []
    for item in data:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _load_json_dict(raw: Any) -> dict[str, Any]:
    """Аккуратно разобрать JSON-объект настроек чата."""
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(raw or EMPTY_SETTINGS)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def dump_json(value: Any) -> str:
    """Сериализовать значение в компактный JSON для SQLite."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return EMPTY_SETTINGS


def load_json_list(raw: Any) -> list[int]:
    """Публичная обёртка над :func:`_load_json_list` (список меток банов)."""
    return _load_json_list(raw)


def load_json_dict(raw: Any) -> dict[str, Any]:
    """Публичная обёртка над :func:`_load_json_dict` (настройки чата)."""
    return _load_json_dict(raw)



def _escape(text: str) -> str:
    """Экранировать спецсимволы HTML для безопасной отправки сообщений."""
    return html_utils.h(text)


def escape_text(text: str) -> str:
    """Публичная обёртка над :func:`_escape` для использования в сервисах."""
    return _escape(text)


def flag(row: Mapping[str, Any], name: str, default: bool = False) -> bool:
    """Прочитать булев флаг из строки запроса.

    Колонки может не быть (старые базы без миграции или узкая выборка),
    поэтому вместо исключения возвращаем значение по умолчанию.

    :param row: строка результата SQL-запроса.
    :param name: имя колонки.
    :param default: что вернуть, если колонки нет.
    """
    try:
        return bool(row[name])
    except (KeyError, IndexError, TypeError):
        return default



# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class UserProfile:
    """Глобальный профиль пользователя (таблица ``users``)."""

    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    reputation: int = 0
    total_messages: int = 0
    is_globally_banned: bool = False
    is_spammer: bool = False
    ban_marks: list[int] = field(default_factory=list)
    created_at: Optional[datetime] = None

    @property
    def display_name(self) -> str:
        """Человекочитаемое имя пользователя без юзернейма.

        Юзернейм (``@username``) в текстах бота не показывается никогда:
        это публичный ник, раскрывать его в сообщениях нельзя.
        """
        return self.first_name or str(self.user_id)

    @property
    def mention(self) -> str:
        """HTML-упоминание пользователя."""
        return f'<a href="tg://user?id={self.user_id}">{_escape(self.display_name)}</a>'

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "UserProfile":
        """Собрать профиль из строки результата SQL-запроса."""
        return cls(
            user_id=int(row["user_id"]),
            username=row["username"],
            first_name=row["first_name"],
            reputation=int(row["reputation"] or 0),
            total_messages=int(row["total_messages"] or 0),
            is_globally_banned=bool(row["is_globally_banned"]),
            is_spammer=flag(row, "is_spammer"),
            ban_marks=_load_json_list(row["ban_marks"]),
            created_at=from_iso(row["created_at"]),
        )


@dataclass(slots=True)
class ChatInfo:
    """Информация о чате, где присутствует бот (таблица ``chats``)."""

    chat_id: int
    title: Optional[str] = None
    owner_id: Optional[int] = None
    owner_channel_id: Optional[int] = None
    settings: dict[str, Any] = field(default_factory=dict)
    members_count: int = 0
    created_at: Optional[datetime] = None

    @property
    def display_title(self) -> str:
        """Название чата или его идентификатор, если названия нет."""
        return self.title or f"Чат {self.chat_id}"

    @property
    def owner_label(self) -> str:
        """Идентификатор владельца чата в виде строки."""
        if self.owner_channel_id:
            return f"канал {self.owner_channel_id}"
        if self.owner_id:
            return str(self.owner_id)
        return "не определён"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ChatInfo":
        """Собрать информацию о чате из строки результата SQL-запроса."""
        return cls(
            chat_id=int(row["chat_id"]),
            title=row["title"],
            owner_id=int(row["owner_id"]) if row["owner_id"] is not None else None,
            owner_channel_id=(
                int(row["owner_channel_id"]) if row["owner_channel_id"] is not None else None
            ),
            settings=_load_json_dict(row["settings"]),
            members_count=int(row["members_count"] or 0),
            created_at=from_iso(row["created_at"]),
        )



@dataclass(slots=True)
class ChatUser:
    """Связка «чат — пользователь» (таблица ``chat_users``)."""

    chat_id: int
    user_id: int
    messages_count: int = 0
    warns_count: int = 0
    is_banned: bool = False
    is_muted: bool = False
    mute_until: Optional[datetime] = None
    ban_until: Optional[datetime] = None
    is_member: bool = True
    is_raid_suspect: bool = False
    joined_at: Optional[datetime] = None
    last_message_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ChatUser":
        """Собрать связку из строки результата SQL-запроса."""
        return cls(
            chat_id=int(row["chat_id"]),
            user_id=int(row["user_id"]),
            messages_count=int(row["messages_count"] or 0),
            warns_count=int(row["warns_count"] or 0),
            is_banned=bool(row["is_banned"]),
            is_muted=bool(row["is_muted"]),
            mute_until=from_iso(row["mute_until"]),
            ban_until=from_iso(row["ban_until"]),
            is_member=bool(row["is_member"]),
            is_raid_suspect=flag(row, "is_raid_suspect"),
            joined_at=from_iso(row["joined_at"]),
            last_message_at=from_iso(row["last_message_at"]),
        )


@dataclass(slots=True)
class Warn:
    """Предупреждение пользователя в чате (таблица ``warns``)."""

    id: Optional[int] = None
    chat_id: int = 0
    user_id: int = 0
    reason: Optional[str] = None
    issued_by: Optional[int] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Warn":
        """Собрать предупреждение из строки результата SQL-запроса."""
        return cls(
            id=int(row["id"]) if row["id"] is not None else None,
            chat_id=int(row["chat_id"]),
            user_id=int(row["user_id"]),
            reason=row["reason"],
            issued_by=int(row["issued_by"]) if row["issued_by"] is not None else None,
            created_at=from_iso(row["created_at"]),
        )


@dataclass(slots=True)
class Punishment:
    """Запись о наказании (таблица ``punishments``)."""

    id: Optional[int] = None
    chat_id: int = 0
    user_id: int = 0
    type: str = ""
    reason: Optional[str] = None
    duration: Optional[str] = None
    duration_seconds: Optional[int] = None
    issued_by: Optional[int] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    is_active: bool = True

    @property
    def type_title(self) -> str:
        """Человекочитаемое название типа наказания."""
        return {"ban": "бан", "mute": "мут", "kick": "кик"}.get(self.type, self.type)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Punishment":
        """Собрать наказание из строки результата SQL-запроса."""
        return cls(
            id=int(row["id"]) if row["id"] is not None else None,
            chat_id=int(row["chat_id"]),
            user_id=int(row["user_id"]),
            type=str(row["type"]),
            reason=row["reason"],
            duration=row["duration"],
            duration_seconds=(
                int(row["duration_seconds"]) if row["duration_seconds"] is not None else None
            ),
            issued_by=int(row["issued_by"]) if row["issued_by"] is not None else None,
            created_at=from_iso(row["created_at"]),
            expires_at=from_iso(row["expires_at"]),
            is_active=bool(row["is_active"]),
        )


@dataclass(slots=True)
class Ban:
    """Активный бан пользователя в чате (проекция таблицы ``punishments``)."""

    chat_id: int
    user_id: int
    reason: Optional[str] = None
    issued_by: Optional[int] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    @property
    def is_permanent(self) -> bool:
        """Является ли бан бессрочным."""
        return self.expires_at is None

    @classmethod
    def from_punishment(cls, punishment: Punishment) -> "Ban":
        """Построить модель бана из общей записи о наказании."""
        return cls(
            chat_id=punishment.chat_id,
            user_id=punishment.user_id,
            reason=punishment.reason,
            issued_by=punishment.issued_by,
            created_at=punishment.created_at,
            expires_at=punishment.expires_at,
        )


@dataclass(slots=True)
class Mute:
    """Активный мут пользователя в чате (проекция таблицы ``punishments``)."""

    chat_id: int
    user_id: int
    reason: Optional[str] = None
    issued_by: Optional[int] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    @property
    def is_permanent(self) -> bool:
        """Является ли мут бессрочным."""
        return self.expires_at is None

    @classmethod
    def from_punishment(cls, punishment: Punishment) -> "Mute":
        """Построить модель мута из общей записи о наказании."""
        return cls(
            chat_id=punishment.chat_id,
            user_id=punishment.user_id,
            reason=punishment.reason,
            issued_by=punishment.issued_by,
            created_at=punishment.created_at,
            expires_at=punishment.expires_at,
        )


# ---------------------------------------------------------------------------
# Админ-панель владельца бота
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Complaint:
    """Жалоба пользователя (таблица ``complaints``).

    Жалоба живёт в статусе ``open`` до решения владельца бота: он либо
    принимает её, либо отклоняет, и тогда записывает ответ (``admin_response``).
    """

    id: Optional[int] = None
    user_id: int = 0
    user_username: Optional[str] = None
    user_first_name: Optional[str] = None
    reason: str = ""
    description: str = ""
    photo_file_id: Optional[str] = None
    status: str = "open"
    admin_response: Optional[str] = None
    created_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    closed_by: Optional[int] = None

    @property
    def display_name(self) -> str:
        """Имя автора жалобы (юзернейм в текстах не показываем)."""
        return self.user_first_name or f"ID {self.user_id}"

    @property
    def is_open(self) -> bool:
        """Открыта ли жалоба (ещё ждёт решения)."""
        return self.status == "open"

    @property
    def has_photo(self) -> bool:
        """Есть ли у жалобы прикреплённое фото."""
        return bool(self.photo_file_id)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Complaint":
        """Собрать жалобу из строки результата SQL-запроса."""
        return cls(
            id=int(row["id"]) if row["id"] is not None else None,
            user_id=int(row["user_id"]),
            user_username=row["user_username"],
            user_first_name=row["user_first_name"],
            reason=str(row["reason"] or ""),
            description=str(row["description"] or ""),
            photo_file_id=row["photo_file_id"],
            status=str(row["status"] or "open"),
            admin_response=row["admin_response"],
            created_at=from_iso(row["created_at"]),
            closed_at=from_iso(row["closed_at"]),
            closed_by=int(row["closed_by"]) if row["closed_by"] is not None else None,
        )


@dataclass(slots=True)
class AdminBan:
    """Бан пользователя от владельца бота (таблица ``admin_bans``)."""

    user_id: int
    reason: Optional[str] = None
    banned_at: Optional[datetime] = None
    banned_by: Optional[int] = None

    @property
    def display_reason(self) -> str:
        """Причина бана для текстов (или «не указана»)."""
        return self.reason or "не указана"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AdminBan":
        """Собрать запись о бане из строки результата SQL-запроса."""
        return cls(
            user_id=int(row["user_id"]),
            reason=row["reason"],
            banned_at=from_iso(row["banned_at"]),
            banned_by=int(row["banned_by"]) if row["banned_by"] is not None else None,
        )


@dataclass(slots=True)
class AdminLogEntry:
    """Запись журнала действий владельца бота (таблица ``admin_log``)."""

    id: Optional[int] = None
    action: str = ""
    target_id: Optional[int] = None
    details: Optional[str] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AdminLogEntry":
        """Собрать запись журнала из строки результата SQL-запроса."""
        return cls(
            id=int(row["id"]) if row["id"] is not None else None,
            action=str(row["action"] or ""),
            target_id=int(row["target_id"]) if row["target_id"] is not None else None,
            details=row["details"],
            created_at=from_iso(row["created_at"]),
        )


