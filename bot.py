"""Точка входа YamoChan: запуск бота, фоновые задачи, graceful shutdown.

Запуск::

    python -m yamochan.bot

Модуль делает всё, чтобы бот не падал:
    * подключает базу данных с retry-логикой и создаёт таблицы;
    * прокидывает ``db`` в обработчики через middleware;
    * включает подсчёт сообщений и глобальный перехват ошибок;
    * запускает фоновый воркер авто-снятия истёкших наказаний;
    * аккуратно выключается по SIGINT/SIGTERM и перезапускает поллинг
      при сетевых сбоях.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Final, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, Message, TelegramObject

from . import config
from .database.db import Database
from .database import queries
from .handlers import (
    admin_panel,
    antiraid,
    call,
    callbacks,
    complaints,
    events,
    faq,
    moderation,
    roleplay,
    rules,
    start,
)
from .services import call as call_service
from .services import punishment
from .utils import error_handler

logger = logging.getLogger(__name__)

#: Список команд для меню Telegram.
BOT_COMMANDS: Final[tuple[BotCommand, ...]] = (
    BotCommand(command="start", description="Открыть меню и профиль"),
    BotCommand(command="help", description="Что я умею"),
)


class DatabaseMiddleware(BaseMiddleware):
    """Прокидывает соединение с базой данных во все обработчики."""

    def __init__(self, db: Database) -> None:
        """Сохранить объект базы данных.

        :param db: соединение с базой данных.
        """
        self._db: Database = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Добавить ``db`` в контекст обработчика."""
        data["db"] = self._db
        return await handler(event, data)


#: Короткий ответ забаненному пользователю на нажатие кнопки.
BANNED_ALERT_TEXT: Final[str] = config.ADMIN_BANNED_ALERT


def _is_private_event(event: TelegramObject) -> bool:
    """Пришло ли событие из личных сообщений боту.

    :param event: сообщение или нажатие кнопки.
    """
    if isinstance(event, CallbackQuery):
        message = event.message
        if message is None:
            return True  # кнопки инлайн-режима тоже считаем личкой бота
        return getattr(getattr(message, "chat", None), "type", None) == ChatType.PRIVATE
    return getattr(getattr(event, "chat", None), "type", None) == ChatType.PRIVATE


class AdminBanMiddleware(BaseMiddleware):
    """Не даёт забаненным ботом пользователям пользоваться ботом в личке.

    Забаненный пользователь получает уведомление о блокировке (не чаще, чем
    раз в :data:`config.ADMIN_BAN_NOTICE_COOLDOWN` секунд), а апдейт дальше не
    идёт. Сообщения в группах не блокируются: там пользователь общается с
    чатом, а не с ботом.
    """

    def __init__(self, db: Database) -> None:
        """Сохранить соединение с базой и подготовить кэш уведомлений.

        :param db: соединение с базой данных.
        """
        self._db: Database = db
        self._notified: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Проверить бан пользователя перед обработкой апдейта."""
        user = data.get("event_from_user") or getattr(event, "from_user", None)
        owner_id = int(config.BOT_OWNER_ID or 0)
        if user is None or int(user.id) == owner_id:
            return await handler(event, data)
        if not _is_private_event(event):
            return await handler(event, data)

        try:
            ban = await queries.get_admin_ban(self._db, int(user.id))
        except Exception as exc:  # noqa: BLE001 - проверка не должна ломать бота
            error_handler.log_exception("проверке бана в боте", exc)
            return await handler(event, data)
        if ban is None:
            return await handler(event, data)

        await self._notify(event, int(user.id), ban)
        return None

    async def _notify(self, event: TelegramObject, user_id: int, ban: Any) -> None:
        """Сообщить забаненному пользователю о блокировке.

        :param event: апдейт пользователя.
        :param user_id: идентификатор пользователя.
        :param ban: запись о бане (``AdminBan``).
        """
        now = time.monotonic()
        fresh = now - self._notified.get(user_id, 0.0) >= config.ADMIN_BAN_NOTICE_COOLDOWN
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(BANNED_ALERT_TEXT, show_alert=True)
            elif fresh and isinstance(event, Message):
                await event.answer(
                    config.ADMIN_BANNED_MESSAGE.format(reason=ban.display_reason)
                )
        except Exception:  # noqa: BLE001 - уведомление не важнее блокировки
            logger.debug("Не удалось уведомить забаненного пользователя", exc_info=True)
        self._notified[user_id] = now
        if len(self._notified) > 1000:
            threshold = now - config.ADMIN_BAN_NOTICE_COOLDOWN * 4
            self._notified = {
                key: moment for key, moment in self._notified.items() if moment >= threshold
            }


def build_dispatcher(db: Database) -> Dispatcher:
    """Собрать диспетчер: middleware, роутеры, обработчики ошибок.

    :param db: соединение с базой данных.
    :returns: готовый к запуску диспетчер.
    """
    dispatcher = Dispatcher(storage=MemoryStorage())

    # База данных доступна всем обработчикам.
    dispatcher.update.middleware(DatabaseMiddleware(db))
    # Забаненные ботом пользователи не могут пользоваться личкой.
    dispatcher.message.outer_middleware(AdminBanMiddleware(db))
    dispatcher.callback_query.outer_middleware(AdminBanMiddleware(db))
    # Подсчёт сообщений в группах (требование проекта №8).
    dispatcher.message.outer_middleware(events.MessageCounterMiddleware(db))

    # Порядок важен: первым идёт роутер жалоб — он владеет кнопками
    # ``adm:complaint:…``; сразу за ним — админ-панель владельца: команда /adm
    # должна ловиться раньше общих роутеров, иначе её перехватит модерация или
    # личка, и панель «не откроется». Дальше: антирейд (со своими состояниями)
    # → ролевые команды → Call → правила/приветствие → личка (/start) →
    # модерация → события чата → FAQ → кнопки.
    dispatcher.include_router(complaints.router)
    dispatcher.include_router(admin_panel.router)
    dispatcher.include_router(antiraid.router)
    dispatcher.include_router(roleplay.router)
    dispatcher.include_router(call.router)
    dispatcher.include_router(rules.router)
    dispatcher.include_router(start.router)
    dispatcher.include_router(moderation.router)
    dispatcher.include_router(events.router)
    dispatcher.include_router(faq.router)
    dispatcher.include_router(callbacks.router)

    error_handler.register_error_handlers(dispatcher)
    return dispatcher


async def expiration_worker(db: Database, bot: Bot, stop_event: asyncio.Event) -> None:
    """Фоновая задача: снимать истёкшие баны и муты, чистить журнал.

    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    :param stop_event: событие выключения бота.
    """
    logger.info("Воркер авто-снятия наказаний запущен.")
    cleanup_counter = 0
    while not stop_event.is_set():
        try:
            await punishment.process_expired_punishments(bot, db)
            cleanup_counter += 1
            # Раз в ~30 минут чистим журнал сообщений старше TTL.
            interval_count = max(1, 30 * 60 // max(1, config.EXPIRATION_CHECK_INTERVAL))
            if cleanup_counter >= interval_count:
                cleanup_counter = 0
                keep_days = max(
                    1, (config.MESSAGE_LOG_TTL_HOURS + 23) // 24
                )
                removed = await queries.cleanup_message_log(db, keep_days)
                if removed:
                    logger.info("Журнал сообщений очищен: удалено %d записей.", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - воркер не должен умирать
            logger.error("Ошибка в воркере наказаний", exc_info=True)

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=max(5, config.EXPIRATION_CHECK_INTERVAL)
            )
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            raise


async def watchdog(dp: Dispatcher, stop_event: asyncio.Event) -> None:
    """Остановить поллинг, как только получен сигнал выключения.

    :param dp: диспетчер бота.
    :param stop_event: событие выключения.
    """
    try:
        await stop_event.wait()
        logger.info("Останавливаю получение апдейтов~")
        await dp.stop_polling()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.error("Не удалось корректно остановить поллинг", exc_info=True)


async def set_bot_commands(bot: Bot) -> None:
    """Опубликовать список команд в меню Telegram.

    :param bot: экземпляр бота.
    """
    try:
        await bot.set_my_commands(list(BOT_COMMANDS))
        logger.info("Список команд бота обновлён.")
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("установке списка команд", exc)


async def run_polling(dp: Dispatcher, bot: Bot, stop_event: asyncio.Event) -> None:
    """Запустить long-polling с автоматическим перезапуском при сбоях.

    :param dp: диспетчер бота.
    :param bot: экземпляр бота.
    :param stop_event: событие выключения.
    """
    try:
        allowed_updates = dp.resolve_used_update_types()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("определении типов апдейтов", exc)
        allowed_updates = None
    logger.info("Подписка на апдейты: %s", allowed_updates or "все")

    while not stop_event.is_set():
        try:
            await dp.start_polling(
                bot,
                allowed_updates=allowed_updates,
                handle_signals=False,
            )
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - поллинг перезапускаем сами
            error_handler.log_exception("long-polling", exc)
            await asyncio.sleep(max(1, config.POLLING_RESTART_DELAY))


async def main() -> int:
    """Точка входа: подготовить бота, запустить поллинг, закрыть ресурсы.

    Аптайм для админ-панели считается от момента импорта
    :mod:`yamochan.services.admin` (константа ``STARTED_AT``) — это и есть
    старт процесса бота, отдельного изменяемого состояния проект не ведёт.

    :returns: код возврата процесса — ``0`` при штатном завершении.
    """
    error_handler.setup_logging()
    logger.info(f"🚀 {config.BOT_NAME} v{config.BOT_VERSION} запущена~")

    # Диагностика админ-панели: с пустым BOT_OWNER_ID команда /adm молчит,
    # и внешне это выглядит как «панель не работает».
    if config.BOT_OWNER_ID > 0:
        logger.info("🔧 BOT_OWNER_ID: %s — админ-панель открыта владельцу.", config.BOT_OWNER_ID)
    else:
        logger.warning(
            "⚠️ BOT_OWNER_ID не задан — админ-панель (/adm) работать не будет. "
            "Добавь строку BOT_OWNER_ID=<твой Telegram ID> в файл .env"
        )

    if not config.BOT_TOKEN:
        logger.error(
            "Не задан BOT_TOKEN! Скопируй .env.example в .env и вставь токен от @BotFather."
        )
        return 1

    db = Database(config.DB_PATH)
    bot: Optional[Bot] = None
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []

    try:
        # 1. База данных: соединение и схема (CREATE TABLE IF NOT EXISTS).
        await db.connect()
        await db.init_schema()

        # 2. Бот и диспетчер.
        bot = Bot(
            token=config.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dp = build_dispatcher(db)
        error_handler.install_signal_handlers(asyncio.get_running_loop(), stop_event, bot)

        try:
            me = await bot.get_me()
            logger.info("YamoChan на связи: @%s (ID %s)", me.username, me.id)
        except Exception as exc:  # noqa: BLE001
            error_handler.log_exception("проверке токена", exc)
            logger.error("Похоже, токен неверный~ Проверь значение BOT_TOKEN в .env")
            return 1

        await set_bot_commands(bot)

        # Восстанавливаем отложенные вызовы, сохранённые в настройках чатов.
        await call_service.restore_scheduled(bot, db)

        # 3. Фоновые задачи.
        tasks.append(asyncio.create_task(expiration_worker(db, bot, stop_event)))
        tasks.append(asyncio.create_task(watchdog(dp, stop_event)))

        # 4. Поллинг с автоперезапуском.
        await run_polling(dp, bot, stop_event)
        return 0
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C~ До встречи! 💕")
        return 0
    except Exception as exc:  # noqa: BLE001 - верхний уровень не должен падать
        error_handler.log_exception("работе бота", exc)
        return 1
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:  # noqa: BLE001
                logger.debug("Сессия бота уже закрыта", exc_info=True)
        try:
            await db.close()
        except Exception:  # noqa: BLE001
            logger.error("Не удалось корректно закрыть базу данных", exc_info=True)
        logger.info("YamoChan выключена~ 💤")


def main_sync() -> int:
    """Синхронная обёртка над :func:`main` для запуска из консоли.

    :returns: код возврата процесса.
    """
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main_sync())