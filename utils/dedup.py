"""Защита от повторной обработки одного и того же события.

Telegram может прислать одно событие двумя каналами: служебным сообщением
(``message.new_chat_members``) и обновлением статуса участника
(``chat_member``). Без дедупликации приветствие ушло бы дважды.

Модуль хранит метки времени в памяти процесса: это осознанно — при перезапуске
бота память сбрасывается, а события, задержавшиеся дольше TTL, уже неактуальны.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Final

logger = logging.getLogger(__name__)

#: Как часто чистить протухшие метки (в секундах).
_CLEANUP_INTERVAL: Final[float] = 60.0


class EventDedup:
    """Реестр «уже обработанных» событий с временным окном (TTL).

    Не асинхронный и не потокобезопасный намеренно: работает только в
    event loop бота, где задачи выполняются последовательно.
    """

    def __init__(
        self,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Создать реестр с окном жизни меток.

        :param ttl: сколько секунд событие считается уже обработанным.
        :param clock: функция времени (монотонные секунды). Внедряется в
            тестах, чтобы проверять TTL без ``sleep`` и без гонки по времени.
        """
        self._ttl: float = float(ttl)
        self._clock: Callable[[], float] = clock
        self._seen: dict[tuple[str, int, int], float] = {}
        self._last_cleanup: float = clock()

    @property
    def ttl(self) -> float:
        """Окно жизни метки в секундах."""
        return self._ttl

    def mark(self, kind: str, chat_id: int, user_id: int) -> bool:
        """Отметить событие. ``True`` — событие новое, ``False`` — дубль.

        :param kind: вид события (``"join"``, ``"leave"`` и т.п.).
        :param chat_id: идентификатор чата.
        :param user_id: идентификатор участника.
        :returns: ``True``, если событие обработано впервые.
        """
        now = self._clock()
        key = (kind, chat_id, user_id)

        previous = self._seen.get(key)
        if previous is not None and now - previous < self._ttl:
            logger.info(
                "Дубль события %s для участника %s в чате %s "
                "(предыдущее %.1f с назад) — пропускаю.",
                kind,
                user_id,
                chat_id,
                now - previous,
            )
            return False

        self._seen[key] = now
        self._cleanup(now)
        return True

    def forget(self, kind: str, chat_id: int, user_id: int) -> None:
        """Забыть событие, чтобы следующее обработалось заново.

        Нужен, когда обработка провалилась и событие нужно повторить.

        :param kind: вид события.
        :param chat_id: идентификатор чата.
        :param user_id: идентификатор участника.
        """
        self._seen.pop((kind, chat_id, user_id), None)

    def size(self) -> int:
        """Сколько меток сейчас в памяти (для диагностики)."""
        return len(self._seen)

    def _cleanup(self, now: float) -> None:
        """Удалить протухшие метки, чтобы память не росла бесконечно."""
        if now - self._last_cleanup < _CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        stale = [key for key, stamp in self._seen.items() if now - stamp >= self._ttl]
        for key in stale:
            del self._seen[key]
        if stale:
            logger.debug("Дедупликация: забыто протухших меток — %s.", len(stale))