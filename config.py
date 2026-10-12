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

BASE_DIR: Final[Path] = Path(__file__).resolve().parent
ENV_FILE: Final[Path] = BASE_DIR / ".env"
LOCAL_ENV_FILE: Final[Path] = BASE_DIR / ".env.local"

load_dotenv(dotenv_path=ENV_FILE)
load_dotenv(dotenv_path=LOCAL_ENV_FILE, override=True)

BOT_TOKEN: Final[str] = os.getenv("BOT_TOKEN", "").strip()
BOT_NAME: Final[str] = "YamoChan"
BOT_VERSION: Final[str] = "1.2.0"

DB_PATH: Final[Path] = Path(
    os.getenv("DB_PATH", str(BASE_DIR / "data" / "yamochan.db"))
).expanduser()

UTC: Final[timezone] = timezone.utc

LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT: Final[str] = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"
LOG_DIR: Final[Path] = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs"))).expanduser()
LOG_FILE: Final[Path] = LOG_DIR / "yamochan.log"
ERROR_LOG_FILE: Final[Path] = LOG_DIR / "yamochan_errors.log"
LOG_FILE_MAX_BYTES: Final[int] = 10 * 1024 * 1024
LOG_FILE_BACKUPS: Final[int] = 5

SUPERADMIN_IDS: Final[frozenset[int]] = frozenset(
    int(chunk)
    for chunk in os.getenv("SUPERADMIN_IDS", "").replace(";", ",").split(",")
    if chunk.strip().lstrip("-").isdigit()
)

def _env_int(name: str, default: int = 0) -> int:
    """Прочитать целое число из окружения (мусор не роняет импорт конфига).

    :param name: имя переменной окружения.
    :param default: значение, если переменная не задана или некорректна.
    """
    raw = os.getenv(name, "").strip() or str(default)
    return int(raw) if raw.lstrip("-").isdigit() else int(default)


BOT_OWNER_ID: Final[int] = _env_int("BOT_OWNER_ID", 0)

if BOT_OWNER_ID <= 0:
    logger.warning(
        "BOT_OWNER_ID не задан в %s — админ-панель (/adm) не будет отвечать. "
        "Добавь строку BOT_OWNER_ID=<твой Telegram ID> и перезапусти бота.",
        LOCAL_ENV_FILE if LOCAL_ENV_FILE.exists() else ENV_FILE,
    )

ADMIN_COMMANDS: Final[tuple[str, ...]] = ("adm", "admin", "адм", "админ")

ADMIN_PAGE_SIZE: Final[int] = 5
ADMIN_LOG_PAGE_SIZE: Final[int] = 20
ADMIN_TOP_LIMIT: Final[int] = 5
ADMIN_BROADCAST_PROGRESS_STEP: Final[int] = 5
ADMIN_ACTIVITY_DAYS: Final[int] = 7
ADMIN_BAN_NOTICE_COOLDOWN: Final[int] = 300
ADMIN_LOG_TTL_DAYS: Final[int] = 30

ADMIN_BAN_REASONS: Final[dict[str, str]] = {
    "spam": "Спам/реклама",
    "rules": "Нарушение правил",
    "scam": "Мошенничество",
    "none": "Без причины",
}

ADMIN_BANNED_MESSAGE: Final[str] = (
    "🚫 Ваш аккаунт заблокирован в YamoChan.\n\n"
    "Причина: {reason}\n\n"
    "Обратитесь к администрации для разблокировки."
)
ADMIN_BANNED_ALERT: Final[str] = "🚫 Ваш аккаунт заблокирован в YamoChan."
ADMIN_UNBANNED_MESSAGE: Final[str] = (
    "✅ Ваш аккаунт разблокирован в YamoChan!\n"
    "Вы снова можете пользоваться ботом~ 💕"
)
ADMIN_DETACH_CHAT_MESSAGE: Final[str] = (
    "⚠️ Внимание!\n\n"
    "Данный чат попал под проверку службы безопасности.\n"
    "Бот YamoChan отвязан от этого чата.\n\n"
    "По вопросам обращайтесь через систему жалоб в ЛС бота."
)
ADMIN_DETACH_OWNER_MESSAGE: Final[str] = (
    "⚠️ Уведомление от {bot}\n\n"
    "Ваш чат «{title}» был отвязан от бота по решению\n"
    "службы безопасности.\n\n"
    "Если вы считаете это ошибкой — отправьте жалобу\n"
    "через кнопку «📩 Оставить жалобу» в главном меню."
)

COMPLAINT_REASONS: Final[dict[str, str]] = {
    "moderation_abuse": "🔨 Злоупотребление модерацией",
    "unfair_ban": "🚫 Несправедливый бан",
    "bot_error": "🤖 Ошибка бота",
    "chat_problem": "💬 Проблема с чатом",
    "bot_spam": "📢 Спам/реклама от бота",
    "other": "❓ Другое",
}

COMPLAINT_STANDARD_ACCEPT: Final[str] = (
    "Ваша жалоба рассмотрена и принята. Меры приняты."
)
COMPLAINT_STANDARD_REJECT: Final[str] = (
    "Жалоба рассмотрена. Нарушений не обнаружено."
)

COMPLAINT_PREVIEW_LENGTH: Final[int] = 200
COMPLAINT_LIST_PREVIEW_LENGTH: Final[int] = 50
PHOTO_CAPTION_LIMIT: Final[int] = 1000

COMPLAINT_STATUS_OPEN: Final[str] = "open"
COMPLAINT_STATUS_ACCEPTED: Final[str] = "accepted"
COMPLAINT_STATUS_REJECTED: Final[str] = "rejected"
COMPLAINT_STATUS_TITLES: Final[dict[str, str]] = {
    COMPLAINT_STATUS_OPEN: "⏳ Открыта",
    COMPLAINT_STATUS_ACCEPTED: "✅ Принята",
    COMPLAINT_STATUS_REJECTED: "❌ Отклонена",
}
COMPLAINT_STATUS_ICONS: Final[dict[str, str]] = {
    COMPLAINT_STATUS_OPEN: "⏳",
    COMPLAINT_STATUS_ACCEPTED: "✅",
    COMPLAINT_STATUS_REJECTED: "❌",
}

COMPLAINT_MIN_LENGTH: Final[int] = 10
COMPLAINT_MAX_LENGTH: Final[int] = 1000

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
COMMAND_SYNC: Final[str] = "синк"
COMMAND_DIAGNOSTICS: Final[str] = "диагностика"

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


TYPE_BAN: Final[str] = "ban"
TYPE_MUTE: Final[str] = "mute"
TYPE_KICK: Final[str] = "kick"

PUNISHMENT_TYPES: Final[tuple[str, ...]] = (TYPE_BAN, TYPE_MUTE, TYPE_KICK)
PUNISHMENT_TITLES: Final[dict[str, str]] = {
    TYPE_BAN: "бан",
    TYPE_MUTE: "мут",
    TYPE_KICK: "кик",
}

MAX_WARNS: Final[int] = 3

AUTO_BAN_REASON: Final[str] = "3/3 предупреждений — автоматический бан 🔨"

SPAM_BAN_REASON: Final[str] = "Спам/флуд: слишком много сообщений подряд 🚫"

REPUTATION_BAN: Final[int] = -10
REPUTATION_UNBAN: Final[int] = 5
REPUTATION_MUTE: Final[int] = -5
REPUTATION_UNMUTE: Final[int] = 3
REPUTATION_WARN: Final[int] = -3
REPUTATION_UNWARN: Final[int] = 2

MIN_PUNISH_TIME_SECONDS: Final[int] = 60

MAX_PUNISH_TIME_SECONDS: Final[int] = 366 * 24 * 60 * 60

BAD_REPUTATION_THRESHOLD: Final[int] = -10

ADMIN_NEUTRAL_LABEL: Final[str] = "Администрация"
ADMIN_CACHE_TTL: Final[int] = 300

ANTIRAID_JOIN_LIMIT: Final[int] = 5
ANTIRAID_WINDOW_SECONDS: Final[int] = 60

ACTIVITY_WINDOW_HOURS: Final[int] = 24
MESSAGE_LOG_TTL_HOURS: Final[int] = 48
EXPIRATION_CHECK_INTERVAL: Final[int] = 30
POLLING_RESTART_DELAY: Final[int] = 5
UPDATE_CONCURRENCY_LIMIT: Final[int] = 32
TELEGRAM_REQUEST_TIMEOUT: Final[float] = 45.0
TELEGRAM_SESSION_LIMIT: Final[int] = 100
TELEGRAM_RETRY_ATTEMPTS: Final[int] = 5
TELEGRAM_RETRY_BASE_DELAY: Final[float] = 1.0
TELEGRAM_RETRY_MAX_DELAY: Final[float] = 30.0
JOIN_MIN_INTERVAL_MS: Final[int] = 1200
JOIN_QUEUE_MAX_PER_CHAT: Final[int] = 200
JOIN_QUEUE_MAX_TOTAL: Final[int] = 1000
SERVICE_DELETE_BATCH: Final[int] = 100
SERVICE_CLEANUP_QUEUE_MAX: Final[int] = 500
SERVICE_CLEANUP_BATCH_DELAY: Final[float] = 0.5
SERVICE_CLEANUP_INTERVAL: Final[float] = 0.5
JOIN_DEDUP_TTL: Final[float] = 10.0
GREETING_RETRY_DELAYS: Final[tuple[float, ...]] = (3.0, 8.0)
MAX_CHATS_IN_LIST: Final[int] = 12
MAX_WARN_CHATS_IN_PROFILE: Final[int] = 5
MAX_CHATS_FOR_OWNER_SYNC: Final[int] = 50

ANTIRAID_DEFAULT_ENABLED: Final[bool] = False
ANTIRAID_DEFAULT_THRESHOLD: Final[int] = 5
ANTIRAID_DEFAULT_TIMEFRAME: Final[int] = 300
ANTIRAID_MIN_THRESHOLD: Final[int] = 2
ANTIRAID_MAX_THRESHOLD: Final[int] = 100
ANTIRAID_MIN_TIMEFRAME: Final[int] = 5
ANTIRAID_MAX_TIMEFRAME: Final[int] = 24 * 60 * 60

ANTISPAM_MODE_CLOSE: Final[str] = "close"
ANTISPAM_MODE_NO_CLOSE: Final[str] = "no_close"
ANTISPAM_MODES: Final[tuple[str, ...]] = (ANTISPAM_MODE_CLOSE, ANTISPAM_MODE_NO_CLOSE)
ANTISPAM_MODE_TITLES: Final[dict[str, str]] = {
    ANTISPAM_MODE_CLOSE: "с закрытием чата",
    ANTISPAM_MODE_NO_CLOSE: "без закрытия чата",
}

SPAM_DEFAULT_THRESHOLD: Final[int] = 7
SPAM_DEFAULT_TIMEFRAME: Final[int] = 10
SPAM_MIN_THRESHOLD: Final[int] = 3
SPAM_MAX_THRESHOLD: Final[int] = 100
SPAM_MIN_TIMEFRAME: Final[int] = 3
SPAM_MAX_TIMEFRAME: Final[int] = 60 * 60

RECENT_MESSAGE_TTL_SECONDS: Final[int] = 15 * 60

MAX_MESSAGES_TO_DELETE: Final[int] = 100

ANTIRAID_COOLDOWN_SECONDS: Final[int] = 60

CALL_COMMANDS: Final[tuple[str, ...]] = ("калл", "call")
CALL_BATCH_SIZE: Final[int] = 50
CALL_EMOJI_DEFAULT: Final[str] = "👤"
CALL_MAX_SCHEDULED: Final[int] = 20
CALL_SCHEDULED_MIN_DELAY: Final[int] = 30
CALL_SCHEDULED_MAX_DELAY: Final[int] = 30 * 24 * 60 * 60


COMMAND_RULES: Final[str] = "правила"
COMMAND_GREETING: Final[str] = "приветствие"
RICH_TEXT_LIMIT: Final[int] = 3900
NAME_PLACEHOLDER: Final[str] = "{name}"
ID_PLACEHOLDER: Final[str] = "{id}"
MENTION_PLACEHOLDER: Final[str] = "{mention}"
CHAT_PLACEHOLDER: Final[str] = "{chat}"
COUNT_PLACEHOLDER: Final[str] = "{count}"
PLACEHOLDERS: Final[tuple[str, ...]] = (
    NAME_PLACEHOLDER,
    ID_PLACEHOLDER,
    MENTION_PLACEHOLDER,
    CHAT_PLACEHOLDER,
    COUNT_PLACEHOLDER,
)
RICH_PREVIEW_LENGTH: Final[int] = 200

MAX_GREETING_BUTTONS: Final[int] = 5
MAX_BUTTON_TEXT_LENGTH: Final[int] = 64
BUTTON_URL_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://", "tg://")
BUTTON_URL_SHORT_PREFIXES: Final[tuple[str, ...]] = ("t.me/", "telegram.me/")
BUTTONS_PER_ROW: Final[int] = 2

BUTTON_STYLE_DANGER: Final[str] = "danger"
BUTTON_STYLE_SUCCESS: Final[str] = "success"
BUTTON_STYLE_PRIMARY: Final[str] = "primary"
BUTTON_STYLES_ENABLED: Final[bool] = (
    os.getenv("BUTTON_STYLES", "1").strip().lower() not in {"0", "false", "no", "off"}
)

MARKED_MUTE_DEFAULT_SECONDS: Final[int] = 30 * 60
MARKED_MUTE_MIN_SECONDS: Final[int] = 60
MARKED_MUTE_MAX_SECONDS: Final[int] = 30 * 24 * 60 * 60
MARKED_MUTE_BAN_MARKS: Final[int] = 5
MARKED_MUTE_REPUTATION: Final[int] = -20
MARKED_MUTE_REASON: Final[str] = "Подозрительный аккаунт: метки банов / плохая репутация"

DISABLED_MOD_COMMANDS_KEY: Final[str] = "disabled_mod_commands"
DISABLED_COMMAND_MESSAGE: Final[str] = (
    "Эта команда отключена для админов~ Только владелец может её использовать."
)

WELCOME_ANONYMOUS_KEY: Final[str] = "welcome_anonymous"
WELCOME_CHECK_DURATION_KEY: Final[str] = "welcome_check_duration"
WELCOME_CHECK_DEFAULT_SECONDS: Final[int] = 60
WELCOME_CHECK_MIN_SECONDS: Final[int] = 30
WELCOME_CHECK_MAX_SECONDS: Final[int] = 24 * 60 * 60
WELCOME_CHECK_REASON: Final[str] = "Проверочный мут нового участника"

JOIN_MUTE_ENABLED_KEY: Final[str] = "join_mute_enabled"
JOIN_MUTE_DURATION_KEY: Final[str] = "join_mute_duration"
JOIN_MUTE_DEFAULT_SECONDS: Final[int] = 300
JOIN_MUTE_MIN_SECONDS: Final[int] = 30
JOIN_MUTE_MAX_SECONDS: Final[int] = 30 * 24 * 60 * 60
JOIN_MUTE_REASON: Final[str] = "Мут при входе для новых участников"

DELETE_SERVICE_MESSAGES_KEY: Final[str] = "delete_service_messages"
CALL_ADMINS_ONLY_KEY: Final[str] = "call_admins_only"
CALL_ADMINS_ONLY_MESSAGE: Final[str] = "📞 Call доступен только админам~ 💕"

MSK_OFFSET_HOURS: Final[int] = 3
MSK_LABEL: Final[str] = "МСК (UTC+3)"

QUIET_HOURS_ENABLED_KEY: Final[str] = "quiet_hours_enabled"
QUIET_HOURS_START_KEY: Final[str] = "quiet_hours_start"
QUIET_HOURS_END_KEY: Final[str] = "quiet_hours_end"
QUIET_HOURS_DAYS_KEY: Final[str] = "quiet_hours_days"
QUIET_HOURS_MESSAGE_PREFIX: Final[str] = "quiet_hours_message"
QUIET_HOURS_OPEN_MESSAGE_PREFIX: Final[str] = "quiet_hours_open_message"
QUIET_HOURS_ACTIVE_KEY: Final[str] = "quiet_hours_active"
QUIET_HOURS_DEFAULT_START: Final[str] = "23:00"
QUIET_HOURS_DEFAULT_END: Final[str] = "08:00"
QUIET_HOURS_DEFAULT_DAYS: Final[tuple[int, ...]] = (0, 1, 2, 3, 4, 5, 6)
QUIET_HOURS_DEFAULT_MESSAGE: Final[str] = (
    "🌙 Чат закрыт на тихое время~ Возвращаемся позже 💤"
)
QUIET_HOURS_DEFAULT_OPEN_MESSAGE: Final[str] = "☀️ Чат снова открыт~ Добро пожаловать!"
QUIET_HOURS_CHECK_INTERVAL: Final[int] = 30
WEEKDAY_TITLES: Final[tuple[str, ...]] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

RP_ADMIN_LABEL: Final[str] = "Админ"
RP_HIDE_ADMINS_KEY: Final[str] = "rp_hide_admins"
CUSTOM_RP_COMMANDS_KEY: Final[str] = "custom_rp_commands"
MAX_CUSTOM_RP_COMMANDS: Final[int] = 30
MAX_CUSTOM_RP_NAME_LENGTH: Final[int] = 32
MAX_CUSTOM_RP_ACTION_LENGTH: Final[int] = 64

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
    "rp_hide_admins": True,
    "custom_rp_commands": [],
    "quiet_hours_enabled": False,
    "quiet_hours_start": QUIET_HOURS_DEFAULT_START,
    "quiet_hours_end": QUIET_HOURS_DEFAULT_END,
    "quiet_hours_days": list(QUIET_HOURS_DEFAULT_DAYS),
    "quiet_hours_message_text": QUIET_HOURS_DEFAULT_MESSAGE,
    "quiet_hours_message_entities": [],
    "quiet_hours_message_photo": None,
    "quiet_hours_open_message_text": QUIET_HOURS_DEFAULT_OPEN_MESSAGE,
    "quiet_hours_open_message_entities": [],
    "quiet_hours_open_message_photo": None,
    "quiet_hours_active": False,
    "commands": {command: True for command in MODERATION_COMMANDS},
}

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


STATE_ON: Final[str] = "✅ ВКЛ"
STATE_OFF: Final[str] = "❌ ВЫКЛ"

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
