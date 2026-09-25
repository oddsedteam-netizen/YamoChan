"""Небольшие помощники для работы с Telegram API.

Зачем модуль: поля объекта чата зависят от версии aiogram (например,
``Chat.members_count`` в aiogram 3.x отсутствует, хотя Telegram его отдаёт),
а обращаться к таким полям напрямую — значит ронять обработчики.
Здесь собраны безопасные обёртки, которые ничего не ломают.
"""

from __future__ import annotations

import logging
from typing import Final, Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.types import InlineKeyboardMarkup, Message

from utils import html_utils

logger = logging.getLogger(__name__)

#: Признак того, что Telegram нечего править: текст и клавиатура те же.
_NOT_MODIFIED_MARKERS: Final[tuple[str, ...]] = ("message is not modified",)


def _is_not_modified(exc: BaseException) -> bool:
    """Telegram сообщает, что экран уже такой же (править нечего)?

    :param exc: исключение Telegram API.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _NOT_MODIFIED_MARKERS)


def resolve_members_count(chat: object) -> Optional[int]:
    """Достать число участников из объекта чата Telegram, если оно есть.

    В aiogram 3.x у :class:`aiogram.types.Chat` нет поля ``members_count``,
    поэтому обращение к нему напрямую бросает ``AttributeError``.

    :param chat: объект чата из апдейта.
    :returns: число участников или ``None``, если поля нет.
    """
    value = getattr(chat, "members_count", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def fetch_members_count(bot: Bot, chat_id: int) -> Optional[int]:
    """Узнать актуальное число участников чата через ``getChatMemberCount``.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    :returns: число участников или ``None``, если запрос не удался.
    """
    try:
        return int(await bot.get_chat_member_count(chat_id=chat_id))
    except TelegramAPIError as exc:
        logger.info("getChatMemberCount(%s) не выполнен: %s", chat_id, exc)
        return None
    except Exception:  # noqa: BLE001 - счётчик участников не критичен
        logger.error("Неожиданная ошибка getChatMemberCount(%s)", chat_id, exc_info=True)
        return None


async def render_screen(
    message: Message,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Показать новый экран в том же сообщении, не падая на плохой разметке.

    Порядок попыток такой:

        1. текст заранее чинится :func:`yamochan.utils.html_utils.safe_html_text`,
           поэтому «сломанный» ``<`` в данных не ломает весь экран;
        2. правим сообщение;
        3. если Telegram всё равно отверг разметку — правим без разметки;
        4. если текст не изменился — обновляем только клавиатуру;
        5. иначе отправляем новое сообщение.

    Функция никогда не бросает исключений Telegram API: экран — это удобство,
    а не повод уронить обработчик.

    :param message: сообщение с экраном (обычно ``callback.message``).
    :param text: текст нового экрана.
    :param markup: клавиатура нового экрана.
    """
    safe_text = html_utils.safe_html_text(text)
    if safe_text != text:
        # Не молчим в логах: так сломанный тег в данных легко найти.
        logger.debug("В экране был неэкранированный «<» — он исправлен автоматически.")
    plain_fallback = False

    try:
        await message.edit_text(safe_text, reply_markup=markup)
        return
    except TelegramForbiddenError as exc:
        logger.info("Нет доступа к редактированию сообщения: %s", exc)
        return
    except TelegramBadRequest as exc:
        if html_utils.is_parse_error(exc):
            logger.warning(
                "Telegram отверг HTML-разметку экрана, повторяю без неё: %s", exc
            )
            plain_fallback = True
        else:
            # Чаще всего это «message is not modified» — хватит клавиатуры.
            logger.debug("edit_text не сработал: %s", exc)
            try:
                await message.edit_reply_markup(reply_markup=markup)
                return
            except TelegramAPIError as inner:
                if _is_not_modified(inner):
                    # Экран уже правильный: дублировать сообщение не нужно.
                    logger.debug("Экран не изменился, обновлять нечего: %s", inner)
                    return
                logger.debug("edit_reply_markup не сработал: %s", inner)
    except TelegramAPIError as exc:
        logger.debug("edit_text не сработал: %s", exc)

    if plain_fallback:
        try:
            await message.edit_text(
                html_utils.plain_text(safe_text), reply_markup=markup
            )
            return
        except TelegramAPIError as exc:
            logger.debug("Правка без разметки не удалась: %s", exc)

    fallback = html_utils.plain_text(safe_text) if plain_fallback else safe_text
    try:
        await message.answer(fallback, reply_markup=markup)
    except TelegramAPIError as exc:
        logger.info("Запасная отправка экрана не удалась: %s", exc)
