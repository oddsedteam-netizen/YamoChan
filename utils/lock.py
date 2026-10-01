"""Защита от запуска двух экземпляров бота на одном токене.

Telegram отдаёт апдейты (в том числе «новый участник вошёл в чат») ровно
одному процессу с данным токеном. Если запущено два бота, они дерутся за
``getUpdates``: Telegram отвечает второму ``Conflict``, и события о входах
теряются — бот «молчит», приветствие не приходит, а в логах только
``TelegramConflictError`` от Telegram.

Файл-лок решает это локально (Windows и Linux), а понятная проверка старта
даёт внятное сообщение вместо непонятного конфликта.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final, Optional

logger = logging.getLogger(__name__)

#: Имя файла-лока в папке с данными.
LOCK_FILENAME: Final[str] = "bot.lock"

#: Сообщение при попытке второго запуска.
ALREADY_RUNNING_MESSAGE: Final[str] = (
    "Бот уже запущен другим процессом с этим же токеном. "
    "Второй экземпляр остановлен: иначе Telegram будет отдавать события "
    "двум ботам сразу, и часть событий (в том числе приветствия новых "
    "участников) потеряется. Закрой другое окно или процесс с ботом и запусти снова."
)


class AlreadyRunningError(RuntimeError):
    """Бот с этим токеном уже работает в другом процессе."""


def _pid_alive(pid: int) -> bool:
    """Проверить, жив ли процесс с таким PID.

    :param pid: идентификатор процесса.
    :returns: ``True``, если процесс существует.
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # Windows
        import ctypes

        # PROCESS_QUERY_LIMITED_INFORMATION достаточно для проверки的存在.
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == still_active
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class InstanceLock:
    """Файл-лок: не даёт запустить второго бота с тем же токеном.

    Используется как контекстный менеджер::

        with InstanceLock(lock_path):
            ... работа бота ...

    Если лок занят живым процессом — :class:`AlreadyRunningError`.
    Если в файле лежит мёртвый PID (бот упал) — лок переиспользуется.
    """

    def __init__(self, path: Path) -> None:
        """Запомнить путь к файлу-локу.

        :param path: путь к файлу-локу (обычно рядом с базой данных).
        """
        self._path: Path = Path(path)
        self._acquired: bool = False

    @property
    def path(self) -> Path:
        """Путь к файлу-локу."""
        return self._path

    def acquire(self) -> None:
        """Занять лок или поднять :class:`AlreadyRunningError`.

        Создание файла атомарно (``O_CREAT | O_EXCL``): два бота, стартующих
        в одну секунду, не могут оба пройти проверку. Раньше было «прочитал →
        записал», и при одновременном старте оба считали лок свободным — то
        есть появлялись два бота с одним токеном.

        :raises AlreadyRunningError: если лок уже держит живой процесс.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Попытка 1: создать файл атомарно. Успех — лок наш.
        try:
            self._write_pid(exclusive=True)
            self._acquired = True
            return
        except FileExistsError:
            pass

        # Попытка 2: файл уже есть. Если его держит живой чужой процесс —
        # это второй бот, запускаться нельзя.
        owner = self._read_owner()
        if owner is not None and owner != os.getpid() and _pid_alive(owner):
            raise AlreadyRunningError(
                f"{ALREADY_RUNNING_MESSAGE} (процесс {owner}, файл {self._path.name})"
            )
        # Мёртвый PID (бот упал) или наш собственный — лок можно переиспользовать.
        self._write_pid(exclusive=False)
        self._acquired = True

    def _write_pid(self, *, exclusive: bool) -> None:
        """Записать свой PID в файл лока.

        :param exclusive: ``True`` — создать файл, упасть если он уже есть.
        """
        flags = os.O_WRONLY | os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        else:
            flags |= os.O_TRUNC
        descriptor = os.open(self._path, flags)
        try:
            os.write(descriptor, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(descriptor)

    def release(self) -> None:
        """Отпустить лок и удалить файл (только если лок держали мы)."""
        if not self._acquired:
            return
        self._acquired = False
        try:
            self._path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - файл мог быть занят
            logger.debug("Не удалось удалить файл-лок %s.", self._path)

    def _read_owner(self) -> Optional[int]:
        """Прочитать PID из файла-лока (``None``, если файла нет или он пуст)."""
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw.lstrip("-").isdigit():
            return None
        return int(raw)

    def __enter__(self) -> "InstanceLock":
        """Занять лок."""
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Отпустить лок при выходе (в том числе по исключению)."""
        self.release()