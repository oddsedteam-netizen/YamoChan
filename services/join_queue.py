"""Фоновая очередь обработки входов участников.

Зачем модуль: при большом наплыве участников обработка входов прямо в
обработчике апдейта упирается во флуд-контрол Telegram (429) и блокировки
SQLite — приветствие «молча» не доходит, а обновления копятся.

Решение: обработчик только ставит задачу в очередь и сразу возвращается,
а воркер чата обрабатывает входы строго последовательно с паузой
:data:`config.JOIN_MIN_INTERVAL_MS` между ними. Так бот:

    * ничего не теряет (очередь ограничена и всё видно в логах);
    * не ловит 429 на каждой отправке;
    * не блокирует приём апдейтов.

Исключения каждой задачи логируются, воркер не умирает.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable, Final

import config
from utils import error_handler

logger = logging.getLogger(__name__)

#: Фабрика задачи: создаёт корутину в момент реальной обработки.
#: Ленивая форма нужна, чтобы параметры (settings, member) не копились
#: в памяти раньше времени и не создавались «мёртвые» корутины.
JobFactory = Callable[[], Awaitable[None]]


class JoinQueue:
    """Очередь входов: одна последовательная очередь на каждый чат."""

    def __init__(self) -> None:
        """Инициализировать пустые очереди и активные воркеры."""
        self._queues: dict[int, deque[JobFactory]] = {}
        self._workers: dict[int, asyncio.Task[Any]] = {}
        self._total = 0
        #: Счётчики для статистики.
        self.enqueued = 0
        self.processed = 0
        self.rejected = 0

    def enqueue(self, chat_id: int, factory: JobFactory) -> bool:
        """Поставить обработку входа в очередь чата.

        :param chat_id: идентификатор чата.
        :param factory: фабрика корутины обработки.
        :returns: ``False``, если очередь переполнена (событие залогировано).
        """
        queue = self._queues.setdefault(chat_id, deque())
        self._total += 1

        if (
            len(queue) >= config.JOIN_QUEUE_MAX_PER_CHAT
            or self._total > config.JOIN_QUEUE_MAX_TOTAL
        ):
            # Переполнение — событие критичное, логируем ERROR: без этого
            # «потерянные» входы были бы невидимы. Отклоняем именно новую
            # задачу, накопленное обрабатываем.
            self._total -= 1
            self.rejected += 1
            logger.error(
                "Очередь входов переполнена (чат %s: %s, всего %s) — "
                "новый вход не обработан. Причина: обработка медленнее "
                "притока, смотри флуд-контрол в логах.",
                chat_id,
                len(queue),
                self._total,
            )
            return False

        queue.append(factory)
        self.enqueued += 1
        self._ensure_worker(chat_id)
        return True

    def _ensure_worker(self, chat_id: int) -> None:
        """Запустить воркера чата, если он ещё не работает."""
        worker = self._workers.get(chat_id)
        if worker is not None and not worker.done():
            return
        self._workers[chat_id] = error_handler.spawn(
            self._run_chat(chat_id), name=f"join_queue:{chat_id}"
        )

    async def _run_chat(self, chat_id: int) -> None:
        """Обрабатывать входы одного чата строго последовательно."""
        interval = max(0, config.JOIN_MIN_INTERVAL_MS) / 1000.0
        try:
            while True:
                queue = self._queues.get(chat_id)
                if not queue:
                    break
                factory = queue.popleft()
                self._total -= 1
                try:
                    await factory()
                    self.processed += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - воркер не должен умирать
                    error_handler.log_exception(
                        f"обработке входа в чате {chat_id}", exc
                    )
                if queue and interval:
                    # Пауза только между задачами: всплеск входов не должен
                    # выстреливать в лог 429-ами.
                    await asyncio.sleep(interval)
        finally:
            # Воркер завершён — убираем себя из реестра.
            current = self._workers.get(chat_id)
            if current is not None and current is asyncio.current_task():
                self._workers.pop(chat_id, None)
            if not self._queues.get(chat_id):
                self._queues.pop(chat_id, None)

    async def drain(self, timeout: float = 5.0) -> None:
        """Дождаться пустых очередей (корректное выключение).

        :param timeout: сколько секунд максимум ждать.
        """
        try:
            await asyncio.wait_for(self._wait_empty(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Очередь входов не опустела за %.1f с (осталось %s) — выключаемся.",
                timeout,
                self._total,
            )

    async def _wait_empty(self) -> None:
        """Ждать, пока все очереди не станут пустыми."""
        while self._total > 0:
            await asyncio.sleep(0.1)

    def stop(self) -> None:
        """Отменить все воркеры (graceful shutdown)."""
        for task in list(self._workers.values()):
            task.cancel()
        self._workers.clear()
        self._queues.clear()
        self._total = 0

    @property
    def pending(self) -> int:
        """Сколько задач ждёт обработки суммарно."""
        return self._total


#: Глобальная очередь входов проекта.
join_queue: Final[JoinQueue] = JoinQueue()