"""Сервисы бизнес-логики YamoChan.

Модули пакета:
    * :mod:`yamochan.services.time_parser` — парсер времени («1д2ч30м»);
    * :mod:`yamochan.services.permissions` — проверка прав и владельца чата;
    * :mod:`yamochan.services.punishment` — наказания и их истечение;
    * :mod:`yamochan.services.profile` — карточки профиля, чата и статистика;
    * :mod:`yamochan.services.antiraid` — антирейд и антиспам;
    * :mod:`yamochan.services.call` — Call режим и отложенные вызовы;
    * :mod:`yamochan.services.admin` — тексты и действия админ-панели
      владельца бота (отвязка чата, бан, рассылка, статистика);
    * :mod:`yamochan.services.complaints` — жалобы пользователей и их разбор.
"""

from __future__ import annotations

__all__ = [
    "admin",
    "antiraid",
    "call",
    "complaints",
    "permissions",
    "profile",
    "punishment",
    "time_parser",
]
