"""Глобальный перехват ошибок, логирование и мягкие ответы пользователю.

Модуль отвечает за устойчивость бота:
    * настраивает ``logging`` (консоль + файл) в едином формате;
    * :class:`ErrorMiddleware` ловит любые исключения из обработчиков,
      логирует их с traceback и мягко извиняется перед пользователем;
    * :func:`register_error_handlers` подключает обработчик ошибок уровня
      диспетчера — он ловит всё, что не поймали роутеры;
    * :func:`install_signal_handlers` включает graceful shutdown по
      SIGINT/SIGTERM.

Главное правило проекта: бот не падает ни при каких обстоятельствах.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from logging.handlers import RotatingFileHandler
from typing import Any, Awaitable, Callable, Final, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import CallbackQuery, ErrorEvent, Message, TelegramObject

import config

logger = logging.getLogger(__name__)

#: Максимальный размер файла логов (5 МБ) и число ротаций.
LOG_FILE_MAX_BYTES: Final[int] = 5 * 1024 * 1024
LOG_FILE_BACKUPS: Final[int] = 3


def setup_logging(level: Optional[str] = None) -> None:
    """Настроить логирование для всего проекта.

    Ошибки пишутся с уровнем ``ERROR`` и трассировкой, файл логов
    автоматически ротируется, чтобы не расти бесконечно.

    :param level: уровень логирования (по умолчанию из конфига).
    """
    try:
        log_level = getattr(logging, (level or config.LOG_LEVEL).upper(), None)
        if not isinstance(log_level, int):
            log_level = logging.INFO

        formatter = logging.Formatter(
            fmt=config.LOG_FORMAT,
            datefmt=config.LOG_DATE_FORMAT,
        )

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(log_level)

        file_handler = RotatingFileHandler(
            filename=str(config.BASE_DIR / "yamochan.log"),
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)

        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(log_level)
        root.addHandler(console_handler)
        root.addHandler(file_handler)

        # Приглушаем избыточные логи сторонних библиотек.
        logging.getLogger("aiogram.event").setLevel(logging.WARNING)
        logging.getLogger("aiosqlite").setLevel(logging.WARNING)

        logger.info("Логирование настроено (уровень %s).", logging.getLevelName(log_level))
    except Exception:  # noqa: BLE001 - логирование не должно ломать запуск
        logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
        logging.getLogger(__name__).error("Не удалось настроить логирование", exc_info=True)


def log_exception(context: str, exc: BaseException) -> None:
    """Залогировать исключение с полной трассировкой.

    :param context: короткое описание места, где произошла ошибка.
    :param exc: пойманное исключение.
    """
    logger.error("Ошибка в %s: %s: %s", context, type(exc).__name__, exc, exc_info=True)


async def notify_user_softly(event: TelegramObject, text: str = config.ERROR_MESSAGE) -> None:
    """Мягко сообщить пользователю об ошибке, не ломая обработку апдейта.

    Поддерживаются сообщения и callback-нажатия; любые сбои отправки
    глушатся, потому что пользователь уже увидел ошибку в логах.

    :param event: сообщение или callback-запрос.
    :param text: текст извинения от лица YamoChan.
    """
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
    except Exception:  # noqa: BLE001 - не можем ответить, но бот продолжает работу
        logger.debug("Не удалось сообщить пользователю об ошибке", exc_info=True)


class ErrorMiddleware(BaseMiddleware):
    """Middleware, который не даёт ни одному обработчику уронить бота.

    Оборачивает вызов хендлера в ``try/except``: любое исключение
    логируется с трассировкой, а пользователь получает мягкое извинение
    «Ой, что-то пошло не так~ ».
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Вызвать обработчик с защитой от любых исключений."""
        try:
            return await handler(event, data)
        except asyncio.CancelledError:
            # Отмена задачи при выключении бота — не ошибка.
            raise
        except Exception as exc:  # noqa: BLE001 - намеренно ловим всё
            log_exception(_describe_event(event), exc)
            await notify_user_softly(event)
            return None


def _describe_event(event: TelegramObject) -> str:
    """Коротко описать апдейт для логов.

    :param event: любой объект апдейта Telegram.
    :returns: строка вида ``message#123 в чате -100123``.
    """
    try:
        if isinstance(event, Message):
            chat_id = getattr(event.chat, "id", "?")
            return f"message#{event.message_id} в чате {chat_id}"
        if isinstance(event, CallbackQuery):
            return f"callback {event.data!r}"
        return type(event).__name__
    except Exception:  # noqa: BLE001
        return type(event).__name__


async def handle_errors(event: ErrorEvent) -> bool:
    """Обработчик ошибок уровня диспетчера.

    Вызывается для исключений, которые случились вне middleware
    (например, при разборе апдейта или в фильтрах).

    :param event: событие с информацией об исключении.
    :returns: ``True`` — ошибка обработана, поллинг продолжается.
    """
    log_exception(f"диспетчере ({_describe_event(event.update)})", event.exception)
    await notify_user_softly(event.update)
    return True


def register_error_handlers(dp: Dispatcher) -> None:
    """Подключить middleware и обработчик ошибок к диспетчеру.

    :param dp: диспетчер, к которому привязываем защиту от ошибок.
    """
    try:
        dp.update.middleware(ErrorMiddleware())
        dp.message.middleware(ErrorMiddleware())
        dp.callback_query.middleware(ErrorMiddleware())
        dp.errors.register(handle_errors)
        logger.info("Глобальный перехват ошибок включён.")
    except Exception:  # noqa: BLE001
        logger.error("Не удалось подключить перехват ошибок", exc_info=True)


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
    bot: Optional[Bot] = None,
) -> None:
    """Подписаться на SIGINT/SIGTERM для аккуратного завершения работы.

    На Windows обработка SIGTERM недоступна — ошибки просто логируются,
    а бот продолжает работать через штатный ``KeyboardInterrupt``.

    :param loop: работающий цикл событий.
    :param stop_event: событие, которое сигнализирует о необходимости выхода.
    :param bot: бот, у которого глушится ожидание апдейтов.
    """

    def _shutdown(signum: int) -> None:
        """Отметить необходимость выключения и остановить получение апдейтов."""
        logger.info("Получен сигнал %s — выключаюсь аккуратно~", signum)
        stop_event.set()
        if bot is not None:
            try:
                loop.create_task(bot.session.close())
            except Exception:  # noqa: BLE001
                logger.debug("Не удалось заранее закрыть сессию бота", exc_info=True)

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is None:
            continue
        try:
            loop.add_signal_handler(signal_value, _shutdown, int(signal_value))
        except (NotImplementedError, RuntimeError, ValueError, AttributeError):
            # Windows и некоторые окружения не поддерживают add_signal_handler.
            logger.debug("Сигнал %s недоступен на этой платформе.", signal_name)
            try:
                signal.signal(signal_value, lambda signum, _frame: _shutdown(int(signum)))
            except (ValueError, OSError, AttributeError):
                logger.debug("Не удалось подписаться на %s.", signal_name)