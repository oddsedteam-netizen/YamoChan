"""Глобальный перехват ошибок, логирование и мягкие ответы пользователю.

Модуль отвечает за устойчивость бота:
    * настраивает ``logging`` (консоль + два файла) в едином формате;
    * :func:`log_telegram_error` разбирает ошибки Telegram API на человеческий
      язык — флуд-контрол (429), права, серверные сбои;
    * :func:`spawn` запускает фоновую задачу так, чтобы её исключение
      гарантированно попало в лог (иначе «Task exception was never
      retrieved» теряется);
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
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import CallbackQuery, ErrorEvent, Message, TelegramObject

import config

logger = logging.getLogger(__name__)


def setup_logging(level: Optional[str] = None) -> None:
    """Настроить логирование для всего проекта.

    Пишем в три места:
        * консоль — уровень из ``LOG_LEVEL`` (но всегда ``WARNING+``);
        * ``logs/yamochan.log`` — ``INFO`` и выше с ротацией;
        * ``logs/yamochan_errors.log`` — только ``ERROR``+ (быстрая диагностика).

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

        config.LOG_DIR.mkdir(parents=True, exist_ok=True)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(log_level)

        file_handler = RotatingFileHandler(
            filename=str(config.LOG_FILE),
            maxBytes=config.LOG_FILE_MAX_BYTES,
            backupCount=config.LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)

        # Отдельный файл только с ошибками: искать по нему быстрее,
        # чем рыться в основном логе.
        error_log_handler = RotatingFileHandler(
            filename=str(config.ERROR_LOG_FILE),
            maxBytes=config.LOG_FILE_MAX_BYTES,
            backupCount=config.LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
        error_log_handler.setFormatter(formatter)
        error_log_handler.setLevel(logging.ERROR)

        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(min(log_level, logging.INFO))
        root.addHandler(console_handler)
        root.addHandler(file_handler)
        root.addHandler(error_log_handler)

        # Приглушаем избыточные логи сторонних библиотек.
        logging.getLogger("aiogram.event").setLevel(logging.WARNING)
        logging.getLogger("aiosqlite").setLevel(logging.WARNING)
        logging.getLogger("aiohttp").setLevel(logging.WARNING)

        logger.info("Логирование настроено (уровень %s).", logging.getLevelName(log_level))
        logger.info("Основной лог: %s", config.LOG_FILE)
        logger.info("Файл ошибок: %s", config.ERROR_LOG_FILE)
    except Exception:  # noqa: BLE001 - логирование не должно ломать запуск
        logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
        logging.getLogger(__name__).error("Не удалось настроить логирование", exc_info=True)


def log_exception(context: str, exc: BaseException) -> None:
    """Залогировать исключение с полной трассировкой.

    :param context: короткое описание места, где произошла ошибка.
    :param exc: пойманное исключение.
    """
    logger.error("Ошибка в %s: %s: %s", context, type(exc).__name__, exc, exc_info=True)


def log_telegram_error(
    context: str,
    exc: BaseException,
    *,
    level: int = logging.ERROR,
) -> None:
    """Разобрать ошибку Telegram API и залогировать её человеческим языком.

    Отдельно выделяется флуд-контрол (``TelegramRetryAfter``): именно он
    чаще всего «молча» убивает отправку приветствий и удаление служебных
    сообщений при большом наплыве.

    :param context: что мы пытались сделать (например, «отправка приветствия»).
    :param exc: пойманное исключение.
    :param level: уровень лога (по умолчанию ``ERROR``).
    """
    method = getattr(exc, "method", None)
    chat_id = getattr(method, "chat_id", None)
    prefix = f"{context} (чат {chat_id})" if chat_id is not None else context

    if isinstance(exc, TelegramRetryAfter):
        # 429: главный виновник «перестал присылать/удалять» при наплыве.
        logger.log(
            level,
            "⚠️ ФЛУД-КОНТРОЛ Telegram в %s: подожди %s сек и повтори. "
            "Частая причина — наплыв входов/сообщений. Ошибка: %s",
            prefix,
            getattr(exc, "retry_after", "?"),
            exc,
        )
        return
    if isinstance(exc, TelegramForbiddenError):
        logger.log(
            level,
            "🚫 Нет доступа в %s: бот не админ, без прав "
            "(can_delete_messages / restrict) или был кикнут. Ошибка: %s",
            prefix,
            exc,
        )
        return
    if isinstance(exc, TelegramBadRequest):
        logger.log(
            level,
            "❌ Telegram отклонил запрос в %s (разметка/права): %s",
            prefix,
            exc,
        )
        return
    if isinstance(exc, (TelegramServerError, TelegramNetworkError)):
        logger.log(
            level,
            "🌐 Сбой Telegram/сети в %s — стоит повторить позже: %s",
            prefix,
            exc,
        )
        return
    if isinstance(exc, TelegramAPIError):
        logger.log(level, "Ошибка Telegram API в %s: %s: %s", prefix, type(exc).__name__, exc)
        return
    logger.log(
        level,
        "Неожиданная ошибка в %s: %s: %s",
        prefix,
        type(exc).__name__,
        exc,
        exc_info=True,
    )


#: Активные фоновые задачи, запущенные через :func:`spawn`.
#: Держим сильные ссылки, пока задача не завершится, — иначе GC может
#: собрать объект до окончания.
_SPAWNED_TASKS: Final[set[asyncio.Task[Any]]] = set()


def spawn(coro: Awaitable[Any], name: str = "background") -> "asyncio.Task[Any]":
    """Запустить фоновую задачу с гарантированным логированием ошибок.

    Обычный ``asyncio.create_task`` при исключении выводит
    «Task exception was never retrieved» в stderr, который в файл лога
    не попадает — и бот «молча» теряет работу. Здесь исключение любого
    фонового процесса всегда уходит в лог.

    :param coro: корутина задачи.
    :param name: понятное имя задачи для логов.
    :returns: объект задачи.
    """

    async def _runner() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - задача не должна падать молча
            log_exception(f"фоновой задаче «{name}»", exc)

    task = asyncio.create_task(_runner(), name=name)
    _SPAWNED_TASKS.add(task)
    task.add_done_callback(_SPAWNED_TASKS.discard)
    return task


def install_loop_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Настроить обработчик необработанных исключений event loop.

    Без этого «exception was never retrieved» и ошибки в callback'ах
    пропадают — бот «работает», а в логах пусто.

    :param loop: работающий цикл событий.
    """

    def _handler(loop_obj: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        if exception is not None:
            log_exception(str(context.get("message", "event loop")), exception)
        else:
            logger.error("Ошибка event loop: %s", context)

    loop.set_exception_handler(_handler)


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