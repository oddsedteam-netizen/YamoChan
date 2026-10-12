"""«Тихое время»: бот закрывает чат по расписанию и присылает сообщение.

Владелец чата задаёт интервал (время начала и конца по МСК) и дни недели.
Пока текущее время попадает в окно, чат закрыт (никто не может писать), а
сразу после закрытия в чат уходит сообщение владельца. Когда окно
заканчивается — чат открывается снова, и (если задано) уходит сообщение
об открытии.

Особенности:
    * время фиксировано в МСК (:data:`config.MSK_OFFSET_HOURS`), чтобы не
      заводить отдельный часовой пояс для каждого чата;
    * окно может переходить через полночь (``23:00 → 08:00``): тогда дни
      недели применяются ко дню начала окна;
    * чат не открывается, если активна антирейд-защита — чтобы не спорить
      с ней за права участников;
    * состояние «сейчас закрыт» хранится в настройках (``quiet_hours_active``)
      и переживает перезапуск бота.

Модуль держит только чистые функции и обход чатов — так его удобно
проверять без Telegram.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Final, Optional, Sequence

from aiogram import Bot

import config
from database import queries
from database.db import Database
from database.models import utcnow
from services import antiraid as antiraid_service
from services import richtext
from utils import error_handler

logger = logging.getLogger(__name__)

_HHMM_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{1,2})[:.\s]?(\d{1,2})$")


def parse_hhmm(raw: Any) -> Optional[tuple[int, int]]:
    """Разобрать время в формате ``ЧЧ:ММ``.

    Принимает ``"23:00"``, ``"8:5"``, ``"8 05"`` и ``"2300"``.

    :param raw: значение из настроек или сообщение владельца.
    :returns: пару ``(часы, минуты)`` или ``None``, если разобрать не вышло.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if ":" not in text and "." not in text and " " not in text and text.isdigit():
        if len(text) == 4:
            text = f"{text[:2]}:{text[2:]}"
        elif len(text) <= 2:
            text = f"{text}:00"
    match = _HHMM_RE.match(text)
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return (hours, minutes)
    return None


def format_hhmm(raw: Any, default: str = "00:00") -> str:
    """Привести время к каноническому виду ``ЧЧ:ММ``.

    :param raw: исходное значение.
    :param default: значение на случай, если разобрать не удалось.
    """
    parsed = parse_hhmm(raw) or parse_hhmm(default) or (0, 0)
    return f"{parsed[0]:02d}:{parsed[1]:02d}"


def normalize_days(raw: Any) -> list[int]:
    """Привести список дней недели к корректному набору (``0`` — понедельник).

    :param raw: значение настройки ``quiet_hours_days``.
    """
    source: Sequence[Any]
    if isinstance(raw, (list, tuple, set, frozenset)):
        source = list(raw)
    else:
        source = list(config.QUIET_HOURS_DEFAULT_DAYS)
    days: list[int] = []
    for item in source:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in days:
            days.append(day)
    return sorted(days) or list(config.QUIET_HOURS_DEFAULT_DAYS)


def days_label(raw: Any) -> str:
    """Человеческая подпись включённых дней недели.

    :param raw: значение настройки ``quiet_hours_days``.
    """
    days = normalize_days(raw)
    if len(days) == 7:
        return "ежедневно"
    return ", ".join(config.WEEKDAY_TITLES[day] for day in days)


def days_short(raw: Any) -> str:
    """Компактная подпись дней для кнопки меню.

    :param raw: значение настройки ``quiet_hours_days``.
    """
    days = normalize_days(raw)
    marks = [(config.WEEKDAY_TITLES[day] if day in days else "·") for day in range(7)]
    return " ".join(marks)


def msk_now() -> datetime:
    """Текущее время в МСК."""
    return utcnow() + timedelta(hours=config.MSK_OFFSET_HOURS)


def is_within_window(
    start: Any,
    end: Any,
    days: Any,
    *,
    moment: Optional[datetime] = None,
) -> bool:
    """Попадает ли момент времени в окно «тихого времени».

    :param start: начало окна (``ЧЧ:ММ``).
    :param end: конец окна (``ЧЧ:ММ``).
    :param days: дни недели, когда окно действует.
    :param moment: момент времени (по умолчанию — текущее МСК).
    """
    now = moment or msk_now()
    current = now.hour * 60 + now.minute
    start_hm = parse_hhmm(start) or (0, 0)
    end_hm = parse_hhmm(end) or (0, 0)
    start_min = start_hm[0] * 60 + start_hm[1]
    end_min = end_hm[0] * 60 + end_hm[1]
    weekday = now.weekday()
    allowed = set(normalize_days(days))

    if start_min == end_min:
        return weekday in allowed
    if start_min < end_min:
        return weekday in allowed and start_min <= current < end_min
    if current >= start_min:
        return weekday in allowed
    if current < end_min:
        return ((weekday - 1) % 7) in allowed
    return False


def quiet_message(settings: dict[str, Any], prefix: str) -> richtext.RichContent:
    """Содержимое сообщения «тихого времени» (текст с сущностями).

    :param settings: настройки чата.
    :param prefix: префикс полей (закрытие или открытие).
    """
    return richtext.content_from_settings(settings, prefix)


def _protection_active(settings: dict[str, Any], chat_id: int) -> bool:
    """Активна ли антирейд-защита (по базе или по памяти менеджера)."""
    if bool(settings.get("antiraid_active_protection")):
        return True
    return antiraid_service.antiraid_manager.is_under_protection(chat_id)


async def _close_chat(
    bot: Bot,
    db: Database,
    chat_id: int,
    settings: dict[str, Any],
) -> None:
    """Закрыть чат и прислать сообщение владельца."""
    if _protection_active(settings, chat_id):
        logger.info(
            "Тихое время: чат %s под защитой антирейда — не закрываю.", chat_id
        )
        return
    closed = await antiraid_service.close_chat(bot, chat_id)
    if not closed:
        logger.warning(
            "Тихое время: не удалось закрыть чат %s — проверь право бота "
            "«Ограничивать участников».",
            chat_id,
        )
        return
    await queries.update_chat_setting(db, chat_id, config.QUIET_HOURS_ACTIVE_KEY, True)
    content = quiet_message(settings, config.QUIET_HOURS_MESSAGE_PREFIX)
    if content.has_content:
        await richtext.send_content(bot, chat_id, content, html_fallback=True)
    logger.info("Тихое время: чат %s закрыт.", chat_id)


async def _open_chat(
    bot: Bot,
    db: Database,
    chat_id: int,
    settings: dict[str, Any],
) -> None:
    """Открыть чат и (если задано) прислать сообщение об открытии."""
    if _protection_active(settings, chat_id):
        logger.info(
            "Тихое время: чат %s под защитой антирейда — не открываю.", chat_id
        )
        return
    opened = await antiraid_service.open_chat(bot, chat_id)
    if not opened:
        logger.warning("Тихое время: не удалось открыть чат %s.", chat_id)
        return
    await queries.update_chat_setting(db, chat_id, config.QUIET_HOURS_ACTIVE_KEY, False)
    content = quiet_message(settings, config.QUIET_HOURS_OPEN_MESSAGE_PREFIX)
    if content.has_content:
        await richtext.send_content(bot, chat_id, content, html_fallback=True)
    logger.info("Тихое время: чат %s открыт.", chat_id)


async def apply_for_chat(db: Database, bot: Bot, chat_id: int) -> None:
    """Привести один чат к нужному состоянию по «тихому времени».

    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    """
    settings = await queries.get_chat_settings(db, chat_id)
    enabled = bool(settings.get(config.QUIET_HOURS_ENABLED_KEY))
    active = bool(settings.get(config.QUIET_HOURS_ACTIVE_KEY))

    if not enabled:
        if active:
            await _open_chat(bot, db, chat_id, settings)
        return

    inside = is_within_window(
        settings.get(config.QUIET_HOURS_START_KEY),
        settings.get(config.QUIET_HOURS_END_KEY),
        settings.get(config.QUIET_HOURS_DAYS_KEY),
    )
    if inside and not active:
        await _close_chat(bot, db, chat_id, settings)
    elif not inside and active:
        await _open_chat(bot, db, chat_id, settings)


async def run_quiet_hours_cycle(db: Database, bot: Bot) -> int:
    """Обойти все чаты с включённым «тихим временем» и применить режим.

    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    :returns: сколько чатов обработано.
    """
    try:
        chat_ids = await queries.get_chats_with_quiet_hours(db)
    except Exception as exc:
        error_handler.log_exception("поиске чатов с тихим временем", exc)
        return 0
    for chat_id in chat_ids:
        try:
            await apply_for_chat(db, bot, chat_id)
        except Exception as exc:
            error_handler.log_exception(
                f"применении тихого времени в чате {chat_id}", exc
            )
    return len(chat_ids)
