"""Обработчики обновлений Telegram.

Модули пакета:
    * :mod:`yamochan.handlers.admin_panel` — админ-панель владельца бота
      (``/adm``): чаты, пользователи, статистика, баны, журнал, рассылка;
    * :mod:`yamochan.handlers.complaints` — жалобы пользователей из главного
      меню (категория → описание → фото → подтверждение);
    * :mod:`yamochan.handlers.antiraid` — кнопки антирейда и ввод порогов;
    * :mod:`yamochan.handlers.roleplay` — ролевые и 18+ команды;
    * :mod:`yamochan.handlers.call` — Call режим и отложенные вызовы;
    * :mod:`yamochan.handlers.start` — личные сообщения (/start, профиль);
    * :mod:`yamochan.handlers.moderation` — команды модератора с гибким
      префиксом (``.``, ``/``, ``!`` или без него);
    * :mod:`yamochan.handlers.diagnostics` — команда ``.диагностика``:
      права бота в чате и проверка отправки;
    * :mod:`yamochan.handlers.events` — пакет событий чата (вход/выход
      участников, антирейд, антиспам, служебные сообщения);
    * :mod:`yamochan.handlers.rules` — правила чата, приветствие новичков,
      инлайн-кнопки приветствия и превентивные муты помеченным;
    * :mod:`yamochan.handlers.faq` — гайд FAQ по разделам бота;
    * :mod:`yamochan.handlers.callbacks` — все инлайн-кнопки.
"""

from __future__ import annotations

__all__ = [
    "admin_panel",
    "antiraid",
    "call",
    "callbacks",
    "complaints",
    "diagnostics",
    "events",
    "faq",
    "moderation",
    "roleplay",
    "rules",
    "start",
]
