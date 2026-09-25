"""Админ-панель владельца бота: сбор статистики, тексты экранов и действия.

Модуль обслуживает раздел ``/adm`` (``.admin``, «админ»):

    * :func:`collect_global_stats` и :func:`collect_chat_overviews` собирают
      данные для экранов панели;
    * функции ``build_*_text`` формируют готовые тексты (панель, список чатов,
      карточка чата, профиль пользователя, статистика, баны, журнал, рассылка);
    * :func:`detach_chat`, :func:`ban_user_in_bot`, :func:`unban_user_in_bot` и
      :func:`send_broadcast` выполняют сами действия владельца.

Хендлеры (:mod:`yamochan.handlers.admin_panel`) остаются тонкими: они лишь
маршрутизируют кнопки и показывают готовые тексты. Все даты — UTC.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Final, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup

import config
from ..database import queries
from ..database.db import Database
from ..database.models import (
    AdminBan,
    AdminLogEntry,
    ChatInfo,
    UserProfile,
    escape_text,
    utcnow,
)
from . import profile as profile_service, richtext
from ..utils import telegram

logger = logging.getLogger(__name__)

#: Момент старта процесса — от него считается аптайм в панели.
STARTED_AT: Final[float] = time.monotonic()

#: Разделитель в текстах панели.
DIVIDER: Final[str] = "━━━━━━━━━━━━━━━━━━━━━━"

#: Названия действий для журнала админа.
ACTION_TITLES: Final[dict[str, str]] = {
    "ban_user": "🚫 Забанен пользователь",
    "unban_user": "✅ Разбанен пользователь",
    "detach_chat": "⚠️ Отвязан чат",
    "global_ban": "🔨 Глобальный бан",
    "global_unban": "🕊 Снят глобальный бан",
    "accept_complaint": "✅ Принята жалоба",
    "reject_complaint": "❌ Отклонена жалоба",
    "reopen_complaint": "🔄 Жалоба открыта заново",
    "reply_complaint": "💬 Ответ по жалобе",
    "broadcast": "📢 Рассылка",
    "message_user": "📨 Сообщение пользователю",
    "reset_admin_cache": "🔄 Сброшен кэш админов",
    "refresh_counters": "📊 Обновлены счётчики",
    "cleanup_logs": "🗑 Очищены старые логи",
    "backup_db": "💾 Бэкап базы данных",
}

#: Сокращения дней недели для графика активности.
WEEKDAYS: Final[tuple[str, ...]] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

#: Ключи настроек чата, которые показывает карточка (подпись, ключ, второй ключ).
CHAT_SETTING_FLAGS: Final[tuple[tuple[str, str, str], ...]] = (
    ("🔞 18+", "nsfw_commands", ""),
    ("🛡 Антирейд", "antiraid_enabled", "antiraid"),
    ("📞 Call", "call_enabled", "call_mode"),
    ("📜 Правила", "rules_enabled", ""),
    ("👋 Приветствие", "greeting_enabled", ""),
)


@dataclass(slots=True)
class ChatOverview:
    """Чат со своей статистикой и владельцем — строка списка админ-панели."""

    chat: ChatInfo
    stats: dict[str, int] = field(default_factory=dict)
    owner: Optional[UserProfile] = None

    @property
    def owner_name(self) -> str:
        """Имя владельца чата (или его идентификатор)."""
        if self.owner is not None:
            return self.owner.display_name
        if self.chat.owner_id:
            return str(self.chat.owner_id)
        return "не определён"


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------
def is_owner(user_id: Optional[int]) -> bool:
    """Является ли пользователь владельцем бота (он же супер-админ панели).

    :param user_id: идентификатор пользователя из Telegram.
    """
    owner_id = int(config.BOT_OWNER_ID or 0)
    return owner_id > 0 and user_id is not None and int(user_id) == owner_id


def make_bar(value: int, max_value: int, width: int = 10) -> str:
    """Построить «график» из блоков Unicode: ``████░░░░░░``.

    :param value: текущее значение.
    :param max_value: максимум шкалы.
    :param width: ширина полоски в символах.
    """
    try:
        maximum = int(max_value)
        current = int(value)
    except (TypeError, ValueError):
        return "░" * max(1, int(width))
    if maximum <= 0:
        filled = 0
    else:
        filled = round(current / maximum * width)
    filled = min(max(0, filled), int(width))
    return "█" * filled + "░" * (int(width) - filled)


def uptime_seconds() -> int:
    """Сколько секунд работает процесс бота."""
    return max(0, int(time.monotonic() - STARTED_AT))


def format_uptime(seconds: Optional[int] = None) -> str:
    """Человекочитаемый аптайм: ``1д 4ч 12м``."""
    total = uptime_seconds() if seconds is None else max(0, int(seconds))
    days, remainder = divmod(total, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}д {hours}ч {minutes}м"
    if hours:
        return f"{hours}ч {minutes}м"
    if minutes:
        return f"{minutes}м {secs}с"
    return f"{secs}с"


def format_moment(moment: Optional[datetime]) -> str:
    """Дата и время в формате ``ДД.ММ.ГГГГ ЧЧ:ММ (UTC)``."""
    return profile_service.format_moment(moment)


def short_moment(moment: Optional[datetime]) -> str:
    """Короткая дата для журнала: ``ДД.ММ ЧЧ:ММ``."""
    if moment is None:
        return "—"
    return moment.strftime("%d.%m %H:%M")


def state_icon(value: object) -> str:
    """Короткая иконка состояния: ``✅`` или ``❌``."""
    return "✅" if bool(value) else "❌"


def page_count(total: int, page_size: int) -> int:
    """Сколько страниц займёт ``total`` элементов при заданном размере страницы."""
    size = max(1, int(page_size))
    items = max(0, int(total))
    return max(1, (items + size - 1) // size)


def clamp_page(page: int, pages: int) -> int:
    """Привести номер страницы к допустимому диапазону."""
    try:
        value = int(page)
    except (TypeError, ValueError):
        value = 1
    return min(max(1, value), max(1, int(pages)))


def describe_log_entry(entry: AdminLogEntry) -> str:
    """Строка журнала: ``14.05 12:30 — ⚠️ Отвязан чат «Имя»``."""
    title = ACTION_TITLES.get(entry.action, f"⚙️ {entry.action}")
    details = (entry.details or "").strip()
    body = f"{title}: {escape_text(details)}" if details else title
    return f"{short_moment(entry.created_at)} — {body}"


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Согласовать существительное с числом (``1 чат`` / ``2 чата`` / ``5 чатов``)."""
    value = abs(int(count)) % 100
    if 11 <= value <= 14:
        return many
    value %= 10
    if value == 1:
        return one
    if 2 <= value <= 4:
        return few
    return many


# ---------------------------------------------------------------------------
# Сбор данных для экранов
# ---------------------------------------------------------------------------
async def collect_global_stats(db: Database) -> dict[str, int]:
    """Сводная статистика бота (и аптайм) для главного экрана панели."""
    stats = await queries.get_global_stats(db)
    stats["uptime"] = uptime_seconds()
    return stats


async def collect_chat_overviews(
    db: Database,
    *,
    page: int = 1,
    page_size: int = config.ADMIN_PAGE_SIZE,
) -> tuple[list[ChatOverview], int, int]:
    """Страница подключённых чатов со статистикой.

    :returns: ``(строки, всего чатов, всего страниц)``.
    """
    total = await queries.count_chats(db)
    pages = page_count(total, page_size)
    page = clamp_page(page, pages)
    chats = await queries.get_chats_page(db, limit=page_size, offset=(page - 1) * page_size)

    overviews: list[ChatOverview] = []
    for chat in chats:
        try:
            stats = await queries.get_chat_stats(db, chat.chat_id)
        except Exception:  # noqa: BLE001 - один чат не должен ломать список
            logger.error("Не удалось собрать статистику чата %s", chat.chat_id, exc_info=True)
            stats = {}
        owner: Optional[UserProfile] = None
        if chat.owner_id:
            try:
                owner = await queries.get_user(db, chat.owner_id)
            except Exception:  # noqa: BLE001
                logger.error("Не удалось прочитать владельца %s", chat.owner_id, exc_info=True)
        overviews.append(ChatOverview(chat=chat, stats=stats, owner=owner))
    return overviews, total, pages


async def collect_banned(
    db: Database,
    *,
    page: int = 1,
    page_size: int = config.ADMIN_PAGE_SIZE,
) -> tuple[list[tuple[AdminBan, Optional[UserProfile]]], int, int]:
    """Страница списка забаненных ботом: пары ``(запись, профиль)``."""
    total = await queries.count_admin_bans(db)
    pages = page_count(total, page_size)
    page = clamp_page(page, pages)
    bans = await queries.get_admin_bans(db, limit=page_size, offset=(page - 1) * page_size)
    profiles = await queries.get_users_by_ids(db, [ban.user_id for ban in bans])
    return [(ban, profiles.get(ban.user_id)) for ban in bans], total, pages


async def collect_admin_log(
    db: Database,
    *,
    page: int = 1,
    page_size: int = config.ADMIN_LOG_PAGE_SIZE,
) -> tuple[list[AdminLogEntry], int, int]:
    """Страница журнала действий владельца бота."""
    total = await queries.count_admin_log(db)
    pages = page_count(total, page_size)
    page = clamp_page(page, pages)
    entries = await queries.get_admin_log(db, limit=page_size, offset=(page - 1) * page_size)
    return entries, total, pages


# ---------------------------------------------------------------------------
# Главный экран панели
# ---------------------------------------------------------------------------
def build_panel_text(stats: dict[str, int]) -> str:
    """Текст главного экрана админ-панели.

    Если есть открытые жалобы, сверху появляется напоминание с их числом.

    :param stats: результат :func:`collect_global_stats`.
    """
    lines = [f"🔧 Панель управления {config.BOT_NAME}", DIVIDER, ""]
    open_complaints = int(stats.get("open_complaints", 0))
    if open_complaints:
        lines.extend(
            [
                f"🔔 У вас {open_complaints} "
                f"{_plural(open_complaints, 'новая жалоба', 'новые жалобы', 'новых жалоб')}!",
                "",
            ]
        )
    lines.extend(
        [
            "📊 Общая статистика:",
            f"  👥 Пользователей в базе: {profile_service.format_number(stats.get('total_users', 0))}",
            f"  💬 Чатов подключено: {profile_service.format_number(stats.get('total_chats', 0))}",
            f"  📨 Сообщений обработано: {profile_service.format_number(stats.get('total_messages', 0))}",
            f"  🔨 Банов выдано: {profile_service.format_number(stats.get('total_bans', 0))}",
            f"  🤫 Мутов выдано: {profile_service.format_number(stats.get('total_mutes', 0))}",
            f"  ⚠️ Варнов выдано: {profile_service.format_number(stats.get('total_warns', 0))}",
            f"  📩 Жалоб всего: {profile_service.format_number(stats.get('total_complaints', 0))}",
            f"  📩 Открытых жалоб: {profile_service.format_number(open_complaints)}",
            f"  🚫 Забанено ботом: {profile_service.format_number(stats.get('admin_banned', 0))}",
            "",
            f"  🕐 Аптайм: {format_uptime(stats.get('uptime'))}",
            f"  📦 Версия: {config.BOT_VERSION}",
            DIVIDER,
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Быстрые действия
# ---------------------------------------------------------------------------
def build_quick_actions_text() -> str:
    """Текст подменю «Быстрые действия»."""
    return (
        "⚡ Быстрые действия\n\n"
        "Здесь служебные операции: сброс кэша админов, пересчёт счётчиков,\n"
        "чистка журнала и резервная копия базы.\n\n"
        f"🗑 Старые логи — записи журнала старше {config.ADMIN_LOG_TTL_DAYS} дней."
    )


def build_cache_reset_text(chats: int) -> str:
    """Результат сброса кэша админов.

    :param chats: сколько чатов было в кэше.
    """
    return (
        "✅ Кэш админов сброшен.\n"
        f"Забыто чатов: {chats}. При следующей проверке права спрошу заново~"
    )


def build_counters_refreshed_text(result: dict[str, int]) -> str:
    """Результат пересчёта счётчиков.

    :param result: словарь из :func:`refresh_counters`.
    """
    return (
        "✅ Счётчики обновлены.\n\n"
        f"📊 Счётчиков варнов исправлено: {result.get('warns_fixed', 0)}\n"
        f"👥 Чатов с обновлённым числом участников: {result.get('members_updated', 0)}\n"
        f"💬 Чатов проверено: {result.get('chats_checked', 0)}"
    )


def build_logs_cleared_text(removed: int) -> str:
    """Результат очистки старых записей журнала."""
    return (
        "🗑 Старые логи удалены.\n"
        f"Удалено записей: {removed} (старше {config.ADMIN_LOG_TTL_DAYS} дней)."
    )


def build_backup_caption() -> str:
    """Подпись к файлу резервной копии базы."""
    return f"💾 Бэкап базы данных {config.BOT_NAME} — {format_moment(utcnow())}"


def build_backup_error_text() -> str:
    """Текст ошибки при создании бэкапа."""
    return "❌ Не удалось отправить бэкап~ Проверь логи и права на файл базы."


# ---------------------------------------------------------------------------
# Список чатов и карточка чата
# ---------------------------------------------------------------------------
def build_chats_list_text(
    overviews: Sequence[ChatOverview],
    total: int,
    page: int,
    pages: int,
) -> str:
    """Текст экрана «Список чатов».

    :param overviews: чаты текущей страницы.
    :param total: всего подключённых чатов.
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    lines = [
        "💬 Подключённые чаты",
        "",
        f"Всего: {profile_service.format_number(total)} "
        f"{_plural(total, 'чат', 'чата', 'чатов')}",
    ]
    if not overviews:
        lines.extend(["", "Пока ни одного чата~ Добавь меня в группу! 🌸"])
        return "\n".join(lines)

    offset = (page - 1) * len(overviews)
    for index, overview in enumerate(overviews, start=offset + 1):
        chat = overview.chat
        stats = overview.stats
        lines.extend(
            [
                "",
                f"{index}. {escape_text(chat.display_title)}",
                f"   👤 Владелец: {escape_text(overview.owner_name)}"
                f" (ID: {chat.owner_id or '?'})",
                f"   👥 Участников: {profile_service.format_number(stats.get('members', 0))}",
                f"   📨 Сообщений: {profile_service.format_number(stats.get('messages', 0))}",
                f"   🔨 Банов: {stats.get('banned', 0)} | 🤫 Мутов: {stats.get('muted', 0)}",
                f"   📅 Подключён: {format_moment(chat.created_at)}",
            ]
        )
    if pages > 1:
        lines.extend(["", f"Страница {page}/{pages}"])
    return "\n".join(lines)


def build_chat_card_text(
    chat: ChatInfo,
    stats: dict[str, int],
    settings: dict[str, Any],
    owner: Optional[UserProfile] = None,
) -> str:
    """Текст карточки чата для админ-панели.

    :param chat: информация о чате из базы.
    :param stats: результат :func:`queries.get_chat_stats`.
    :param settings: настройки чата (тумблеры).
    :param owner: профиль владельца (если известен).
    """
    members = int(chat.members_count or stats.get("members", 0))
    owner_name = owner.display_name if owner is not None else str(chat.owner_id or "?")
    lines = [
        "💬 Карточка чата",
        "",
        f"📛 Название: {escape_text(chat.display_title)}",
        f"🆔 ID: <code>{chat.chat_id}</code>",
        f"👤 Владелец: {escape_text(owner_name)} (ID: {chat.owner_id or '?'})",
        f"👥 Участников: {profile_service.format_number(members)}",
        f"📨 Сообщений всего: {profile_service.format_number(stats.get('messages', 0))}",
        f"📈 За 24ч: {profile_service.format_number(stats.get('daily_messages', 0))} сообщений",
        "",
        f"🔨 В бане: {stats.get('banned', 0)}",
        f"🤫 В муте: {stats.get('muted', 0)}",
        f"⚠️ С варнами: {stats.get('warned', 0)}",
        "",
        "⚙️ Настройки:",
    ]
    for title, key, alias in CHAT_SETTING_FLAGS:
        value = settings.get(key)
        if value is None and alias:
            value = settings.get(alias)
        lines.append(f"  {title}: {state_icon(value)}")
    lines.extend(["", f"📅 Подключён: {format_moment(chat.created_at)}"])
    return "\n".join(lines)


def build_detach_confirm_text(title: str) -> str:
    """Текст подтверждения отвязки чата."""
    return (
        f"⚠️ Отвязать чат «{escape_text(title)}»?\n\n"
        "Бот выйдет из чата и отправит владельцу\n"
        "уведомление о проверке СБ.\n\n"
        "Это действие необратимо!"
    )


def build_detached_done_text() -> str:
    """Сообщение админу после успешной отвязки чата."""
    return "✅ Чат отвязан успешно."


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
def build_users_text(
    total: int,
    bad_reputation: int,
    spammers: int,
    globally_banned: int,
) -> str:
    """Текст экрана «Пользователи».

    :param total: всего пользователей в базе.
    :param bad_reputation: сколько с репутацией ниже ``config.BAD_REPUTATION_THRESHOLD``.
    :param spammers: сколько помечено спамерами.
    :param globally_banned: сколько с глобальным баном.
    """
    return "\n".join(
        [
            "👥 Пользователи",
            "",
            f"Всего в базе: {profile_service.format_number(total)}",
            # «&lt;» вместо «<»: иначе Telegram считает это началом тега.
            f"С плохой репутацией (&lt; {config.BAD_REPUTATION_THRESHOLD}): "
            f"{profile_service.format_number(bad_reputation)}",
            f"Спамеры: {profile_service.format_number(spammers)}",
            f"Глобально забанены: {profile_service.format_number(globally_banned)}",
        ]
    )


def build_user_list_text(
    title: str,
    users: Sequence[UserProfile],
    page: int,
    pages: int,
) -> str:
    """Текст списка пользователей по фильтру панели.

    :param title: заголовок списка (например «🔴 Плохая репутация»).
    :param users: пользователи текущей страницы.
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    lines = [title, ""]
    if not users:
        lines.append("Здесь пусто~ ✨")
        return "\n".join(lines)

    offset = (page - 1) * len(users)
    for index, user in enumerate(users, start=offset + 1):
        lines.append(
            f"{index}. {escape_text(user.display_name)} (ID: <code>{user.user_id}</code>) — "
            f"реп. {user.reputation}, сообщ. {profile_service.format_number(user.total_messages)}"
        )
    if pages > 1:
        lines.extend(["", f"Страница {page}/{pages}"])
    return "\n".join(lines)


def build_admin_user_profile_text(
    profile: UserProfile,
    stats: dict[str, int],
    chats: Sequence[tuple[ChatInfo, Any]],
    complaint_counts: dict[str, int],
    admin_ban: Optional[AdminBan],
) -> str:
    """Текст админского профиля пользователя.

    :param profile: профиль пользователя из базы.
    :param stats: результат :func:`queries.get_user_global_stats`.
    :param chats: пары ``(чат, локальная статистика)`` из :func:`queries.get_user_chats`.
    :param complaint_counts: результат :func:`queries.get_user_complaint_counts`.
    :param admin_ban: запись о бане в боте (``None`` — не забанен).
    """
    reputation = int(stats.get("reputation", profile.reputation))
    username = profile.username or "отсутствует"
    lines = [
        "👤 Профиль пользователя (АДМИН)",
        "",
        f"📛 Имя: {escape_text(profile.display_name)}",
        f"🔗 Username: {('@' + escape_text(username)) if profile.username else username}",
        f"🆔 ID: <code>{profile.user_id}</code>",
        "",
        f"📊 Репутация: {reputation} {profile_service.reputation_scale(reputation)}",
        f"💬 Всего сообщений: {profile_service.format_number(stats.get('total_messages', 0))}",
        f"🏷 Метки банов: {stats.get('ban_marks', 0)} чатов",
        f"🚫 Спамер: {state_icon(profile.is_spammer)}",
        f"🔨 Глобальный бан: {state_icon(profile.is_globally_banned)}",
        f"🔧 Забанен ботом: {state_icon(admin_ban is not None)}",
    ]

    lines.append("")
    if chats:
        lines.append("💬 Состоит в чатах:")
        for chat, chat_user in chats[:10]:
            lines.append(
                f"  • {escape_text(chat.display_title)} — "
                f"{profile_service.format_number(getattr(chat_user, 'messages_count', 0))} сообщений, "
                f"{getattr(chat_user, 'warns_count', 0)} варнов"
            )
        if len(chats) > 10:
            lines.append(f"  • …и ещё {len(chats) - 10}")
    else:
        lines.append("💬 Состоит в чатах: нет данных~")

    lines.extend(
        [
            "",
            "📩 Жалобы:",
            f"  Отправлено: {complaint_counts.get('total', 0)}",
            f"  Открытых: {complaint_counts.get(config.COMPLAINT_STATUS_OPEN, 0)}",
            f"  Принятых: {complaint_counts.get(config.COMPLAINT_STATUS_ACCEPTED, 0)}",
            f"  Отклонённых: {complaint_counts.get(config.COMPLAINT_STATUS_REJECTED, 0)}",
            "",
            f"📅 В базе с: {format_moment(profile.created_at)}",
        ]
    )
    return "\n".join(lines)


def build_ban_confirm_text(name: str, user_id: int) -> str:
    """Текст подтверждения бана пользователя в боте."""
    return (
        f"🚫 Забанить пользователя {escape_text(name)} (ID: <code>{user_id}</code>)?\n\n"
        "Бот запретит ему:\n"
        "• Пользоваться ботом в ЛС\n"
        "• Добавлять бота в свои чаты\n"
        "• Если он владелец чатов — все будут отвязаны\n\n"
        'Укажи причину (или нажми "Без причины"):'
    )


# ---------------------------------------------------------------------------
# Забаненные, журнал и поиск
# ---------------------------------------------------------------------------
def build_banned_list_text(
    items: Sequence[tuple[AdminBan, Optional[UserProfile]]],
    total: int,
    page: int,
    pages: int,
) -> str:
    """Текст экрана «Забаненные».

    :param items: пары ``(запись о бане, профиль)`` текущей страницы.
    :param total: всего забаненных ботом.
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    lines = [
        "🚫 Забаненные пользователи",
        "",
        f"Всего: {profile_service.format_number(total)}",
    ]
    if not items:
        lines.extend(["", "Список пуст~ ✨"])
        return "\n".join(lines)

    offset = (page - 1) * len(items)
    for index, (ban, profile) in enumerate(items, start=offset + 1):
        name = profile.display_name if profile is not None else f"ID {ban.user_id}"
        lines.extend(
            [
                "",
                f"{index}. {escape_text(name)} (ID: <code>{ban.user_id}</code>)",
                f"   📅 Бан: {format_moment(ban.banned_at)}",
                f"   📝 Причина: {escape_text(ban.display_reason)}",
            ]
        )
    if pages > 1:
        lines.extend(["", f"Страница {page}/{pages}"])
    return "\n".join(lines)


def build_log_text(
    entries: Sequence[AdminLogEntry],
    total: int,
    page: int,
    pages: int,
) -> str:
    """Текст экрана «Лог действий».

    :param entries: записи текущей страницы (свежие сверху).
    :param total: всего записей в журнале.
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    lines = ["📋 Лог действий", ""]
    if not entries:
        lines.append("Журнал пока пуст~ ✨")
        return "\n".join(lines)

    offset = (page - 1) * len(entries) + 1
    for index, entry in enumerate(entries, start=offset):
        lines.append(f"{index}. {describe_log_entry(entry)}")
    lines.extend(["", f"Всего записей: {profile_service.format_number(total)}"])
    if pages > 1:
        lines.append(f"Страница {page}/{pages}")
    return "\n".join(lines)


def build_search_prompt_text() -> str:
    """Текст запроса поиска пользователя в админ-панели."""
    return (
        "🔍 Поиск пользователя\n\n"
        "Отправь мне:\n"
        "• ID пользователя (числом)\n"
        "• @username\n\n"
        "Я найду его в своей базе~\n\n"
        "Для отмены отправь /cancel"
    )


def build_search_not_found_text() -> str:
    """Текст, если пользователя нет в базе."""
    return "Пользователь не найден в базе~ 🤔"


# ---------------------------------------------------------------------------
# Расширенная статистика
# ---------------------------------------------------------------------------
def build_activity_lines(daily: dict[str, int], days: int = config.ADMIN_ACTIVITY_DAYS) -> list[str]:
    """Строки «графика» активности по дням с полосками из блоков.

    :param daily: результат :func:`queries.get_daily_message_counts`.
    :param days: сколько последних дней показать.
    """
    today = utcnow().date()
    rows: list[tuple[str, int]] = []
    for offset in range(max(1, int(days)) - 1, -1, -1):
        day = today - timedelta(days=offset)
        rows.append((WEEKDAYS[day.weekday()], int(daily.get(day.isoformat(), 0))))
    maximum = max([value for _, value in rows] + [1])
    return [
        f"{title}: {make_bar(value, maximum)} {value}" for title, value in rows
    ]


def build_stats_text(
    daily: dict[str, int],
    top_chats: Sequence[tuple[str, int]],
    top_users: Sequence[tuple[str, int]],
    punishment_counts: dict[str, int],
    warns_count: int,
    antiraid_hits: int,
    antispam_hits: int,
) -> str:
    """Текст экрана «Расширенная статистика».

    :param daily: сообщения по дням (``ГГГГ-ММ-ДД`` → число).
    :param top_chats: ``(название чата, сообщений за день)``.
    :param top_users: ``(имя пользователя, сообщений всего)``.
    :param punishment_counts: количество банов/мутов/киков за неделю.
    :param warns_count: количество варнов за неделю (таблица ``warns``).
    :param antiraid_hits: срабатываний антирейда с момента запуска.
    :param antispam_hits: срабатываний антиспама с момента запуска.
    """
    lines = [
        "📊 Расширенная статистика",
        "",
        f"📈 Активность за последние {config.ADMIN_ACTIVITY_DAYS} дней:",
    ]
    lines.extend(build_activity_lines(daily))

    lines.extend(["", "📊 Топ-5 активных чатов:"])
    if top_chats:
        for index, (title, count) in enumerate(top_chats, start=1):
            lines.append(f"{index}. {escape_text(title)} — {count} сообщений/день")
    else:
        lines.append("Пока нет данных~")

    lines.extend(["", "📊 Топ-5 активных юзеров:"])
    if top_users:
        for index, (name, count) in enumerate(top_users, start=1):
            lines.append(
                f"{index}. {escape_text(name)} — {profile_service.format_number(count)} сообщений"
            )
    else:
        lines.append("Пока нет данных~")

    lines.extend(
        [
            "",
            "🔨 Наказания за неделю:",
            f"  Банов: {punishment_counts.get(config.TYPE_BAN, 0)}",
            f"  Мутов: {punishment_counts.get(config.TYPE_MUTE, 0)}",
            f"  Варнов: {warns_count}",
            f"  Киков: {punishment_counts.get(config.TYPE_KICK, 0)}",
            "",
            f"🛡 Антирейд срабатываний: {antiraid_hits}",
            f"📨 Антиспам срабатываний: {antispam_hits}",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Рассылка
# ---------------------------------------------------------------------------
def build_broadcast_menu_text() -> str:
    """Текст экрана «Рассылка» — выбор получателей."""
    return "📢 Рассылка\n\nВыбери тип:"


def build_broadcast_prompt_text() -> str:
    """Текст запроса содержимого рассылки."""
    return (
        "📢 Отправь сообщение для рассылки.\n"
        "Можно с фото, форматированием, премиум эмодзи.\n\n"
        "Для отмены отправь /cancel"
    )


def build_broadcast_preview_text(content: richtext.RichContent, targets: int, kind: str) -> str:
    """Текст превью рассылки с подтверждением.

    :param content: содержимое рассылки (текст, сущности, фото).
    :param targets: сколько получателей.
    :param kind: ``chats`` — во все чаты, ``owners`` — владельцам в ЛС.
    """
    if kind == "owners":
        target_line = f"Отправить в {targets} личных сообщений владельцам?"
    else:
        target_line = f"Отправить в {targets} {_plural(targets, 'чат', 'чата', 'чатов')}?"
    body = (content.text or "").strip() or "🖼 (только фото)"
    return f"📢 Превью рассылки:\n\n{body}\n\n{target_line}"


def build_broadcast_progress_text(sent: int, errors: int, total: int) -> str:
    """Текст прогресса рассылки (обновляется каждые несколько отправок)."""
    return f"📢 Рассылка...\nОтправлено: {sent}/{total}\nОшибок: {errors}"


def build_broadcast_done_text(sent: int, errors: int) -> str:
    """Текст завершения рассылки."""
    return f"✅ Рассылка завершена!\nОтправлено: {sent}\nОшибок: {errors}"


def build_broadcast_cancelled_text() -> str:
    """Текст отмены рассылки."""
    return "Рассылка отменена~ 🌸"


def broadcast_targets_title(kind: str, count: int) -> str:
    """Подпись кнопки выбора получателей рассылки."""
    if kind == "owners":
        return f"👤 Всем владельцам в ЛС ({count})"
    return f"💬 Во все чаты ({count})"


def broadcast_kind_title(kind: str) -> str:
    """Название типа рассылки для журнала действий."""
    return "владельцам в ЛС" if kind == "owners" else "во все чаты"


# ---------------------------------------------------------------------------
# Действия владельца
# ---------------------------------------------------------------------------
async def notify_user(
    bot: Bot,
    user_id: int,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """Отправить пользователю личное сообщение.

    :param bot: экземпляр бота.
    :param user_id: кому пишем.
    :param text: текст сообщения.
    :param reply_markup: клавиатура (необязательно).
    :returns: ``True``, если сообщение доставлено.
    """
    try:
        await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)
        return True
    except TelegramAPIError as exc:
        logger.info("Не удалось написать пользователю %s: %s", user_id, exc)
    except Exception:  # noqa: BLE001 - уведомление не должно ронять бота
        logger.error("Неожиданная ошибка уведомления %s", user_id, exc_info=True)
    return False


async def detach_chat(
    bot: Bot,
    db: Database,
    chat_id: int,
    *,
    title: Optional[str] = None,
    owner_id: Optional[int] = None,
    notify_chat: bool = True,
    notify_owner: bool = True,
    actor_id: Optional[int] = None,
) -> bool:
    """Отвязать чат: предупредить чат и владельца, выйти и удалить данные.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param title: название чата (если уже известно).
    :param owner_id: идентификатор владельца (если уже известен).
    :param notify_chat: писать ли предупреждение в сам чат.
    :param notify_owner: писать ли владельцу в личку.
    :param actor_id: кто выполнил действие (для журнала).
    :returns: ``True``, если бот успешно покинул чат.
    """
    chat: Optional[ChatInfo] = None
    if title is None or owner_id is None:
        try:
            chat = await queries.get_chat(db, chat_id)
        except Exception:  # noqa: BLE001
            logger.error("Не удалось прочитать чат %s перед отвязкой", chat_id, exc_info=True)

    chat_title = title or (chat.display_title if chat is not None else f"Чат {chat_id}")
    owner = owner_id if owner_id is not None else (chat.owner_id if chat is not None else None)

    if notify_chat:
        try:
            await bot.send_message(chat_id=chat_id, text=config.ADMIN_DETACH_CHAT_MESSAGE)
        except TelegramAPIError as exc:
            logger.info("Предупреждение в чат %s не доставлено: %s", chat_id, exc)
        except Exception:  # noqa: BLE001
            logger.error("Ошибка предупреждения чата %s", chat_id, exc_info=True)

    if notify_owner and owner:
        await notify_user(
            bot,
            int(owner),
            config.ADMIN_DETACH_OWNER_MESSAGE.format(
                bot=config.BOT_NAME, title=escape_text(chat_title)
            ),
        )

    left = True
    try:
        await bot.leave_chat(chat_id=chat_id)
    except TelegramAPIError as exc:
        left = False
        logger.error("Не удалось выйти из чата %s: %s", chat_id, exc)
    except Exception:  # noqa: BLE001
        left = False
        logger.error("Неожиданная ошибка выхода из чата %s", chat_id, exc_info=True)

    try:
        await queries.detach_chat(db, chat_id)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось удалить данные чата %s", chat_id, exc_info=True)

    await queries.log_admin_action(db, "detach_chat", chat_id, f"«{chat_title}» (ID {chat_id})")
    logger.warning("Чат %s «%s» отвязан (админ %s).", chat_id, chat_title, actor_id)
    return left


async def ban_user_in_bot(
    bot: Bot,
    db: Database,
    user_id: int,
    reason: Optional[str],
    *,
    banned_by: Optional[int],
    name: Optional[str] = None,
) -> list[int]:
    """Забанить пользователя в боте и отвязать все его чаты.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param user_id: кого баним.
    :param reason: причина бана (``None`` — без причины).
    :param banned_by: кто забанил (владелец бота).
    :param name: имя пользователя для журнала.
    :returns: список отвязанных чатов.
    """
    await queries.add_admin_ban(db, user_id, reason, banned_by)
    await notify_user(
        bot,
        user_id,
        config.ADMIN_BANNED_MESSAGE.format(reason=reason or "не указана"),
    )

    detached: list[int] = []
    try:
        owned = await queries.get_owner_chats(db, user_id)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось получить чаты пользователя %s", user_id, exc_info=True)
        owned = []

    for chat in owned:
        await detach_chat(
            bot,
            db,
            chat.chat_id,
            title=chat.display_title,
            owner_id=user_id,
            notify_owner=False,
            actor_id=banned_by,
        )
        detached.append(chat.chat_id)

    await queries.log_admin_action(
        db,
        "ban_user",
        user_id,
        f"{name or ('ID ' + str(user_id))} (ID {user_id}), "
        f"причина: {reason or 'не указана'}, отвязано чатов: {len(detached)}",
    )
    logger.warning("Пользователь %s забанен в боте (админ %s).", user_id, banned_by)
    return detached


async def unban_user_in_bot(
    bot: Bot,
    db: Database,
    user_id: int,
    *,
    actor_id: Optional[int] = None,
    name: Optional[str] = None,
) -> bool:
    """Снять бан в боте и уведомить пользователя.

    :returns: ``True``, если бан действительно был снят.
    """
    removed = await queries.remove_admin_ban(db, user_id)
    if not removed:
        return False
    await notify_user(bot, user_id, config.ADMIN_UNBANNED_MESSAGE)
    await queries.log_admin_action(
        db, "unban_user", user_id, f"{name or ('ID ' + str(user_id))} (ID {user_id})"
    )
    return True


async def set_global_ban_flag(db: Database, user_id: int, value: bool) -> None:
    """Поставить или снять глобальную метку бана и записать это в журнал."""
    await queries.set_global_ban(db, user_id, bool(value))
    await queries.log_admin_action(
        db,
        "global_ban" if value else "global_unban",
        user_id,
        f"ID {user_id}",
    )


async def refresh_counters(bot: Bot, db: Database) -> dict[str, int]:
    """Пересчитать счётчики: варны участников и число людей в чатах.

    :param bot: экземпляр бота (для ``getChatMemberCount``).
    :param db: соединение с базой данных.
    :returns: словарь с ключами ``warns_fixed``, ``members_updated``, ``chats_checked``.
    """
    warns_fixed = 0
    try:
        warns_fixed = await queries.recount_warn_counters(db)
    except Exception:  # noqa: BLE001 - пересчёт не должен ронять панель
        logger.error("Не удалось пересчитать счётчики варнов", exc_info=True)

    chats_checked = 0
    members_updated = 0
    try:
        chat_ids = await queries.get_known_chat_ids(db)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось получить список чатов для пересчёта", exc_info=True)
        chat_ids = []

    for chat_id in chat_ids[: config.MAX_CHATS_FOR_OWNER_SYNC]:
        chats_checked += 1
        count = await telegram.fetch_members_count(bot, chat_id)
        if count is None:
            continue
        try:
            await queries.update_members_count(db, chat_id, count)
            members_updated += 1
        except Exception:  # noqa: BLE001
            logger.error("Не удалось обновить число участников чата %s", chat_id, exc_info=True)

    result = {
        "warns_fixed": int(warns_fixed),
        "members_updated": int(members_updated),
        "chats_checked": int(chats_checked),
    }
    await queries.log_admin_action(
        db,
        "refresh_counters",
        None,
        f"варнов исправлено: {result['warns_fixed']}, чатов проверено: {chats_checked}",
    )
    return result


def backup_filename() -> str:
    """Имя файла резервной копии базы (с датой и временем в UTC)."""
    return f"yamochan_backup_{utcnow().strftime('%Y%m%d_%H%M')}.db"


async def send_admin_message(
    bot: Bot,
    db: Database,
    user_id: int,
    content: richtext.RichContent,
    *,
    actor_id: Optional[int] = None,
) -> bool:
    """Отправить пользователю сообщение с форматированием от имени админа."""
    delivered = await richtext.send_content(bot, user_id, content, html_fallback=False)
    await queries.log_admin_action(
        db,
        "message_user",
        user_id,
        f"ID {user_id}: {'доставлено' if delivered else 'не доставлено'}",
    )
    logger.info(
        "Админ %s написал пользователю %s: %s",
        actor_id,
        user_id,
        "доставлено" if delivered else "ошибка",
    )
    return delivered


async def send_broadcast(
    bot: Bot,
    db: Database,
    content: richtext.RichContent,
    targets: Sequence[int],
    kind: str,
    *,
    actor_id: Optional[int] = None,
    on_progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    progress_step: int = config.ADMIN_BROADCAST_PROGRESS_STEP,
) -> tuple[int, int]:
    """Отправить содержимое всем получателям рассылки.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param content: содержимое рассылки (текст, сущности, фото).
    :param targets: чаты или личные сообщения получателей.
    :param kind: ``chats`` — чаты, ``owners`` — владельцы в ЛС.
    :param actor_id: кто отправил рассылку (для журнала).
    :param on_progress: асинхронный обработчик ``(отправлено, ошибок)``.
    :param progress_step: через сколько отправок вызывать обработчик.
    :returns: пару ``(отправлено, ошибок)``.
    """
    sent = 0
    errors = 0
    step = max(1, int(progress_step))
    total = len(targets)

    for index, target in enumerate(targets, start=1):
        try:
            delivered = await richtext.send_content(
                bot, int(target), content, html_fallback=False
            )
        except Exception:  # noqa: BLE001 - одна ошибка не останавливает рассылку
            logger.error("Ошибка рассылки в %s", target, exc_info=True)
            delivered = False
        if delivered:
            sent += 1
        else:
            errors += 1

        if on_progress is not None and (index % step == 0 or index == total):
            try:
                await on_progress(sent, errors)
            except Exception:  # noqa: BLE001 - прогресс не важнее рассылки
                logger.error("Не удалось обновить прогресс рассылки", exc_info=True)

    await queries.log_admin_action(
        db,
        "broadcast",
        actor_id,
        f"Рассылка {broadcast_kind_title(kind)}: отправлено {sent}, ошибок {errors}",
    )
    logger.info(
        "Рассылка %s завершена: отправлено %s, ошибок %s (админ %s).",
        kind,
        sent,
        errors,
        actor_id,
    )
    return sent, errors
