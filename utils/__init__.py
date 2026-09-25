"""Утилиты YamoChan.

Модули пакета:
    * :mod:`yamochan.utils.error_handler` — перехват ошибок и логирование;
    * :mod:`yamochan.utils.telegram` — безопасные помощники Telegram API;
    * :mod:`yamochan.utils.command_filter` — гибкое распознавание команд
      (``.``, ``/``, ``!`` или без префикса);
    * :mod:`yamochan.utils.html_utils` — экранирование текста и лечение
      HTML-разметки (``<`` в данных больше не ломает экраны);
    * :mod:`yamochan.utils.faq_texts` — тексты гайда FAQ.
"""

from __future__ import annotations

__all__ = ["command_filter", "error_handler", "faq_texts", "html_utils", "telegram"]
