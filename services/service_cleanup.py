"""Очередь и воркер удаления служебных сообщений (вход/выход и пр.).

Зачем модуль: при наплыве участников Telegram отдаёт десятки служебных
сообщений подряд. Если удалять их по одному запросу, бот упирается во
флуд-контрол (429) — и сообщения остаются в чате, а причина «молчит».

Решение: обработчики только кладут ``message_id`` в очередь, а воркер
удаляет накопленное батчами ``deleteMessages`` (до 100 id за один запрос)
с паузой между батчами и повтором при 429. Статистика каждого цикла
пишется в лог: сколько удалено, сколько осталось, сколько ошибок.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Final, Optional

from aiogram import Bot

import config
from utils import error_handler, telegram

logger = logging.getLogger(__name__)


class ServiceCleanupQueue:
    """Очередь служебных сообщений по чатам + фоновый воркер."""

    def __init__(self) -> None:
        """Инициализировать пустые очереди и счётчики."""
        #: chat_id → очередь message_id (FIFO, с ограничением по размеру).
        self._queues: dict[int, deque[int]] = {}
        self._worker: Optional[asyncio.Task[Any]] = None
        self._stopping = False
        #: Сколько раз подряд батч чата не удалялся (защита от вечного цикла).
        self._attempts: dict[int, int] = {}
        #: Счётчики для статистики.
        self.enqueued = 0
        self.deleted = 0
        self.failed = 0
        self.dropped = 0

    @property
    def pending(self) -> int:
        """Сколько служебных сообщений ждёт удаления."""
        return sum(len(q) for q in self._queues.values())

    def add(self, chat_id: int, message_id: int) -> None:
        """Положить служебное сообщение в очередь на удаление.

        :param chat_id: идентификатор чата.
        :param message_id: идентификатор сообщения.
        """
        if self._stopping:
            return
        if self.pending >= config.SERVICE_CLEANUP_QUEUE_MAX:
            # Защита памяти: сбрасываем самое старое сообщение.
            self.dropped += 1
            logger.warning(
                "Очередь удаления служебных сообщений переполнена (%s) — "
                "самое старое сообщение в чате %s сброшено (сброшено всего: %s).",
                self.pending,
                chat_id,
                self.dropped,
            )
            for queue in self._queues.values():
                if queue:
                    queue.popleft()
                    break
        self._queues.setdefault(chat_id, deque()).append(int(message_id))
        self.enqueued += 1

    def start(self, bot: Bot, stop_event: asyncio.Event) -> None:
        """Запустить фоновый воркер (один на весь бот).

        :param bot: экземпляр бота.
        :param stop_event: событие выключения бота.
        """
        if self._worker is not None and not self._worker.done():
            return
        self._stopping = False
        self._worker = error_handler.spawn(
            self._run(bot, stop_event), name="service_cleanup"
        )

    async def _run(self, bot: Bot, stop_event: asyncio.Event) -> None:
        """Воркер: периодически сливать накопленное батчами."""
        logger.info(
            "Воркер удаления служебных сообщений запущен "
            "(батч %s шт., пауза %.1f с между батчами).",
            config.SERVICE_DELETE_BATCH,
            config.SERVICE_CLEANUP_BATCH_DELAY,
        )
        while not stop_event.is_set():
            try:
                await self._flush_all(bot)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - воркер не должен умирать
                logger.error(
                    "Ошибка в воркере удаления служебных сообщений", exc_info=True
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=config.SERVICE_CLEANUP_INTERVAL
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _flush_all(self, bot: Bot) -> None:
        """Удалить по одному батчу из каждого чата очереди."""
        if not self._queues:
            return
        for chat_id in list(self._queues.keys()):
            queue = self._queues.get(chat_id)
            if not queue:
                self._queues.pop(chat_id, None)
                continue
            batch: list[int] = []
            while queue and len(batch) < config.SERVICE_DELETE_BATCH:
                batch.append(queue.popleft())
            if not batch:
                self._queues.pop(chat_id, None)
                continue

            removed = await telegram.safe_delete_messages(
                bot, chat_id, batch, context="удаление служебных сообщений"
            )
            if removed:
                self.deleted += removed
                self._attempts.pop(chat_id, None)
                logger.info(
                    "Служебных сообщений удалено в чате %s: %s из %s.",
                    chat_id,
                    removed,
                    len(batch),
                )
            else:
                # Не удалилось — возвращаем в начало очереди и повторим:
                # чаще всего причина 429, и повтор пройдёт после паузы.
                self.failed += len(batch)
                attempts = self._attempts.get(chat_id, 0) + 1
                self._attempts[chat_id] = attempts
                if attempts >= 10:
                    # Постоянная отказка (обычно нет прав can_delete_messages) —
                    # бросаем батч, но с ОШИБКОЙ в лог, чтобы её точно увидели.
                    self.dropped += len(batch)
                    self._attempts.pop(chat_id, None)
                    logger.error(
                        "Служебные сообщения в чате %s не удаляются уже %s циклов "
                        "(%s шт.) — прекращаю повторы. Проверь боту право "
                        "can_delete_messages в чате. Последняя ошибка: сообщения "
                        "остаются в чате.",
                        chat_id,
                        attempts,
                        len(batch),
                    )
                    continue
                logger.warning(
                    "Не удалось удалить %s служебных сообщений в чате %s "
                    "(попытка %s/10) — оставляю в очереди для повтора.",
                    len(batch),
                    chat_id,
                    attempts,
                )
                queue.extendleft(reversed(batch))
                break  # этот чат уже отказал — идём к следующему циклу

            if self.pending and config.SERVICE_CLEANUP_BATCH_DELAY:
                await asyncio.sleep(config.SERVICE_CLEANUP_BATCH_DELAY)

        # Чистим пустые чаты.
        for chat_id in [cid for cid, q in self._queues.items() if not q]:
            self._queues.pop(chat_id, None)

    def stats(self) -> dict[str, int]:
        """Статистика для логов.

        :returns: словарь со счётчиками.
        """
        return {
            "pending": self.pending,
            "enqueued": self.enqueued,
            "deleted": self.deleted,
            "failed": self.failed,
            "dropped": self.dropped,
        }

    def stop(self) -> None:
        """Остановить воркер (graceful shutdown)."""
        self._stopping = True
        if self._worker is not None:
            self._worker.cancel()
        self._worker = None

    async def drain(self, bot: Bot, timeout: float = 3.0) -> None:
        """Дождаться, пока очередь не опустеет (корректное выключение).

        Сообщения, которые бот не успел удалить при жизни, должны уйти
        до закрытия сессии — иначе они останутся в чате навсегда.

        :param bot: экземпляр бота.
        :param timeout: сколько секунд максимум ждать.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while self.pending and asyncio.get_event_loop().time() < deadline:
            await self._flush_all(bot)
            if self.pending:
                await asyncio.sleep(config.SERVICE_CLEANUP_BATCH_DELAY)
        if self.pending:
            logger.warning(
                "Выключение: %s служебных сообщений не успели удалиться.",
                self.pending,
            )


#: Глобальная очередь удаления служебных сообщений проекта.
service_cleanup: Final[ServiceCleanupQueue] = ServiceCleanupQueue()