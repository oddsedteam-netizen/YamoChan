"""Формирование профилей, карточек чатов и текстов от лица YamoChan.

Модуль собирает все «человеческие» тексты бота: главное меню, профиль,
список чатов, карточку чата, приветствия новичков и сводки о наказаниях.
Никаких обращений к Telegram здесь нет — только чистые функции,
поэтому тексты легко проверить и переиспользовать.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Iterable, Optional, Sequence

import config
from database.models import (
    ChatInfo,
    ChatUser,
    Punishment,
    UserProfile,
    escape_text,
    utcnow,
)
from services import richtext, time_parser

#: Эмодзи палитры репутации: от «плохо» к «отлично».
REPUTATION_EMOJI: tuple[str, ...] = ("🔴", "🟠", "🟡", "🟢", "⚪")

#: Верхняя граница «чистой» репутации: выше неё репутация уже «отличная».
REPUTATION_GOOD_LIMIT: Final[int] = 20


def _reputation_value(reputation: object) -> int:
    """Привести репутацию к числу (мусор и ``None`` → 0)."""
    try:
        return int(reputation)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def reputation_scale(reputation: int) -> str:
    """Построить наглядную шкалу репутации из пяти кружочков.

    Уровни (как в текстах карточки профиля):

        * ``<= -10`` — 🔴🔴🔴🔴🔴
        * ``-10..-1`` — 🟠🟠🔴⚪⚪
        * ``0`` — 🟡🟡🟡⚪⚪
        * ``1..20`` — 🟢🟢🟡⚪⚪
        * ``> 20`` — 🟢🟢🟢🟢🟢

    :param reputation: текущая репутация пользователя.
    :returns: строка вида ``🟢🟢🟡⚪⚪``.
    """
    value = _reputation_value(reputation)
    if value <= config.BAD_REPUTATION_THRESHOLD:
        return "🔴🔴🔴🔴🔴"
    if value < 0:
        return "🟠🟠🔴⚪⚪"
    if value == 0:
        return "🟡🟡🟡⚪⚪"
    if value <= REPUTATION_GOOD_LIMIT:
        return "🟢🟢🟡⚪⚪"
    return "🟢🟢🟢🟢🟢"


def reputation_status(reputation: int) -> str:
    """Текстовая оценка репутации — пять уровней.

    :param reputation: текущая репутация пользователя.
    :returns: фраза для карточки профиля.
    """
    value = _reputation_value(reputation)
    if value <= config.BAD_REPUTATION_THRESHOLD:
        return "⚠️ У тебя ПЛОХАЯ репутация~ Будь осторожнее! 💔"
    if value < 0:
        return "😔 У тебя подпорчена репутация~ Веди себя лучше!"
    if value == 0:
        return "😐 Нейтральная репутация~"
    if value <= REPUTATION_GOOD_LIMIT:
        return "✨ Твоя репутация чиста~ Так держать! 💖"
    return "🌟 Отличная репутация~ Ты молодец! 💕"


def format_number(value: int) -> str:
    """Отформатировать число с разделителями тысяч (например ``1 234``)."""
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def format_moment(moment: Optional[datetime]) -> str:
    """Показать дату в виде ``ДД.ММ.ГГГГ ЧЧ:ММ (UTC)``."""
    if moment is None:
        return "неизвестно"
    return moment.strftime("%d.%m.%Y %H:%M (UTC)")


def format_left(expires_at: Optional[datetime]) -> str:
    """Сколько времени осталось до конца наказания (или «бессрочно»)."""
    if expires_at is None:
        return "бессрочно"
    delta = expires_at - utcnow()
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "истекает"
    return f"осталось {time_parser.format_duration(seconds)}"


def display_name(user: Optional[UserProfile], fallback: str = "Незнакомец") -> str:
    """Безопасно получить имя пользователя (без HTML-разметки и ``@``)."""
    if user is None or not user.display_name:
        return fallback
    return user.display_name


def public_name(first_name: Optional[str], user_id: Optional[int] = None) -> str:
    """Имя для публичных сообщений: только ``first_name``, без юзернейма.

    :param first_name: имя из Telegram или из профиля.
    :param user_id: идентификатор (подставляется, если имени нет).
    """
    clean = (first_name or "").strip()
    if clean:
        return clean
    return f"ID {user_id}" if user_id else "Незнакомец"


def neutral_admin_label() -> str:
    """Нейтральная подпись вместо имени админа/владельца."""
    return config.ADMIN_NEUTRAL_LABEL


def user_label(name: str, user_id: int) -> str:
    """Жирное имя с HTML-экранированием для текстов бота."""
    return f"<b>{escape_text(name or f'ID {user_id}')}</b>"


def build_main_menu_text(guarded_count: int = 0) -> str:
    """Текст главного меню бота в личных сообщениях.

    Внизу — маленькая подпись с названием и версией бота
    (:data:`config.BOT_VERSION`).

    :param guarded_count: сколько чатов сейчас под активной защитой.
    """
    lines = [
        "✨ Привет! Я YamoChan — твой модератор~ 💕",
        "Добавь меня в чат и я наведу порядок!",
    ]
    if guarded_count:
        lines.extend(
            [
                "",
                f"🛡 Внимание: защита включена в {guarded_count} "
                f"{'чате' if guarded_count == 1 else 'чатах'} — снять можно кнопкой ниже.",
            ]
        )
    lines.extend(["", "━━━━━━━━━━━━━", f"{config.BOT_NAME} v{config.BOT_VERSION}"])
    return "\n".join(lines)


def build_capabilities_text() -> str:
    """Текст кнопки «Возможности» (``/help`` и меню).

    Внизу — подпись с названием и версией бота (:data:`config.BOT_VERSION`).
    """
    return (
        "⚡ Мои возможности~\n\n"
        "🔨 Модерация:\n"
        "• .бан — забанить нарушителя\n"
        "• .разбан — снять бан\n"
        "• .мут — замутить болтуна\n"
        "• .размут — снять мут\n"
        "• .варн — предупреждение (3 = бан)\n"
        "• .снятьварн — снять предупреждение\n"
        "• .кик — выгнать из чата\n"
        "• .инфо — информация о пользователе\n"
        "• .открыть — открыть чат после защиты\n"
        "• .синк — сбросить кэш админов и перепроверить владельца\n\n"
        "📢 Контент чата:\n"
        "• .правила — показать правила чата\n"
        "• .приветствие — предпросмотр приветствия (админам)\n"
        "• .калл — позвать всех (текст необязателен)\n\n"
        "📊 Профили:\n"
        "• Автоматическое создание профилей\n"
        "• Отслеживание репутации\n"
        "• Глобальные метки банов\n\n"
        "🛡 Защита:\n"
        "• Антирейд режим\n"
        "• Обнаружение забаненных юзеров\n"
        "• Автобан после 3 варнов\n\n"
        "⚙️ Настройки через ЛС для владельца чата\n\n"
        "━━━━━━━━━━━━━\n"
        f"{config.BOT_NAME} v{config.BOT_VERSION}"
    )


def build_profile_text(
    profile: Optional[UserProfile],
    stats: dict[str, int],
    warns_by_chat: Sequence[tuple[int, int]],
    chat_titles: dict[int, str],
    active_punishments: int,
) -> str:
    """Собрать карточку профиля пользователя.

    :param profile: профиль пользователя.
    :param stats: результат :func:`queries.get_user_global_stats`.
    :param warns_by_chat: список ``(chat_id, количество варнов)``.
    :param chat_titles: соответствие ``chat_id → название чата``.
    :param active_punishments: сколько активных наказаний у пользователя.
    """
    reputation = int(stats.get("reputation", profile.reputation if profile else 0))
    total_messages = int(stats.get("total_messages", profile.total_messages if profile else 0))
    ban_marks = int(stats.get("ban_marks", len(profile.ban_marks) if profile else 0))

    if warns_by_chat:
        lines = [
            f"• {escape_text(chat_titles.get(chat_id, f'чат {chat_id}'))}: "
            f"{count}/{config.MAX_WARNS}"
            for chat_id, count in warns_by_chat[: config.MAX_WARN_CHATS_IN_PROFILE]
        ]
        warns_block = "⚠️ Активные варны:\n" + "\n".join(lines)
    else:
        warns_block = "⚠️ Активные варны: нет~ ✨"

    parts = [
        "👤 Твой профиль~\n",
        f"📛 Имя: {escape_text(display_name(profile))}",
        f"🆔 ID: {profile.user_id if profile else 0}",
        f"📊 Репутация: {reputation} {reputation_scale(reputation)}",
        f"💬 Всего сообщений: {format_number(total_messages)}",
        f"🏷 Метки банов: {ban_marks} чатов",
        warns_block,
    ]
    if active_punishments:
        parts.append(f"🚷 Активных наказаний: {active_punishments}")

    parts.append(f"\n{reputation_status(reputation)}")
    return "\n".join(parts)


def build_chats_list_text(chats: Sequence[ChatInfo]) -> str:
    """Текст списка «Мои чаты»."""
    if not chats:
        return (
            "💬 Твои чаты~\n\n"
            "Пока пусто~ Добавь меня в чат, и я всё запомню! 🌸"
        )
    return "💬 Твои чаты~\nНажми на чат для подробностей!"


def build_chat_card_text(
    chat: ChatInfo,
    stats: dict[str, int],
    is_owner: bool,
    chat_user: Optional[ChatUser] = None,
) -> str:
    """Собрать карточку чата для кнопки «Мои чаты».

    :param chat: информация о чате.
    :param stats: результат :func:`queries.get_chat_stats`.
    :param is_owner: является ли пользователь владельцем чата.
    :param chat_user: локальная статистика пользователя в этом чате.
    """
    members = int(stats.get("members", chat.members_count or 0))
    lines = [
        f"📊 Карточка чата: <b>{escape_text(chat.display_title)}</b>\n",
        f"👥 Участников: {format_number(members)}",
        f"💬 Сообщений всего: {format_number(int(stats.get('messages', 0)))}",
        f"🚫 В бане: {format_number(int(stats.get('banned', 0)))}",
        f"🤫 В муте: {format_number(int(stats.get('muted', 0)))}",
        f"⚠️ С варнами: {format_number(int(stats.get('warned', 0)))}",
        f"📈 Активность за сутки: {format_number(int(stats.get('daily_messages', 0)))} сообщений",
    ]
    if chat_user is not None:
        lines.extend(
            [
                "",
                f"🙋 Твои сообщения: {format_number(chat_user.messages_count)}",
                f"⚠️ Твои варны: {chat_user.warns_count}/{config.MAX_WARNS}",
            ]
        )
    lines.append("")
    if is_owner:
        lines.append("⚙️ Ты владелец чата — настройки бота под рукой~ 💕")
    else:
        lines.append("💡 Настройки доступны владельцу чата~")
    return "\n".join(lines)


def build_settings_text(chat: ChatInfo, settings: dict[str, object]) -> str:
    """Текст меню настроек бота для владельца чата.

    Порядок строк повторяет порядок кнопок
    (:func:`yamochan.keyboards.inline.settings_keyboard`).
    """

    def mark(value: object) -> str:
        return config.STATE_ON if bool(value) else config.STATE_OFF

    enabled_commands = sum(
        1 for value in (settings.get("commands") or {}).values() if bool(value)
    )
    return (
        f"⚙️ Настройки бота для <b>{escape_text(chat.display_title)}</b>\n\n"
        f"🔞 18+ команды: {mark(settings.get('nsfw_commands'))}\n"
        f"🛡 Антирейд: {mark(settings.get('antiraid_enabled', settings.get('antiraid')))}\n"
        f"📞 Call-режим: {mark(settings.get('call_mode'))}\n"
        f"📜 Правила чата: {mark(settings.get('rules_enabled'))}\n"
        f"👋 Приветствие: {mark(settings.get('greeting_enabled'))}\n"
        "🧹 Служебные сообщения: "
        + ("✅ СКРЫТЫ" if bool(settings.get(config.DELETE_SERVICE_MESSAGES_KEY, True)) else "❌ ВИДНЫ")
        + "\n"
        f"🛠 Команды модератора: включено {enabled_commands}"
        f"/{len(config.MODERATION_COMMANDS)}\n\n"
        "Нажимай на кнопки, чтобы переключать режимы~ 🌸"
    )


def build_commands_settings_text(chat: ChatInfo, settings: dict[str, object]) -> str:
    """Текст подменю «Настройка команд модератора»."""
    lines = [
        f"🛠 Команды модератора в <b>{escape_text(chat.display_title)}</b>\n",
        "Выключенная команда работает только у владельца:",
        "админам бот ответит отказом, а обычным участникам — промолчит~ ✨",
        "",
    ]
    commands = settings.get("commands") or {}
    for command in config.MODERATION_COMMANDS:
        title = config.COMMAND_TITLES.get(command, command)
        state = config.STATE_ON if bool(commands.get(command, True)) else config.STATE_OFF
        lines.append(f"{title}: {state}")
    return "\n".join(lines)


def build_user_info_text(
    profile: Optional[UserProfile],
    chat_user: Optional[ChatUser],
    punishments: Sequence[Punishment],
    fallback_name: str,
    username: Optional[str] = None,
) -> str:
    """Карточка пользователя для команды ``.инфо``.

    :param profile: глобальный профиль пользователя (может отсутствовать).
    :param chat_user: локальная статистика в чате (может отсутствовать).
    :param punishments: список активных наказаний в этом чате.
    :param fallback_name: имя из Telegram, если профиля в базе ещё нет.
    :param username: юзернейм из Telegram (если известен).
    """
    name = escape_text(display_name(profile, fallback_name))
    user_id = profile.user_id if profile is not None else (chat_user.user_id if chat_user else 0)
    reputation = profile.reputation if profile is not None else 0
    messages = chat_user.messages_count if chat_user is not None else 0
    warns = chat_user.warns_count if chat_user is not None else 0
    joined = chat_user.joined_at if chat_user is not None else None

    handle = username or (profile.username if profile is not None else None)
    lines = [
        "📋 Информация об участнике~",
        "",
        f"📛 Имя: <b>{name}</b>",
        f"🔗 Юзернейм: {'@' + escape_text(handle) if handle else 'нет'}",
        f"🆔 ID: <code>{user_id}</code>",
        f"💬 Сообщений в чате: {format_number(messages)}",
        f"⚠️ Предупреждения: {warns}/{config.MAX_WARNS}",
        f"📊 Репутация: {reputation} {reputation_scale(reputation)}",
        f"📅 В чате с: {format_moment(joined)}",
    ]

    if punishments:
        lines.append("")
        lines.append("🚷 Активные наказания:")
        for punishment in punishments:
            reason = escape_text(punishment.reason) if punishment.reason else "не указана"
            lines.append(
                f"• {punishment.type_title}: {format_left(punishment.expires_at)} "
                f"(причина: {reason})"
            )
    else:
        lines.append("")
        lines.append("✨ Активных наказаний нет~ Молодец!")

    if chat_user is not None and not chat_user.is_member:
        lines.append("")
        lines.append("🚪 Сейчас участника нет в чате.")
    return "\n".join(lines)


def build_welcome_new_text(name: str) -> str:
    """Приветствие для совсем нового участника.

    :param name: имя пользователя.
    """
    return f"👋 Добро пожаловать, <b>{escape_text(name)}</b>~ Веди себя хорошо! 💕"


def build_welcome_new_in_chat_text(name: str) -> str:
    """Приветствие участнику, который уже есть в базе, но в этом чате впервые.

    :param name: имя пользователя.
    """
    return f"👋 Добро пожаловать, <b>{escape_text(name)}</b>~ Рада видеть тебя! 💕"


def build_welcome_banned_note_text(profile: UserProfile, name: str) -> str:
    """Предупреждение о метках банов в других чатах.

    :param profile: профиль пользователя с метками банов.
    :param name: имя пользователя.
    """
    return (
        f"⚠️ Внимание! Пользователь <b>{escape_text(name)}</b> имеет метки банов "
        f"в других чатах ({len(profile.ban_marks)} чатов)~\n"
        f"Репутация: {profile.reputation} 🔻"
    )


def build_welcome_returning_text(
    profile: UserProfile,
    chat_user: ChatUser,
    punishments: Sequence[Punishment],
    name: str,
) -> str:
    """Краткая сводка для участника, который уже был в этом чате.

    :param profile: глобальный профиль пользователя.
    :param chat_user: локальная статистика пользователя в чате.
    :param punishments: активные наказания в этом чате.
    :param name: имя пользователя.
    """
    lines = [
        f"📋 С возвращением, <b>{escape_text(name)}</b>~",
        "📊 Сообщений: "
        f"{format_number(chat_user.messages_count)} | "
        f"Репутация: {profile.reputation} | Варны: {chat_user.warns_count}/{config.MAX_WARNS}",
    ]
    for punishment in punishments:
        reason = escape_text(punishment.reason) if punishment.reason else "не указана"
        lines.append(
            f"🚷 {punishment.type_title}: {format_left(punishment.expires_at)} "
            f"(причина: {reason})"
        )
    return "\n".join(lines)


def build_join_profile_summary(
    profile: Optional[UserProfile],
    chat_user: Optional[ChatUser],
    member: object,
    *,
    punishments: Sequence[Punishment] = (),
    is_new_globally: bool = False,
    is_new_in_chat: bool = False,
    fallback_name: str = "",
) -> str:
    """Блок «сводка по участнику» для сообщения о входе — с именем и ID.

    Используется настройкой ``greeting_show_profile``: сводка идёт ВМЕСТЕ с
    приветствием, а не вместо него. Приватность соблюдена: юзернейм не
    показывается, только ``first_name`` внутри ссылки ``tg://user``.

    :param profile: глобальный профиль участника (может отсутствовать).
    :param chat_user: локальная статистика в чате (может отсутствовать).
    :param member: объект пользователя Telegram (``User``).
    :param punishments: активные наказания участника в этом чате.
    :param is_new_globally: профиля не было в базе до этого входа.
    :param is_new_in_chat: участник впервые в этом чате.
    :param fallback_name: имя из Telegram, если в базе его ещё нет.
    """
    user_id = int(getattr(member, "id", 0) or (profile.user_id if profile else 0) or 0)
    fallback = (
        fallback_name
        or str(getattr(member, "full_name", "") or "")
        or str(getattr(member, "first_name", "") or "")
    )
    name = display_name(profile, fallback or "Незнакомец")

    lines = [f"📊 <b>Сводка по</b> {user_mention(name, user_id)}:"]
    lines.append(f"🆔 ID: <code>{user_id}</code>")

    reputation = int(profile.reputation or 0) if profile is not None else 0
    lines.append(f"⭐ Репутация: {reputation} {reputation_scale(reputation)}")

    messages = chat_user.messages_count if chat_user is not None else 0
    lines.append(f"💬 Сообщений: {format_number(messages)}")
    if chat_user is not None:
        lines.append(f"⚠️ Предупреждения: {chat_user.warns_count}/{config.MAX_WARNS}")

    marks = len(profile.ban_marks or []) if profile is not None else 0
    if marks > 0:
        lines.append(f"🏷 Метки банов: {marks} чатов")
    if profile is not None and profile.is_spammer:
        lines.append("🚫 Метка: спамер")
    if profile is not None and profile.is_globally_banned:
        lines.append("🔨 Глобальный бан")

    for punishment in punishments:
        reason = escape_text(punishment.reason) if punishment.reason else "не указана"
        lines.append(
            f"🚷 {punishment.type_title}: {format_left(punishment.expires_at)} "
            f"(причина: {reason})"
        )

    if is_new_globally:
        lines.append("✨ Впервые в моей базе")
    elif is_new_in_chat:
        lines.append("✨ Впервые в этом чате")
    return "\n".join(lines)


def build_anonymous_profile_summary(
    profile: Optional[UserProfile],
    *,
    is_new_globally: bool = False,
) -> str:
    """Анонимная сводка о новом участнике — без имени, ID и юзернейма.

    Используется режимом ``welcome_anonymous``: бот не раскрывает личность
    вошедшего, зато показывает его репутацию и метки, чтобы чат понимал,
    кого стоит проверять. Сводка дополняет приветствие, а не заменяет его.

    :param profile: глобальный профиль участника (``None`` — данных нет).
    :param is_new_globally: профиля не было в базе до этого входа.
    """
    lines = ["👤 Зашёл новый пользователь!", ""]

    if profile is None:
        lines.append("✨ Новый аккаунт, нет данных")
    else:
        reputation = int(profile.reputation or 0)
        if reputation <= config.BAD_REPUTATION_THRESHOLD:
            lines.append(f"⚠️ Репутация: плохая ({reputation}) 🔴")
        elif reputation < 0:
            lines.append(f"😔 Репутация: подпорчена ({reputation}) 🟠")
        elif reputation == 0:
            lines.append("😐 Репутация: нейтральная")
        else:
            lines.append(f"✨ Репутация: {reputation} 🟢")

        marks = len(profile.ban_marks or [])
        if marks > 0:
            lines.append(f"🏷 Метки банов: {marks} чатов")
        if profile.is_spammer:
            lines.append("🚫 Метка: спамер")
        if profile.is_globally_banned:
            lines.append("🔨 Глобальный бан")
        if is_new_globally:
            lines.append("✨ Впервые в моей базе")
    return "\n".join(lines)


def build_leave_text(name: str) -> str:
    """Строка прощания для участника, покинувшего чат.

    :param name: имя пользователя.
    """
    return f"👋 <b>{escape_text(name)}</b> покинул(а) чат~"


def build_anonymous_welcome_text(
    profile: Optional[UserProfile],
    duration_seconds: int,
    *,
    muted: bool = True,
    is_new_globally: bool = False,
) -> str:
    """Анонимное уведомление о новом участнике — сводка и инфо о муте.

    Совместимая обёртка: сводка берётся из
    :func:`build_anonymous_profile_summary`, строка о муте — из
    :func:`build_entry_mute_notice_text`. Имя, ID и юзернейм вошедшего не
    показываются.

    :param profile: глобальный профиль участника (``None`` — данных нет).
    :param duration_seconds: длительность проверочного мута.
    :param muted: удалось ли действительно замутить новичка.
    :param is_new_globally: профиля не было в базе до этого входа.
    """
    summary = build_anonymous_profile_summary(
        profile, is_new_globally=is_new_globally
    )
    if muted:
        notice = (
            f"🔇 Мут на {time_parser.format_duration(duration_seconds)} для проверки"
        )
    else:
        notice = "🔇 Мут не удалось поставить — проверь мои права 🙏"
    return f"{summary}\n\n{notice}"


def build_antiraid_kick_text(name: str) -> str:
    """Сообщение о кике новичка из-за антирейда.

    :param name: имя пользователя.
    """
    return (
        "🛡 Антирейд: слишком много входов подряд~\n"
        f"<b>{escape_text(name)}</b> отправлен(а) обратно."
    )


# ---------------------------------------------------------------------------
# Антирейд и антиспам
# ---------------------------------------------------------------------------
def user_mention(name: str, user_id: int) -> str:
    """HTML-ссылка на пользователя (работает даже без юзернейма)."""
    return f'<a href="tg://user?id={user_id}">{escape_text(name or f"ID {user_id}")}</a>'


def build_antiraid_menu_text(chat: ChatInfo, settings: dict[str, object]) -> str:
    """Текст меню настроек антирейда для владельца."""
    threshold = int(settings.get("antiraid_threshold") or config.ANTIRAID_DEFAULT_THRESHOLD)
    timeframe = int(settings.get("antiraid_timeframe") or config.ANTIRAID_DEFAULT_TIMEFRAME)
    mode = str(settings.get("antispam_mode") or config.ANTISPAM_MODE_NO_CLOSE)
    mode_title = config.ANTISPAM_MODE_TITLES.get(mode, mode)
    protection = bool(settings.get("antiraid_active_protection"))
    spam_threshold = int(settings.get("spam_msg_threshold") or config.SPAM_DEFAULT_THRESHOLD)
    spam_timeframe = int(settings.get("spam_msg_timeframe") or config.SPAM_DEFAULT_TIMEFRAME)

    return (
        "🛡 <b>Антирейд — настройки</b>\n"
        f"{escape_text(chat.display_title)}\n\n"
        "Антирейд автоматически определяет массовый вход\n"
        "пользователей в чат и блокирует его~\n\n"
        "📊 <b>Текущие настройки:</b>\n"
        f"  👥 Порог: {threshold} человек за {time_parser.human_duration(timeframe)}\n"
        f"  📨 Антиспам: {mode_title} "
        f"({spam_threshold} сооб за {time_parser.human_duration(spam_timeframe)})\n"
        f"  🔒 Защита сейчас: {'активна ✅' if protection else 'неактивна ❌'}\n\n"
        "<b>Как это работает:</b>\n"
        "Если за указанное время заходит слишком много\n"
        "людей — я закрою чат, замучу подозрительных\n"
        "и уведомлю тебя~ 💕\n\n"
        "Пока рейда нет, антирейд никого не мутит:\n"
        "мут при входе настраивается в меню\n"
        "👋 «Приветствие» (мут при входе / помеченным)~"
    )


def build_antiraid_prompt_text(
    chat: ChatInfo,
    settings: dict[str, object],
    kind: str = "antiraid",
) -> str:
    """Текст запроса нового порога (антирейда или антиспама).

    :param kind: ``"spam"`` — порог спама, иначе порог антирейда.
    """
    title = escape_text(chat.display_title)
    if kind == "spam":
        amount = int(settings.get("spam_msg_threshold") or config.SPAM_DEFAULT_THRESHOLD)
        seconds = int(settings.get("spam_msg_timeframe") or config.SPAM_DEFAULT_TIMEFRAME)
        return (
            "📊 <b>Настройка порога антиспама~</b>\n\n"
            f"Сейчас: {amount} сооб за {time_parser.human_duration(seconds)}\n\n"
            "Отправь мне новое значение в формате:\n"
            "<b>количество время</b>\n\n"
            "Примеры:\n"
            "• 7 10с — 7 сообщений за 10 секунд\n"
            "• 15 1м — 15 сообщений за минуту\n"
            "• 20 2м — 20 сообщений за 2 минуты\n\n"
            f"Для «{title}». Отмена — кнопка ниже 🌸"
        )
    amount = int(settings.get("antiraid_threshold") or config.ANTIRAID_DEFAULT_THRESHOLD)
    seconds = int(settings.get("antiraid_timeframe") or config.ANTIRAID_DEFAULT_TIMEFRAME)
    return (
        "👥 <b>Настройка порога антирейда~</b>\n\n"
        f"Текущий порог: {amount} человек за "
        f"{time_parser.human_duration(seconds)}\n\n"
        "Отправь мне новое значение в формате:\n"
        "<b>количество время</b>\n\n"
        "Примеры:\n"
        "• 5 5м — 5 человек за 5 минут\n"
        "• 1 2м — 1 человек за 2 минуты\n"
        "• 10 1ч — 10 человек за 1 час\n"
        "• 3 30с — 3 человека за 30 секунд\n\n"
        f"Для «{title}». Отмена — кнопка ниже 🌸"
    )


def build_raid_alert_text(
    chat: ChatInfo,
    suspects_count: int,
    timeframe_seconds: int,
) -> str:
    """Уведомление владельцу о найденном рейде.

    :param chat: информация о чате.
    :param suspects_count: сколько аккаунтов заподозрено.
    :param timeframe_seconds: за какое окно они зашли.
    """
    return (
        "🚨 <b>ОБНАРУЖЕН РЕЙД В ЧАТЕ!</b>\n\n"
        f"📛 Чат: <b>{escape_text(chat.display_title)}</b>\n"
        f"👥 Подозрительных: {suspects_count} человек\n"
        f"⏱ Зашли за: {time_parser.human_duration(timeframe_seconds)}\n\n"
        "Я ограничила чат и замутила подозрительных~\n"
        "Что мне делать? 🥺"
    )


def build_raid_chat_notice_text(suspects_count: int) -> str:
    """Сообщение в сам чат в момент обнаружения рейда."""
    return (
        "🚨 <b>Антирейд!</b> Зафиксирован массовый вход участников.\n"
        f"Подозрительных: {suspects_count}. Чат временно закрыт~ 🔒\n"
        "Владелец получил уведомление."
    )


def build_raid_confirmed_text(
    suspects: Sequence[tuple[int, str, Optional[str]]],
) -> str:
    """Карточка подтверждённого рейда со списком аккаунтов.

    Юзернеймы не показываем: это публичный ник, раскрывать его нельзя.

    :param suspects: последовательность ``(user_id, имя, юзернейм)``.
    """
    lines = ["🚫 <b>Рейд подтверждён!</b>", "", "<b>Подозрительные аккаунты:</b>"]
    if suspects:
        for index, (user_id, name, _username) in enumerate(suspects, start=1):
            label = public_name(name, user_id)
            lines.append(
                f"{index}. {user_mention(label, user_id)} — ID: <code>{user_id}</code>"
            )
    else:
        lines.append("• список пуст — подозрительных уже нет~")
    lines.extend(
        [
            "",
            "💡 Рекомендую сначала забанить негодников, а потом снять защиту~",
            "Снимаю защиту? 🤔",
        ]
    )
    return "\n".join(lines)


def build_spam_text(name: str, user_id: int, chat_closed: bool) -> str:
    """Сообщение в чат о забаненном спамере.

    :param name: имя нарушителя.
    :param user_id: идентификатор нарушителя.
    :param chat_closed: закрывается ли чат (режим антиспама).
    """
    mention = user_mention(name, user_id)
    if chat_closed:
        return (
            f"🚫 Пользователь {mention} забанен за спам!\n"
            "Чат временно закрыт для безопасности~ 🔒\n\n"
            "Админы, напишите .открыть для снятия ограничений."
        )
    return (
        f"🚫 Пользователь {mention} забанен за спам! 🔨\n"
        "Сообщения удалены, чат продолжает работать~"
    )


def build_false_alarm_text() -> str:
    """Ответ на кнопку «Это не рейд — снять защиту»."""
    return "✅ Ложная тревога~ Все ограничения сняты! 💕"


def build_raid_kept_text() -> str:
    """Ответ на кнопку «Нет, оставить»."""
    return "🛡 Защита остаётся. Когда будешь готова — нажми кнопку снятия~"


def build_protection_lifted_text() -> str:
    """Ответ на снятие защиты."""
    return "✅ Защита снята~ Чат восстановлен! 💕"


def build_chat_unlocked_text() -> str:
    """Ответ команды ``.открыть``."""
    return "🔓 Чат открыт~ Можно общаться! 💕"


def build_no_access_to_antiraid_text() -> str:
    """Ответ чужому человеку, который нажал кнопки антирейда."""
    return "Настройки антирейда видит только владелец чата~ 🌸"


def build_nsfw_menu_text(enabled: bool) -> str:
    """Экран настройки 18+ команд для владельца чата.

    :param enabled: включены ли 18+ команды сейчас.
    """
    examples = ".трахнуть, .отсосать, .вылизать, .раздеть и другие~"
    if enabled:
        return (
            "🔞 <b>18+ команды</b>\n\n"
            "Сейчас: ✅ ВКЛЮЧЕНЫ\n\n"
            "Доступные команды:\n"
            f"{examples}\n\n"
            "Полный список — кнопкой ниже 👇"
        )
    return (
        "🔞 <b>18+ команды</b>\n\n"
        "Сейчас: ❌ ВЫКЛЮЧЕНЫ\n\n"
        "При включении станут доступны команды:\n"
        f"{examples}\n\n"
        "⚠️ Включай только если чат 18+!"
    )


def build_nsfw_list_text(entries: Sequence[tuple[str, str, str]]) -> str:
    """Список всех 18+ команд с описанием.

    :param entries: последовательность ``(команда, эмодзи, действие)``.
    """
    lines = ["🔞 <b>Список 18+ команд</b>", ""]
    for name, emoji, action in entries:
        lines.append(f"{emoji} <code>.{escape_text(name)}</code> — {escape_text(action)}")
    lines.extend(
        [
            "",
            f"Всего команд: <b>{len(entries)}</b>",
            "Работают по реплаю, @юзернейму или ID~ 🌸",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Call режим
# ---------------------------------------------------------------------------
def build_call_header_text(
    author_name: Optional[str],
    author_id: Optional[int],
    *,
    neutral: bool = False,
) -> str:
    """Шапка вызова: кто зовёт всех.

    Приватность: если автора нельзя раскрывать (админ или владелец чата),
    вместо имени подставляется нейтральная подпись «Администрация».

    :param author_name: имя автора вызова (без ``@``).
    :param author_id: идентификатор автора (``None`` — автор неизвестен).
    :param neutral: скрыть имя автора и показать нейтральную подпись.
    """
    if neutral:
        who = f"<b>{escape_text(neutral_admin_label())}</b>"
    elif author_id:
        who = user_mention(public_name(author_name, author_id), int(author_id))
    else:
        who = f"<b>{config.BOT_NAME}</b>"
    return f"📢 {who} зовёт всех~"


def build_call_menu_text(chat: ChatInfo, settings: dict[str, object]) -> str:
    """Текст меню Call режима для владельца.

    :param chat: информация о чате.
    :param settings: текущие настройки чата.
    """
    enabled = bool(settings.get("call_enabled"))
    admins = bool(settings.get("call_mention_admins", True))
    emoji_mode = bool(settings.get("call_use_emoji"))
    emoji = str(settings.get("call_emoji") or config.CALL_EMOJI_DEFAULT)
    delete_messages = bool(settings.get("call_delete_messages"))
    scheduled = len(settings.get("call_scheduled") or [])
    mark = lambda value: "✅" if value else "❌"  # noqa: E731 - короткий локальный помощник

    return (
        "📞 <b>Call режим — настройки</b>\n"
        f"{escape_text(chat.display_title)}\n\n"
        "Позволяет одной командой позвать всех\n"
        "участников чата с любым сообщением~\n\n"
        "📊 <b>Текущие настройки:</b>\n"
        f"  🔔 Call режим: {mark(enabled)}\n"
        f"  👑 Отмечать админов: {mark(admins)}\n"
        f"  🎭 Эмодзи вместо имён: {mark(emoji_mode)} (сейчас {escape_text(emoji)})\n"
        f"  🗑 Удалять сообщение команды: {mark(delete_messages)}\n"
        f"  ⏰ Отложенных вызовов: {scheduled}\n\n"
        "Команды: .калл, .call, /калл, /call\n"
        "Пример: .калл всем подписаться!"
    )


def build_call_emoji_prompt_text(settings: dict[str, object]) -> str:
    """Запрос нового эмодзи для режима «эмодзи вместо имён»."""
    current = str(settings.get("call_emoji") or config.CALL_EMOJI_DEFAULT)
    return (
        "🎨 <b>Выбери эмодзи для замены имён</b>\n\n"
        f"Текущий: {escape_text(current)}\n\n"
        "Отправь мне любой эмодзи~"
    )


def build_call_schedule_prompt_text() -> str:
    """Запрос отложенного вызова в формате «время | текст»."""
    return (
        "⏰ <b>Отложенный Call</b>\n\n"
        "Настрой автоматический вызов~\n\n"
        "Отправь мне сообщение в формате:\n"
        "<b>[время] | [текст]</b>\n\n"
        "Примеры:\n"
        "• 30м | Всем привет!\n"
        "• 1ч | Стрим начинается\n"
        "• 2д | Годовщина чата 🎉\n\n"
        "✨ Можно приложить фото, а форматирование и премиум-эмодзи\n"
        "сохраняются как есть."
    )


def build_call_scheduled_list_text(entries: Sequence[dict[str, object]]) -> str:
    """Список отложенных вызовов с оставшимся временем.

    :param entries: записи ``{"time": ts, "text": ..., "created_by": ...}``.
    """
    lines = ["📋 <b>Отложенные вызовы</b>", ""]
    if not entries:
        lines.append("Пока ничего не запланировано~ 🌸")
        return "\n".join(lines)

    for index, entry in enumerate(entries, start=1):
        moment = float(entry.get("time") or 0)
        left = int(max(0, moment - utcnow().timestamp()))
        text = str(entry.get("text") or "")
        lines.append(f"{index}. Через {time_parser.human_duration(left)}")
        lines.append(f"   «{escape_text(text)}»")
    lines.append("")
    lines.append("Отменить можно кнопкой ниже 👇")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Правила чата и приветствие (тексты владельца с премиум-эмодзи и фото)
# ---------------------------------------------------------------------------
def rich_text_snippet(text: str, limit: int = config.RICH_PREVIEW_LENGTH) -> str:
    """Однострочный фрагмент текста владельца для превью в меню.

    :param text: текст правил/приветствия.
    :param limit: сколько символов показывать.
    """
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


def _has_saved_content(settings: dict[str, object], prefix: str) -> bool:
    """Есть ли у блока сохранённый текст или фото."""
    return bool(str(settings.get(f"{prefix}_text") or "").strip()) or bool(
        settings.get(f"{prefix}_photo")
    )


def _has_premium_emoji(settings: dict[str, object], prefix: str) -> bool:
    """Есть ли среди сущностей блока премиум-эмодзи."""
    return any(
        isinstance(entity, dict) and entity.get("type") == "custom_emoji"
        for entity in (settings.get(f"{prefix}_entities") or [])
    )


def build_rules_menu_text(chat: ChatInfo, settings: dict[str, object]) -> str:
    """Текст меню «Правила чата» для владельца.

    :param chat: чат, чьи правила настраиваются.
    :param settings: текущие настройки чата.
    """
    text = str(settings.get("rules_text") or "")
    has_content = _has_saved_content(settings, "rules")
    photo = bool(settings.get("rules_photo"))
    show_on_join = bool(settings.get("rules_show_on_join"))
    premium = _has_premium_emoji(settings, "rules")

    lines = [
        "📜 <b>Правила чата</b>",
        escape_text(chat.display_title),
        "",
        "Здесь можно настроить правила твоего чата~",
        "Я могу отправлять их автоматически новым участникам.",
        "",
        "📊 <b>Настройки:</b>",
        f"  📝 Правила заданы: {'✅' if has_content else '❌'}",
        f"  🚪 Показывать при входе: {'✅' if show_on_join else '❌'}",
        f"  🖼 Фото: {'есть 🖼' if photo else 'нет ❌'}",
        f"  ✨ Премиум-эмодзи: {'есть ✨' if premium else 'нет'}",
        f"  📝 Символов: {len(text)} / {config.RICH_TEXT_LIMIT}",
    ]
    if has_content:
        lines.extend(["", "<b>Превью:</b>", escape_text(rich_text_snippet(text))])
    lines.extend(["", f"Команда: .{config.COMMAND_RULES}, /{config.COMMAND_RULES}"])
    return "\n".join(lines)


def build_greeting_menu_text(chat: ChatInfo, settings: dict[str, object]) -> str:
    """Текст меню «Приветствие» для владельца.

    :param chat: чат, чьё приветствие настраивается.
    :param settings: текущие настройки чата.
    """
    text = str(settings.get("greeting_text") or "")
    has_content = _has_saved_content(settings, "greeting")
    photo = bool(settings.get("greeting_photo"))
    show_profile = bool(settings.get("greeting_show_profile"))
    anonymous = bool(settings.get(config.WELCOME_ANONYMOUS_KEY))
    check_duration = int(
        settings.get(config.WELCOME_CHECK_DURATION_KEY)
        or config.WELCOME_CHECK_DEFAULT_SECONDS
    )
    mute_enabled = bool(settings.get("marked_mute_enabled"))
    mute_duration = int(
        settings.get("marked_mute_duration") or config.MARKED_MUTE_DEFAULT_SECONDS
    )
    join_mute_enabled = bool(settings.get(config.JOIN_MUTE_ENABLED_KEY))
    join_mute_duration = int(
        settings.get(config.JOIN_MUTE_DURATION_KEY) or config.JOIN_MUTE_DEFAULT_SECONDS
    )
    join_mute_line = (
        f"  🔇 Мут при входе: {config.STATE_ON}"
        f" ({time_parser.human_duration(join_mute_duration)})"
        if join_mute_enabled
        else f"  🔇 Мут при входе: {config.STATE_OFF}"
    )
    buttons = len(richtext.parse_saved_buttons(settings.get("greeting_buttons")))
    premium = _has_premium_emoji(settings, "greeting")

    lines = [
        "👋 <b>Приветствие новых участников</b>",
        escape_text(chat.display_title),
        "",
        "Настрой персональное приветствие для чата~",
        "",
        "📊 <b>Настройки:</b>",
        f"  📝 Приветствие: {'✅' if has_content else '❌'}",
        f"  📋 Профиль при входе: {'✅' if show_profile else '❌'}",
        f"  🔒 Анонимный привет: {'✅' if anonymous else '❌'}",
        f"  ⏱ Мут проверки: {time_parser.format_duration(check_duration)}",
        join_mute_line,
        f"  🔇 Мут помеченным: {'✅' if mute_enabled else '❌'}",
        f"  ⏱ Длительность мута: {time_parser.format_duration(mute_duration)}",
        f"  🔘 Кнопок: {buttons} / {config.MAX_GREETING_BUTTONS}",
        f"  🖼 Фото: {'есть 🖼' if photo else 'нет ❌'}",
        f"  ✨ Премиум-эмодзи: {'есть ✨' if premium else 'нет'}",
        f"  📝 Символов: {len(text)} / {config.RICH_TEXT_LIMIT}",
    ]
    if has_content:
        lines.extend(["", "<b>Превью:</b>", escape_text(rich_text_snippet(text))])
    lines.extend(
        [
            "",
            "💡 Настройки складываются: своё приветствие, сводка (или",
            "анонимная сводка без имени) и инфо о муте уходят вместе~",
        ]
    )
    lines.extend(["", f"Команда предпросмотра: .{config.COMMAND_GREETING}"])
    return "\n".join(lines)


def build_welcome_check_duration_prompt_text(settings: dict[str, object]) -> str:
    """Запрос длительности проверочного мута (анонимное приветствие).

    :param settings: текущие настройки чата.
    """
    duration = int(
        settings.get(config.WELCOME_CHECK_DURATION_KEY)
        or config.WELCOME_CHECK_DEFAULT_SECONDS
    )
    return (
        "⏱ <b>Время мута для проверки</b>\n\n"
        f"Текущее значение: {time_parser.format_duration(duration)}\n\n"
        "Отправь новое время, например:\n"
        "• 30с\n"
        "• 2м\n"
        "• 1ч\n\n"
        "Число без букв считается минутами.\n"
        f"От {time_parser.human_duration(config.WELCOME_CHECK_MIN_SECONDS)} "
        f"до {time_parser.human_duration(config.WELCOME_CHECK_MAX_SECONDS)}"
    )


def build_placeholders_help_text() -> str:
    """Список спец-команд для подсказок в редакторах."""
    return (
        "Спец-команды:\n"
        f"• {config.NAME_PLACEHOLDER} — имя вошедшего участника\n"
        f"• {config.ID_PLACEHOLDER} — его ID\n"
        f"• {config.MENTION_PLACEHOLDER} — упоминание участника\n"
        f"• {config.CHAT_PLACEHOLDER} — название чата\n"
        f"• {config.COUNT_PLACEHOLDER} — количество участников"
    )


def build_content_prompt_text(kind: str) -> str:
    """Запрос содержимого для блока «правила» или «приветствие».

    :param kind: ``rules`` или ``greeting``.
    """
    if kind == "rules":
        return (
            "✏️ <b>Редактор правил</b>\n\n"
            "Отправь мне текст правил одним сообщением~\n"
            "Можешь использовать:\n"
            "• Форматирование (жирный, курсив, ссылки)\n"
            "• Премиум-эмодзи ✨\n"
            "• Фото (одно фото с текстом)\n\n"
            f"{build_placeholders_help_text()}\n\n"
            "Правила увидят все по команде «.правила»."
        )
    return (
        "✏️ <b>Приветствие — шаг 1/2</b>\n\n"
        "Отправь текст приветствия~\n"
        "Можно с фото, форматированием и премиум-эмодзи ✨\n\n"
        f"{build_placeholders_help_text()}"
    )


def build_greeting_buttons_question_text() -> str:
    """Шаг 2 редактора приветствия: спросить про инлайн-кнопки."""
    return (
        "✅ Текст сохранён!\n\n"
        "Добавить инлайн-кнопки к приветствию?"
    )


def build_greeting_button_name_prompt_text(index: int, limit: int) -> str:
    """Запрос названия кнопки.

    :param index: номер добавляемой кнопки (1-based).
    :param limit: максимум кнопок.
    """
    return (
        f"🔘 <b>Добавление кнопки [{index}/{limit}]</b>\n\n"
        "Отправь название кнопки:"
    )


def build_greeting_button_url_prompt_text() -> str:
    """Запрос ссылки кнопки после названия."""
    return (
        "🔗 Теперь отправь ссылку:\n"
        "(https://… или tg://… или t.me/…)"
    )


def build_greeting_button_added_text(count: int, limit: int) -> str:
    """Подтверждение добавления кнопки.

    :param count: сколько кнопок уже сохранено.
    :param limit: максимум кнопок.
    """
    return (
        f"✅ Кнопка добавлена!\n\n"
        f"Всего кнопок: [{count}/{limit}]\n\n"
        "Добавить ещё?"
    )


def build_greeting_buttons_done_text(count: int) -> str:
    """Итог настройки кнопок приветствия.

    :param count: сколько кнопок сохранено.
    """
    if count:
        return f"🔘 Кнопки приветствия сохранены: {count} шт. 💕"
    return "Хорошо, приветствие будет без кнопок~ 🌸"


def build_invalid_button_url_text() -> str:
    """Ответ на недопустимую ссылку кнопки."""
    return (
        "Неверная ссылка~ Разрешены только http://, https:// и tg:// "
        "(или короткая ссылка вида t.me/…)."
    )


def build_buttons_cleared_text() -> str:
    """Подтверждение удаления кнопок приветствия."""
    return "🗑 Кнопки приветствия убраны~"


def build_marked_mute_menu_text(chat: ChatInfo, settings: dict[str, object]) -> str:
    """Текст подменю «Муты помеченным».

    :param chat: чат, чья настройка меняется.
    :param settings: текущие настройки чата.
    """
    enabled = bool(settings.get("marked_mute_enabled"))
    duration = int(
        settings.get("marked_mute_duration") or config.MARKED_MUTE_DEFAULT_SECONDS
    )
    return (
        "🔇 <b>Муты помеченным</b>\n"
        f"{escape_text(chat.display_title)}\n\n"
        "Автоматически мутить новых участников с плохими метками?\n\n"
        "<b>Метки:</b>\n"
        "• Спамер (был забанен за спам)\n"
        f"• Много банов ({config.MARKED_MUTE_BAN_MARKS}+ ban_marks)\n"
        # «&lt;» вместо «<»: без экранирования Telegram видит начало тега
        # с пустым именем и отклоняет всё сообщение (Unsupported start tag).
        f"• Плохая репутация (&lt; {config.MARKED_MUTE_REPUTATION})\n\n"
        "📊 <b>Настройки:</b>\n"
        f"  🔇 Автомут: {'✅ ВКЛ' if enabled else '❌ ВЫКЛ'}\n"
        f"  ⏱ Длительность: {time_parser.format_duration(duration)}"
    )


def build_marked_mute_duration_prompt_text(settings: dict[str, object]) -> str:
    """Запрос новой длительности превентивного мута.

    :param settings: текущие настройки чата.
    """
    duration = int(
        settings.get("marked_mute_duration") or config.MARKED_MUTE_DEFAULT_SECONDS
    )
    return (
        "⏱ <b>Длительность превентивного мута</b>\n\n"
        f"Текущее значение: {time_parser.format_duration(duration)}\n\n"
        "Отправь новое время, например:\n"
        "• 30м\n"
        "• 2ч\n"
        "• 1д\n\n"
        f"От {time_parser.human_duration(config.MARKED_MUTE_MIN_SECONDS)} "
        f"до {time_parser.human_duration(config.MARKED_MUTE_MAX_SECONDS)}."
    )


def build_join_mute_menu_text(chat: ChatInfo, settings: dict[str, object]) -> str:
    """Текст подменю «Мут при входе» (для всех новых участников).

    :param chat: чат, чья настройка меняется.
    :param settings: текущие настройки чата.
    """
    enabled = bool(settings.get(config.JOIN_MUTE_ENABLED_KEY))
    duration = int(
        settings.get(config.JOIN_MUTE_DURATION_KEY) or config.JOIN_MUTE_DEFAULT_SECONDS
    )
    return (
        "🔇 <b>Мут при входе</b>\n"
        f"{escape_text(chat.display_title)}\n\n"
        "Всем новым участникам будет выдаваться\n"
        "временный мут на указанное время~\n\n"
        "Полезно чтобы новые не могли сразу спамить.\n\n"
        "📊 <b>Текущее:</b>\n"
        f"  🔔 Статус: {config.STATE_ON if enabled else config.STATE_OFF}\n"
        f"  ⏱ Длительность: {time_parser.human_duration(duration)}"
    )


def build_join_mute_duration_prompt_text(settings: dict[str, object]) -> str:
    """Запрос длительности мута при входе для всех новичков.

    :param settings: текущие настройки чата.
    """
    duration = int(
        settings.get(config.JOIN_MUTE_DURATION_KEY) or config.JOIN_MUTE_DEFAULT_SECONDS
    )
    return (
        "⏱ <b>Настройка времени мута</b>\n\n"
        f"Текущее время: {time_parser.human_duration(duration)}\n\n"
        "Отправь новое время в любом формате:\n"
        "• 30с — 30 секунд\n"
        "• 5м — 5 минут\n"
        "• 1ч — 1 час\n"
        "• 1ч30м — 1 час 30 минут\n"
        "• 2д — 2 дня\n\n"
        f"Максимум: {time_parser.human_duration(config.JOIN_MUTE_MAX_SECONDS)}"
    )


def is_marked_profile(profile: UserProfile) -> bool:
    """Помечен ли аккаунт: спамер, много банов или плохая репутация.

    Используется превентивным мутом новых участников
    (``marked_mute_enabled`` в настройках чата).

    :param profile: глобальный профиль пользователя.
    """
    if profile.is_spammer:
        return True
    if len(profile.ban_marks or []) >= config.MARKED_MUTE_BAN_MARKS:
        return True
    return int(profile.reputation or 0) < config.MARKED_MUTE_REPUTATION


def build_marked_mute_notice_text(name: str, duration_seconds: int) -> str:
    """Сообщение в чат о превентивном муте.

    Блок сообщения о входе собирает :func:`build_entry_mute_notice_text`:
    он умеет скрывать имя в анонимном режиме. Эта функция остаётся для
    мест, где участника можно называть по имени.

    :param name: имя участника (экранируется).
    :param duration_seconds: срок мута в секундах.
    """
    return (
        "🔇 Пользователь <b>{}</b> получил превентивный мут~\n"
        "📝 Причина: подозрительный аккаунт\n"
        "⏱ Срок: {}"
    ).format(escape_text(name), time_parser.format_duration(duration_seconds))


def build_join_mute_notice_text(name: str, duration_seconds: int) -> str:
    """Сообщение в чат о муте при входе для всех новичков.

    Блок сообщения о входе собирает :func:`build_entry_mute_notice_text`:
    он умеет скрывать имя в анонимном режиме. Эта функция остаётся для
    мест, где участника можно называть по имени.

    :param name: имя участника (экранируется).
    :param duration_seconds: срок мута в секундах.
    """
    return (
        "🔇 Новый участник <b>{}</b> получил мут при входе~\n"
        "📝 Причина: мут для всех новичков\n"
        "⏱ Срок: {}"
    ).format(escape_text(name), time_parser.format_duration(duration_seconds))


#: Подписи причин мута при входе для блока в сообщении о входе.
ENTRY_MUTE_REASON_LABELS: Final[dict[str, str]] = {
    config.MARKED_MUTE_REASON: "подозрительный аккаунт (метки банов)",
    config.JOIN_MUTE_REASON: "мут для всех новичков",
    config.WELCOME_CHECK_REASON: "проверка нового участника",
}


def build_entry_mute_notice_text(
    duration_seconds: int,
    *,
    reason: str = "",
    anonymous: bool = False,
    name: str = "",
) -> str:
    """Блок «инфо о муте» для сообщения о входе участника.

    Уходит ВМЕСТЕ с приветствием и сводкой, а не вместо них. В анонимном
    режиме имя участника не показывается: личность новичка не раскрываем.

    :param duration_seconds: срок мута в секундах.
    :param reason: причина мута из :func:`apply_entry_mute` (``events``).
    :param anonymous: анонимный режим — имя скрыто.
    :param name: имя участника (экранируется), если его можно показывать.
    """
    label = ENTRY_MUTE_REASON_LABELS.get(reason, "")
    if anonymous or not name:
        lines = ["🔇 Новый участник получил мут"]
    else:
        lines = [f"🔇 Новый участник <b>{escape_text(name)}</b> получил мут"]
    lines.append(f"⏱ Срок: {time_parser.format_duration(duration_seconds)}")
    if label:
        lines.append(f"📝 Причина: {label}")
    return "\n".join(lines)


def build_content_saved_text(kind: str) -> str:
    """Подтверждение сохранения правил/приветствия.

    Для правил — предложение превью, для приветствия — вопрос о кнопках
    (шаг 2 редактора).

    :param kind: ``rules`` или ``greeting``.
    """
    if kind == "rules":
        return (
            "✅ Правила сохранены!\n\n"
            "Хочешь посмотреть как это будет выглядеть?"
        )
    return build_greeting_buttons_question_text()


def build_rules_disabled_text() -> str:
    """Ответ на «.правила», когда правила не настроены."""
    return "📜 Правила тут пока не настроены~ Владелец может добавить их в настройках 🌸"


def build_rules_not_found_text() -> str:
    """Ответ, если правила включены, но пусты."""
    return "📜 Правила пока пустые~ Владелец скоро их добавит 🌸"


def build_greeting_disabled_text() -> str:
    """Ответ на «.приветствие», когда приветствие не настроено."""
    return (
        "👋 Своё приветствие тут пока не настроено~\n"
        "Владелец может задать его в настройках бота 🌸"
    )


def build_greeting_preview_note_text() -> str:
    """Пояснение к предпросмотру приветствия.

    Сам предпросмотр — ровно текст владельца: бот ничего не добавляет
    «от себя», поэтому шапки вида «Привет, имя» здесь не бывает.
    """
    return "👁 Превью приветствия — так увидят его новые участники:"


def build_greeting_dm_failed_text() -> str:
    """Ответ, если предпросмотр нельзя прислать в личку."""
    return "Не смогла написать тебе в личку~ Открой меня и нажми /start 🌸"


def build_content_cleared_text(kind: str) -> str:
    """Подтверждение очистки блока."""
    what = "Правила" if kind == "rules" else "Приветствие"
    return f"🧹 {what} очищены и выключены~"


def build_rules_command_help_text() -> str:
    """Строка-подсказка для справки о команде правил."""
    return f"📜 .{config.COMMAND_RULES} — показать правила чата"

