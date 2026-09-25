"""Конфигурация бота YamoChan.

Здесь собраны все константы проекта. Значения, зависящие от окружения,
читаются из файла ``.env`` (образец — ``.env.example``).
Глобальных изменяемых переменных в проекте нет: всё состояние живёт
в базе данных, в ``workflow_data`` диспетчера или в локальных объектах.
"""

from __future__ import annotations

import logging
import os
from datetime import timezone
from pathlib import Path
from typing import Any, Final

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Пути и окружение
# ---------------------------------------------------------------------------
BASE_DIR: Final[Path] = Path(__file__).resolve().parent
ENV_FILE: Final[Path] = BASE_DIR / ".env"

# Читаем .env один раз при импорте модуля.
load_dotenv(dotenv_path=ENV_FILE)

# ---------------------------------------------------------------------------
# Основные настройки бота
# ---------------------------------------------------------------------------
BOT_TOKEN: Final[str] = os.getenv("BOT_TOKEN", "").strip()
BOT_NAME: Final[str] = "YamoChan"
#: Версия бота — показывается в логах, приветствии ``/start`` и справке.
BOT_VERSION: Final[str] = "1.0.0"

#: Путь к файлу базы данных SQLite (в подпапке database/, как в стабильных проектах).
DB_PATH: Final[Path] = Path(
    os.getenv("DB_PATH", str(BASE_DIR / "database" / "yamochan.db"))
).expanduser()

#: Часовой пояс всего проекта — только UTC.
UTC: Final[timezone] = timezone.utc

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT: Final[str] = "[%(asctime)s] %(levelname)s: %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# Супер-админы бота (опционально): им разрешены команды модерации
# в любом чате, где присутствует бот.
# ---------------------------------------------------------------------------
SUPERADMIN_IDS: Final[frozenset[int]] = frozenset(
    int(chunk)
    for chunk in os.getenv("SUPERADMIN_IDS", "").replace(";", ",").split(",")
    if chunk.strip().lstrip("-").isdigit()
)

# ---------------------------------------------------------------------------
# Владелец бота и админ-панель (команды ``.adm``, ``/admin``, «админ»)
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int = 0) -> int:
    """Прочитать целое число из окружения (мусор не роняет импорт конфига).

    :param name: имя переменной окружения.
    :param default: значение, если переменная не задана или некорректна.
    """
    raw = os.getenv(name, "").strip() or str(default)
    return int(raw) if raw.lstrip("-").isdigit() else int(default)


#: ID владельца бота (супер-админ): только он видит админ-панель.
#: ``0`` — панель выключена, команда молча ничего не делает.
BOT_OWNER_ID: Final[int] = _env_int("BOT_OWNER_ID", 0)

if BOT_OWNER_ID <= 0:
    # Сообщаем сразу при импорте: забытая строка в .env — самая частая причина
    # «/admin не работает», а по логам это иначе не видно.
    logger.warning(
        "BOT_OWNER_ID не задан в %s — админ-панель (/adm) не будет отвечать. "
        "Добавь строку BOT_OWNER_ID=<твой Telegram ID> и перезапусти бота.",
        ENV_FILE,
    )

#: Имена команды входа в админ-панель (префикс не важен: ``.adm``, ``/adm``, ``adm``).
ADMIN_COMMANDS: Final[tuple[str, ...]] = ("adm", "admin", "адм", "админ")

#: Сколько элементов показывать на одной странице списков админ-панели.
ADMIN_PAGE_SIZE: Final[int] = 5
#: Сколько последних действий показывать на странице журнала админа.
ADMIN_LOG_PAGE_SIZE: Final[int] = 20
#: Сколько чатов и пользователей показывать в топах расширенной статистики.
ADMIN_TOP_LIMIT: Final[int] = 5
#: Сколько сообщений отправлять между обновлениями прогресса рассылки.
ADMIN_BROADCAST_PROGRESS_STEP: Final[int] = 5
#: Сколько дней показывать в графике активности админ-панели.
ADMIN_ACTIVITY_DAYS: Final[int] = 7
#: Через сколько секунд можно повторно уведомлять забаненного пользователя.
ADMIN_BAN_NOTICE_COOLDOWN: Final[int] = 300
#: Сколько дней хранить журнал действий админа (кнопка «Очистить старые логи»).
ADMIN_LOG_TTL_DAYS: Final[int] = 30

#: Причины бана в боте: ключ кнопки → текст причины (и подпись кнопки).
ADMIN_BAN_REASONS: Final[dict[str, str]] = {
    "spam": "Спам/реклама",
    "rules": "Нарушение правил",
    "scam": "Мошенничество",
    "none": "Без причины",
}

#: Текст уведомления пользователю, забаненному в боте.
ADMIN_BANNED_MESSAGE: Final[str] = (
    "🚫 Ваш аккаунт заблокирован в YamoChan.\n\n"
    "Причина: {reason}\n\n"
    "Обратитесь к администрации для разблокировки."
)
#: Короткий ответ забаненному на нажатие кнопки (лимит Telegram — 200 символов).
ADMIN_BANNED_ALERT: Final[str] = "🚫 Ваш аккаунт заблокирован в YamoChan."
#: Текст уведомления о снятии бана в боте.
ADMIN_UNBANNED_MESSAGE: Final[str] = (
    "✅ Ваш аккаунт разблокирован в YamoChan!\n"
    "Вы снова можете пользоваться ботом~ 💕"
)
#: Что бот пишет в чат при отвязке по решению службы безопасности.
ADMIN_DETACH_CHAT_MESSAGE: Final[str] = (
    "⚠️ Внимание!\n\n"
    "Данный чат попал под проверку службы безопасности.\n"
    "Бот YamoChan отвязан от этого чата.\n\n"
    "По вопросам обращайтесь через систему жалоб в ЛС бота."
)
#: Что бот пишет владельцу чата при отвязке (шаблон с ``{title}``).
ADMIN_DETACH_OWNER_MESSAGE: Final[str] = (
    "⚠️ Уведомление от {bot}\n\n"
    "Ваш чат «{title}» был отвязан от бота по решению\n"
    "службы безопасности.\n\n"
    "Если вы считаете это ошибкой — отправьте жалобу\n"
    "через кнопку «📩 Оставить жалобу» в главном меню."
)

# ---------------------------------------------------------------------------
# Жалобы пользователей
# ---------------------------------------------------------------------------
#: Категории жалоб: ключ кнопки → подпись.
COMPLAINT_REASONS: Final[dict[str, str]] = {
    "moderation_abuse": "🔨 Злоупотребление модерацией",
    "unfair_ban": "🚫 Несправедливый бан",
    "bot_error": "🤖 Ошибка бота",
    "chat_problem": "💬 Проблема с чатом",
    "bot_spam": "📢 Спам/реклама от бота",
    "other": "❓ Другое",
}

#: Стандартные ответы администрации по жалобе (кнопка «📝 Стандартный ответ»).
COMPLAINT_STANDARD_ACCEPT: Final[str] = (
    "Ваша жалоба рассмотрена и принята. Меры приняты."
)
COMPLAINT_STANDARD_REJECT: Final[str] = (
    "Жалоба рассмотрена. Нарушений не обнаружено."
)

#: Сколько символов описания показывать в предпросмотре и списках жалоб.
COMPLAINT_PREVIEW_LENGTH: Final[int] = 200
COMPLAINT_LIST_PREVIEW_LENGTH: Final[int] = 50
#: Лимит подписи к фото в Telegram (длинные уведомления уходят двумя сообщениями).
PHOTO_CAPTION_LIMIT: Final[int] = 1000

#: Статусы жалобы: открыта, принята, отклонена.
COMPLAINT_STATUS_OPEN: Final[str] = "open"
COMPLAINT_STATUS_ACCEPTED: Final[str] = "accepted"
COMPLAINT_STATUS_REJECTED: Final[str] = "rejected"
#: Человекочитаемые названия статусов жалобы.
COMPLAINT_STATUS_TITLES: Final[dict[str, str]] = {
    COMPLAINT_STATUS_OPEN: "⏳ Открыта",
    COMPLAINT_STATUS_ACCEPTED: "✅ Принята",
    COMPLAINT_STATUS_REJECTED: "❌ Отклонена",
}
#: Короткие значки статусов для списков жалоб.
COMPLAINT_STATUS_ICONS: Final[dict[str, str]] = {
    COMPLAINT_STATUS_OPEN: "⏳",
    COMPLAINT_STATUS_ACCEPTED: "✅",
    COMPLAINT_STATUS_REJECTED: "❌",
}

#: Минимальная и максимальная длина описания жалобы.
COMPLAINT_MIN_LENGTH: Final[int] = 10
COMPLAINT_MAX_LENGTH: Final[int] = 1000

# ---------------------------------------------------------------------------
# Команды модерации (префикс — «.», «/», «!» или без него)
# ---------------------------------------------------------------------------
#: Классический префикс команд; остальные варианты — в
#: :mod:`yamochan.utils.command_filter`.
COMMAND_PREFIX: Final[str] = "."

COMMAND_BAN: Final[str] = "бан"
COMMAND_UNBAN: Final[str] = "разбан"
COMMAND_MUTE: Final[str] = "мут"
COMMAND_UNMUTE: Final[str] = "размут"
COMMAND_WARN: Final[str] = "варн"
COMMAND_UNWARN: Final[str] = "снятьварн"
COMMAND_KICK: Final[str] = "кик"
COMMAND_INFO: Final[str] = "инфо"
COMMAND_OPEN: Final[str] = "открыть"
#: Служебная команда: сбросить кэш админов и пересинхронизировать владельца.
COMMAND_SYNC: Final[str] = "синк"

#: Полный список команд модерации (используется в настройках чата).
MODERATION_COMMANDS: Final[tuple[str, ...]] = (
    COMMAND_BAN,
    COMMAND_UNBAN,
    COMMAND_MUTE,
    COMMAND_UNMUTE,
    COMMAND_WARN,
    COMMAND_UNWARN,
    COMMAND_KICK,
    COMMAND_INFO,
    COMMAND_OPEN,
)


# ---------------------------------------------------------------------------
# Наказания и репутация
# ---------------------------------------------------------------------------
TYPE_BAN: Final[str] = "ban"
TYPE_MUTE: Final[str] = "mute"
TYPE_KICK: Final[str] = "kick"

PUNISHMENT_TYPES: Final[tuple[str, ...]] = (TYPE_BAN, TYPE_MUTE, TYPE_KICK)
PUNISHMENT_TITLES: Final[dict[str, str]] = {
    TYPE_BAN: "бан",
    TYPE_MUTE: "мут",
    TYPE_KICK: "кик",
}

#: Максимальное количество предупреждений до автоматического бана.
MAX_WARNS: Final[int] = 3

#: Причина автоматического бана после третьего варна.
AUTO_BAN_REASON: Final[str] = "3/3 предупреждений — автоматический бан 🔨"

#: Причина бана за спам/флуд (антиспам).
SPAM_BAN_REASON: Final[str] = "Спам/флуд: слишком много сообщений подряд 🚫"

#: Изменение репутации за действия модерации.
REPUTATION_BAN: Final[int] = -10
REPUTATION_UNBAN: Final[int] = 5
REPUTATION_MUTE: Final[int] = -5
REPUTATION_UNMUTE: Final[int] = 3
REPUTATION_WARN: Final[int] = -3
REPUTATION_UNWARN: Final[int] = 2

#: Минимальная длительность мута/бана в секундах (Telegram игнорирует < 30 сек).
MIN_PUNISH_TIME_SECONDS: Final[int] = 60

#: Всё, что больше 366 дней, Telegram трактует как бессрочное ограничение.
MAX_PUNISH_TIME_SECONDS: Final[int] = 366 * 24 * 60 * 60

#: Порог «плохой» репутации для профиля.
BAD_REPUTATION_THRESHOLD: Final[int] = -10

# ---------------------------------------------------------------------------
# Приватность упоминаний
# ---------------------------------------------------------------------------
#: Нейтральная подпись вместо имени админа/владельца в публичных сообщениях.
ADMIN_NEUTRAL_LABEL: Final[str] = "Администрация"
#: Сколько секунд живёт кэш списка админов чата (защита от лишних запросов).
ADMIN_CACHE_TTL: Final[int] = 300

# ---------------------------------------------------------------------------
# Антирейд
# ---------------------------------------------------------------------------
#: Время мута новичков при включённом антирейде (секунды).
ANTIRAID_MUTE_SECONDS: Final[int] = 10 * 60
#: Сколько человек за окно разрешено впустить.
ANTIRAID_JOIN_LIMIT: Final[int] = 5
#: Длина окна подсчёта входов (секунды).
ANTIRAID_WINDOW_SECONDS: Final[int] = 60

# ---------------------------------------------------------------------------
# Активность и фоновые задачи
# ---------------------------------------------------------------------------
#: Окно подсчёта «активности за сутки».
ACTIVITY_WINDOW_HOURS: Final[int] = 24
#: Сколько часов хранить журнал сообщений (для быстрых агрегатов).
MESSAGE_LOG_TTL_HOURS: Final[int] = 48
#: Как часто фоновый воркер проверяет истёкшие наказания (секунды).
EXPIRATION_CHECK_INTERVAL: Final[int] = 30
#: Пауза перед перезапуском long-polling после ошибки (секунды).
POLLING_RESTART_DELAY: Final[int] = 5
#: Сколько последних чатов показывать в «Мои чаты».
MAX_CHATS_IN_LIST: Final[int] = 12
#: Сколько чатов с варнами показывать в карточке профиля.
MAX_WARN_CHATS_IN_PROFILE: Final[int] = 5
#: Сколько чатов максимум проверять в Telegram при ``/start`` в личке
#: (поиск чатов, где пользователь — создатель, для связи user_id ↔ chat_id).
MAX_CHATS_FOR_OWNER_SYNC: Final[int] = 50

# ---------------------------------------------------------------------------
# Антирейд и антиспам (значения по умолчанию и границы настройки)
# ---------------------------------------------------------------------------
#: Включён ли антирейд по умолчанию.
ANTIRAID_DEFAULT_ENABLED: Final[bool] = False
#: Порог входов: сколько человек за окно считаются рейдом.
ANTIRAID_DEFAULT_THRESHOLD: Final[int] = 5
#: Окно подсчёта входов по умолчанию (секунды).
ANTIRAID_DEFAULT_TIMEFRAME: Final[int] = 300
ANTIRAID_MIN_THRESHOLD: Final[int] = 2
ANTIRAID_MAX_THRESHOLD: Final[int] = 100
ANTIRAID_MIN_TIMEFRAME: Final[int] = 5
ANTIRAID_MAX_TIMEFRAME: Final[int] = 24 * 60 * 60

#: Режимы антиспама: с закрытием чата и без него.
ANTISPAM_MODE_CLOSE: Final[str] = "close"
ANTISPAM_MODE_NO_CLOSE: Final[str] = "no_close"
ANTISPAM_MODES: Final[tuple[str, ...]] = (ANTISPAM_MODE_CLOSE, ANTISPAM_MODE_NO_CLOSE)
#: Человеческие названия режимов антиспама.
ANTISPAM_MODE_TITLES: Final[dict[str, str]] = {
    ANTISPAM_MODE_CLOSE: "с закрытием чата",
    ANTISPAM_MODE_NO_CLOSE: "без закрытия чата",
}

#: Порог антиспама по умолчанию: сообщений за окно.
SPAM_DEFAULT_THRESHOLD: Final[int] = 7
#: Окно антиспама по умолчанию (секунды).
SPAM_DEFAULT_TIMEFRAME: Final[int] = 10
SPAM_MIN_THRESHOLD: Final[int] = 3
SPAM_MAX_THRESHOLD: Final[int] = 100
SPAM_MIN_TIMEFRAME: Final[int] = 3
SPAM_MAX_TIMEFRAME: Final[int] = 60 * 60

#: Сколько секунд держать в памяти идентификаторы сообщений (для удаления).
RECENT_MESSAGE_TTL_SECONDS: Final[int] = 15 * 60

#: Сколько последних сообщений удалять при срабатывании антирейда/антиспама.
MAX_MESSAGES_TO_DELETE: Final[int] = 100

#: Пауза антирейда при снятии защиты (секунды), чтобы не срабатывал повторно.
ANTIRAID_COOLDOWN_SECONDS: Final[int] = 60

# ---------------------------------------------------------------------------
# Call режим (зов всех участников чата)
# ---------------------------------------------------------------------------
#: Имена команд вызова (``.``, ``/``, ``!`` или без префикса).
CALL_COMMANDS: Final[tuple[str, ...]] = ("калл", "call")
#: Сколько упоминаний помещается в одно сообщение Telegram.
CALL_BATCH_SIZE: Final[int] = 50
#: Эмодзи-заглушка по умолчанию (режим «эмодзи вместо имён»).
CALL_EMOJI_DEFAULT: Final[str] = "👤"
#: Сколько отложенных вызовов разрешено на чат.
CALL_MAX_SCHEDULED: Final[int] = 20
#: Минимальная задержка отложенного вызова (секунды).
CALL_SCHEDULED_MIN_DELAY: Final[int] = 30
#: Максимальная задержка отложенного вызова (секунды) — 30 суток.
CALL_SCHEDULED_MAX_DELAY: Final[int] = 30 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# Правила чата и приветствие (тексты владельца с премиум-эмодзи и фото)
# ---------------------------------------------------------------------------
#: Команда показа правил в группе: ``.правила`` / ``/правила``.
COMMAND_RULES: Final[str] = "правила"
#: Команда предпросмотра приветствия: ``.приветствие`` / ``/приветствие``.
COMMAND_GREETING: Final[str] = "приветствие"
#: Максимальная длина текста правил/приветствия (лимит подписи Telegram ~4096).
RICH_TEXT_LIMIT: Final[int] = 3900
#: Плейсхолдер имени участника внутри приветствия.
NAME_PLACEHOLDER: Final[str] = "{name}"
#: Плейсхолдер идентификатора участника.
ID_PLACEHOLDER: Final[str] = "{id}"
#: Плейсхолдер упоминания участника (ссылка ``tg://user?id=…``).
MENTION_PLACEHOLDER: Final[str] = "{mention}"
#: Плейсхолдер названия чата.
CHAT_PLACEHOLDER: Final[str] = "{chat}"
#: Плейсхолдер количества участников чата.
COUNT_PLACEHOLDER: Final[str] = "{count}"
#: Все поддерживаемые спец-команды текстов владельца.
PLACEHOLDERS: Final[tuple[str, ...]] = (
    NAME_PLACEHOLDER,
    ID_PLACEHOLDER,
    MENTION_PLACEHOLDER,
    CHAT_PLACEHOLDER,
    COUNT_PLACEHOLDER,
)
#: Сколько первых символов правил показывать в меню настроек.
RICH_PREVIEW_LENGTH: Final[int] = 200

# ---------------------------------------------------------------------------
# Инлайн-кнопки приветствия (их собирает владелец в FSM)
# ---------------------------------------------------------------------------
#: Максимум кнопок под приветствием.
MAX_GREETING_BUTTONS: Final[int] = 5
#: Лимит длины подписи кнопки в Telegram.
MAX_BUTTON_TEXT_LENGTH: Final[int] = 64
#: Разрешённые схемы ссылок кнопок.
BUTTON_URL_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://", "tg://")
#: Короткие домены: к ним схема ``https://`` дописывается сама.
BUTTON_URL_SHORT_PREFIXES: Final[tuple[str, ...]] = ("t.me/", "telegram.me/")
#: Сколько кнопок ставить в один ряд.
BUTTONS_PER_ROW: Final[int] = 2

# ---------------------------------------------------------------------------
# Цвета инлайн-кнопок (Bot API: style = danger / success / primary)
# ---------------------------------------------------------------------------
#: Красный: кнопки возврата «назад» и удаляющих действий.
BUTTON_STYLE_DANGER: Final[str] = "danger"
#: Зелёный: кнопки добавления и настройки в положении «включено».
BUTTON_STYLE_SUCCESS: Final[str] = "success"
#: Синий: кнопки-гайды (вход в FAQ).
BUTTON_STYLE_PRIMARY: Final[str] = "primary"
#: Раскраска кнопок; ``BUTTON_STYLES=0`` в .env выключает её для старых
#: серверов Bot API, которые ещё не знают поле ``style``.
BUTTON_STYLES_ENABLED: Final[bool] = (
    os.getenv("BUTTON_STYLES", "1").strip().lower() not in {"0", "false", "no", "off"}
)

# ---------------------------------------------------------------------------
# Превентивные муты помеченным участникам
# ---------------------------------------------------------------------------
#: Длительность превентивного мута по умолчанию (30 минут).
MARKED_MUTE_DEFAULT_SECONDS: Final[int] = 30 * 60
#: Минимальная и максимальная длительность превентивного мута.
MARKED_MUTE_MIN_SECONDS: Final[int] = 60
MARKED_MUTE_MAX_SECONDS: Final[int] = 30 * 24 * 60 * 60
#: Сколько меток банов считается «плохой» репутацией аккаунта.
MARKED_MUTE_BAN_MARKS: Final[int] = 5
#: Порог репутации, ниже которого аккаунт считается помеченным.
MARKED_MUTE_REPUTATION: Final[int] = -20
#: Причина превентивного мута (для записи в базе и текста в чате).
MARKED_MUTE_REASON: Final[str] = "Подозрительный аккаунт: метки банов / плохая репутация"

# ---------------------------------------------------------------------------
# Отключённые модер-команды
# ---------------------------------------------------------------------------
#: Настройка чата: список модер-команд, выключенных для админов.
#: Владелец чата и супер-админы обходят этот список всегда.
DISABLED_MOD_COMMANDS_KEY: Final[str] = "disabled_mod_commands"
#: Ответ админу, который вызвал выключенную для админов команду.
DISABLED_COMMAND_MESSAGE: Final[str] = (
    "Эта команда отключена для админов~ Только владелец может её использовать."
)

# ---------------------------------------------------------------------------
# Анонимное приветствие и проверочный мут новичка
# ---------------------------------------------------------------------------
#: Настройка чата: не раскрывать данные нового участника при входе.
WELCOME_ANONYMOUS_KEY: Final[str] = "welcome_anonymous"
#: Настройка чата: сколько секунд мутить новичка на время проверки.
WELCOME_CHECK_DURATION_KEY: Final[str] = "welcome_check_duration"
#: Длительность проверочного мута по умолчанию (60 секунд).
WELCOME_CHECK_DEFAULT_SECONDS: Final[int] = 60
#: Границы проверочного мута: Telegram игнорирует ограничения < 30 секунд.
WELCOME_CHECK_MIN_SECONDS: Final[int] = 30
WELCOME_CHECK_MAX_SECONDS: Final[int] = 24 * 60 * 60
#: Причина проверочного мута (для выбора самого долгого мута при входе).
WELCOME_CHECK_REASON: Final[str] = "Проверочный мут нового участника"

# ---------------------------------------------------------------------------
# Мут при входе для всех новых участников
# ---------------------------------------------------------------------------
#: Настройка чата: выдавать мут всем новым участникам при входе.
JOIN_MUTE_ENABLED_KEY: Final[str] = "join_mute_enabled"
#: Настройка чата: длительность мута при входе для всех (секунды).
JOIN_MUTE_DURATION_KEY: Final[str] = "join_mute_duration"
#: Длительность мута при входе по умолчанию (5 минут).
JOIN_MUTE_DEFAULT_SECONDS: Final[int] = 300
#: Границы мута при входе: 30 секунд .. 30 дней (ограничение Telegram).
JOIN_MUTE_MIN_SECONDS: Final[int] = 30
JOIN_MUTE_MAX_SECONDS: Final[int] = 30 * 24 * 60 * 60
#: Причина мута при входе (для выбора самого долгого мута при входе).
JOIN_MUTE_REASON: Final[str] = "Мут при входе для новых участников"

# ---------------------------------------------------------------------------
# Служебные сообщения чата и Call только для админов
# ---------------------------------------------------------------------------
#: Настройка чата: скрывать служебные сообщения (вход/выход, закреп, название).
DELETE_SERVICE_MESSAGES_KEY: Final[str] = "delete_service_messages"
#: Настройка чата: Call-режим доступен только администраторам.
CALL_ADMINS_ONLY_KEY: Final[str] = "call_admins_only"
#: Ответ участнику, когда Call разрешён только админам.
CALL_ADMINS_ONLY_MESSAGE: Final[str] = "📞 Call доступен только админам~ 💕"

# ---------------------------------------------------------------------------
# Настройки чата по умолчанию (json-поле chats.settings)
# ---------------------------------------------------------------------------
DEFAULT_CHAT_SETTINGS: Final[dict[str, Any]] = {
    "nsfw_commands": False,
    "antiraid": False,
    "antiraid_enabled": ANTIRAID_DEFAULT_ENABLED,
    "antiraid_threshold": ANTIRAID_DEFAULT_THRESHOLD,
    "antiraid_timeframe": ANTIRAID_DEFAULT_TIMEFRAME,
    "antiraid_active_protection": False,
    "antispam_mode": ANTISPAM_MODE_NO_CLOSE,
    "spam_msg_threshold": SPAM_DEFAULT_THRESHOLD,
    "spam_msg_timeframe": SPAM_DEFAULT_TIMEFRAME,
    "call_enabled": False,
    "call_mention_admins": True,
    "call_use_emoji": False,
    "call_emoji": CALL_EMOJI_DEFAULT,
    "call_delete_messages": False,
    "call_admins_only": False,
    "call_scheduled": [],
    "rules_enabled": False,
    "rules_show_on_join": False,
    "rules_text": "",
    "rules_entities": [],
    "rules_photo": None,
    "greeting_enabled": False,
    "greeting_show_profile": False,
    "welcome_anonymous": False,
    "welcome_check_duration": WELCOME_CHECK_DEFAULT_SECONDS,
    "greeting_text": "",
    "greeting_entities": [],
    "greeting_photo": None,
    "greeting_buttons": [],
    "marked_mute_enabled": False,
    "marked_mute_duration": MARKED_MUTE_DEFAULT_SECONDS,
    "join_mute_enabled": False,
    "join_mute_duration": JOIN_MUTE_DEFAULT_SECONDS,
    "delete_service_messages": True,
    "disabled_mod_commands": [],
    "commands": {command: True for command in MODERATION_COMMANDS},
}

# ---------------------------------------------------------------------------
# Текстовые шаблоны, используемые в нескольких модулях
# ---------------------------------------------------------------------------
ERROR_MESSAGE: Final[str] = "Ой, что-то пошло не так~ 💔 Попробуй чуть позже."
NOT_ADMIN_MESSAGE: Final[str] = (
    "Прости, но команды модерации доступны только админам чата~ 🌸"
)
PROTECTED_TARGET_MESSAGE: Final[str] = (
    "Этого пользователя я тронуть не могу — он админ или владелец чата~ 🛡"
)
NO_TARGET_MESSAGE: Final[str] = (
    "Не поняла, кого наказывать~ 🤔\n"
    "Сделай реплай на сообщение, укажи @юзернейм или ID."
)


#: Подписи состояния переключателей (кнопки настроек и тексты).
STATE_ON: Final[str] = "✅ ВКЛ"
STATE_OFF: Final[str] = "❌ ВЫКЛ"

#: Читаемые названия команд для кнопок «Настройка команд модератора».
COMMAND_TITLES: Final[dict[str, str]] = {
    COMMAND_BAN: "🔨 .бан",
    COMMAND_UNBAN: "💖 .разбан",
    COMMAND_MUTE: "🤫 .мут",
    COMMAND_UNMUTE: "🎉 .размут",
    COMMAND_WARN: "⚠️ .варн",
    COMMAND_UNWARN: "💝 .снятьварн",
    COMMAND_KICK: "👋 .кик",
    COMMAND_INFO: "📋 .инфо",
    COMMAND_OPEN: "🔓 .открыть",
}
