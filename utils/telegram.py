"""Небольшие помощники для работы с Telegram API.

Зачем модуль: поля объекта чата зависят от версии aiogram (например,
``Chat.members_count`` в aiogram 3.x отсутствует, хотя Telegram его отдаёт),
а обращаться к таким полям напрямую — значит ронять обработчики.
Здесь собраны безопасные обёртки, которые ничего не ломают.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Final, Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup, Message

import config
from utils import error_handler, html_utils

logger = logging.getLogger(__name__)

#: Признак того, что Telegram нечего править: текст и клавиатура те же.
_NOT_MODIFIED_MARKERS: Final[tuple[str, ...]] = ("message is not modified",)

#: Ошибки, при которых повтор реально поможет:
#: 429 → ждём столько, сколько просит Telegram; 5xx/сеть → растущая пауза.
_RETRYABLE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    TelegramRetryAfter,
    TelegramServerError,
    TelegramNetworkError,
)


def _is_not_modified(exc: BaseException) -> bool:
    """Telegram сообщает, что экран уже такой же (править нечего)?

    :param exc: исключение Telegram API.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _NOT_MODIFIED_MARKERS)


def _is_not_found(exc: BaseException) -> bool:
    """Сообщение уже удалено / не найдено — это норма, не ошибка.

    :param exc: исключение Telegram API.
    """
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("message to delete not found", "message can't be deleted")
    )


# ---------------------------------------------------------------------------
# Надёжная отправка: повторы при флуд-контроле и сетевых сбоях
# ---------------------------------------------------------------------------
async def call_with_retry(
    factory: Callable[[], Awaitable[Any]],
    *,
    context: str,
    attempts: Optional[int] = None,
) -> Any:
    """Выполнить Telegram-вызов, повторяя при флуд-контроле и сбоях сети.

    Поведение по типам ошибок:
        * ``TelegramRetryAfter`` (429) — ждём ровно столько секунд, сколько
          просит Telegram (плюс 0.5 с запаса) и повторяем;
        * ``TelegramServerError`` / ``TelegramNetworkError`` — экспоненциальная
          пауза с лёгким джиттером;
        * остальные ошибки Telegram API — не повторяем (повтор не поможет),
          сразу логируем человеческим языком через
          :func:`utils.error_handler.log_telegram_error`.

    :param factory: корутина-«фабрика» вызова (вызывается заново на каждую попытку).
    :param context: описание вызова для логов (например, «приветствие в чате -100…»).
    :param attempts: максимум попыток (по умолчанию из конфига).
    :returns: результат вызова.
    :raises TelegramAPIError: если все попытки исчерпаны или ошибка неповторяемая.
    """
    max_attempts = max(1, int(attempts or config.TELEGRAM_RETRY_ATTEMPTS))
    last_exc: Optional[BaseException] = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await factory()
        except _RETRYABLE_ERRORS as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            if isinstance(exc, TelegramRetryAfter):
                # Telegram сам сказал, сколько ждать.
                delay = float(exc.retry_after) + 0.5
                error_handler.log_telegram_error(
                    f"{context} (попытка {attempt}/{max_attempts})",
                    exc,
                    level=logging.WARNING,
                )
            else:
                delay = min(
                    config.TELEGRAM_RETRY_MAX_DELAY,
                    config.TELEGRAM_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                )
                delay += random.uniform(0.0, 0.3)
                logger.warning(
                    "Сбой Telegram/сети в %s (попытка %s/%s): %s — повтор через %.1f с",
                    context,
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
            await asyncio.sleep(delay)
        except TelegramAPIError as exc:
            # Неповторяемая ошибка (права, разметка) — логируем сразу и падаем.
            error_handler.log_telegram_error(context, exc, level=logging.WARNING)
            raise

    if last_exc is not None:
        error_handler.log_telegram_error(
            f"{context} (все {max_attempts} попыток исчерпано)", last_exc
        )
        raise last_exc
    return None


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    context: str = "отправка сообщения",
    **kwargs: Any,
) -> bool:
    """Отправить сообщение, никогда не бросая исключений.

    :param bot: экземпляр бота.
    :param chat_id: чат или личка получателя.
    :param text: текст сообщения.
    :param context: описание вызова для логов.
    :param kwargs: остальные аргументы ``send_message``.
    :returns: ``True``, если сообщение доставлено.
    """
    try:
        await call_with_retry(
            lambda: bot.send_message(chat_id=chat_id, text=text, **kwargs),
            context=f"{context} → {chat_id}",
        )
        return True
    except Exception as exc:  # noqa: BLE001 - обёртка никогда не бросает
        error_handler.log_telegram_error(f"{context} → {chat_id}", exc)
        return False


async def safe_delete_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    *,
    context: str = "удаление служебного сообщения",
) -> bool:
    """Удалить одно сообщение с повторами, никогда не бросая исключений.

    «Уже удалено» считается успехом: Telegram отдаёт BadRequest с этим текстом.

    :param bot: экземпляр бота.
    :param chat_id: чат.
    :param message_id: идентификатор сообщения.
    :param context: описание вызова для логов.
    :returns: ``True``, если сообщения больше нет в чате.
    """
    try:
        await call_with_retry(
            lambda: bot.delete_message(chat_id=chat_id, message_id=message_id),
            context=f"{context} → {chat_id}#{message_id}",
            attempts=3,
        )
        return True
    except TelegramBadRequest as exc:
        if _is_not_found(exc):
            return True  # уже удалено — цель достигнута
        logger.debug("Не удалось удалить сообщение %s в %s: %s", message_id, chat_id, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - обёртка никогда не бросает
        error_handler.log_telegram_error(
            f"{context} → {chat_id}#{message_id}", exc, level=logging.WARNING
        )
        return False


async def safe_delete_messages(
    bot: Bot,
    chat_id: int,
    message_ids: list[int],
    *,
    context: str = "батч-удаление служебных сообщений",
) -> int:
    """Удалить сообщения одним батчем ``deleteMessages`` (до 100 за вызов).

    Один запрос вместо сотни — именно это спасает при наплыве входов.

    :param bot: экземпляр бота.
    :param chat_id: чат.
    :param message_ids: идентификаторы сообщений (обрезается до лимита).
    :param context: описание вызова для логов.
    :returns: сколько сообщений удалено (по лучшим данным Telegram).
    """
    if not message_ids:
        return 0
    batch = [int(mid) for mid in message_ids][: config.SERVICE_DELETE_BATCH]
    try:
        await call_with_retry(
            lambda: bot.delete_messages(chat_id=chat_id, message_ids=batch),
            context=f"{context} → {chat_id} ({len(batch)} шт.)",
            attempts=3,
        )
        return len(batch)
    except TelegramBadRequest as exc:
        if _is_not_found(exc):
            return len(batch)
        logger.debug("Батч-удаление в %s не выполнено: %s", chat_id, exc)
        return 0
    except Exception as exc:  # noqa: BLE001 - обёртка никогда не бросает
        error_handler.log_telegram_error(
            f"{context} → {chat_id} ({len(batch)} шт.)", exc, level=logging.WARNING
        )
        return 0


async def safe_send_photo(
    bot: Bot,
    chat_id: int,
    photo: Any,
    *,
    context: str = "отправка фото",
    **kwargs: Any,
) -> bool:
    """Отправить фото, никогда не бросая исключений.

    :param bot: экземпляр бота.
    :param chat_id: чат или личка получателя.
    :param photo: ``file_id``, URL или ссылка.
    :param context: описание вызова для логов.
    :param kwargs: остальные аргументы ``send_photo``.
    :returns: ``True``, если сообщение доставлено.
    """
    try:
        await call_with_retry(
            lambda: bot.send_photo(chat_id=chat_id, photo=photo, **kwargs),
            context=f"{context} → {chat_id}",
        )
        return True
    except Exception as exc:  # noqa: BLE001 - обёртка никогда не бросает
        error_handler.log_telegram_error(f"{context} → {chat_id}", exc)
        return False


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
