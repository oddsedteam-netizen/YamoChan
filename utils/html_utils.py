"""Экранирование текста для сообщений с ``parse_mode=HTML``.

Telegram разбирает символ ``<`` как начало тега, поэтому любые данные извне
(название чата, имя участника, текст владельца, пороги настроек) нужно
экранировать. Иначе сообщение не отправится с ошибкой вида
``can't parse entities: Unsupported start tag ""`` — и пользователь вообще
не увидит экран.

Модуль задаёт единственный канонический способ экранирования — :func:`h`;
его же используют :func:`yamochan.database.models.escape_text` и
:func:`yamochan.services.richtext.escape_html`, поэтому поведение везде
одинаковое.

Инструменты:
    * :func:`h` — экранировать любое значение целиком;
    * :func:`safe_html_text` — вылечить готовый текст, сохранив валидные теги;
    * :func:`plain_text` — убрать разметку, оставив текст безопасным;
    * :func:`is_parse_error` — понять, что это Telegram отверг разметку.
"""

from __future__ import annotations

import re
from html import escape as _html_escape
from typing import Any, Final

#: Теги, которые понимает HTML-разметка Telegram Bot API.
SUPPORTED_TAGS: Final[tuple[str, ...]] = (
    "b",
    "i",
    "u",
    "s",
    "em",
    "strong",
    "code",
    "pre",
    "a",
    "blockquote",
    "tg-emoji",
    "tg-spoiler",
    "span",
)

#: Начало корректного тега: ``<b>``, ``</b>``, ``<a href="…"``.
#: Проверяется только имя тега, дальше идёт ``>``, ``/`` или пробел.
_VALID_TAG_START: Final[re.Pattern[str]] = re.compile(
    r"</?(?:" + "|".join(SUPPORTED_TAGS) + r")(?=[\s/>])",
    re.IGNORECASE,
)

#: Полностью корректный тег вместе с атрибутами — для :func:`plain_text`.
_FULL_TAG: Final[re.Pattern[str]] = re.compile(
    r"</?(?:" + "|".join(SUPPORTED_TAGS) + r")(?:\s[^<>]*)?/?>",
    re.IGNORECASE,
)

#: Признаки того, что Telegram не смог разобрать разметку сообщения.
_PARSE_ERROR_MARKERS: Final[tuple[str, ...]] = (
    "can't parse entities",
    "can't parse entity",
    "unsupported start tag",
    "unsupported end tag",
)


def h(value: Any) -> str:
    """Безопасно экранировать любое значение для HTML-разметки.

    :param value: что угодно (``None`` превращается в пустую строку).
    :returns: текст, который Telegram покажет ровно как есть.
    """
    if value is None:
        return ""
    return _html_escape(str(value), quote=False).replace('"', "&quot;")


def safe_html_text(text: str) -> str:
    """Сделать текст валидным для HTML-разметки, сохранив корректные теги.

    Экранируются только «сломанные» символы ``<`` (например ``< -20`` или
    ``<время>``), а настоящие теги Telegram (``<b>``, ``<code>``,
    ``<a href=…>``, ``<tg-emoji …>``) продолжают работать. Так одна неверная
    подпись не отнимает форматирование у всего экрана.

    :param text: готовый текст экрана.
    :returns: тот же текст, но без незакрытых «начал тегов».
    """
    if not text or "<" not in text:
        return text or ""

    pieces: list[str] = []
    index = 0
    for match in re.finditer("<", text):
        pieces.append(text[index:match.start()])
        if _VALID_TAG_START.match(text, match.start()):
            pieces.append("<")
        else:
            pieces.append("&lt;")
        index = match.start() + 1
    pieces.append(text[index:])
    return "".join(pieces)


def plain_text(text: str) -> str:
    """Убрать разметку, оставив текст безопасным для ``parse_mode=HTML``.

    Удаляются только настоящие теги Telegram, а HTML-сущности (``&lt;``,
    ``&amp;``, ``&quot;``) остаются на месте: Telegram показывает их как
    обычные символы, а «голый» ``<`` снова сломал бы разметку сообщения.

    :param text: текст с возможной HTML-разметкой.
    :returns: текст без тегов, но по-прежнему безопасный.
    """
    return _FULL_TAG.sub("", text or "")


def is_parse_error(exc: BaseException) -> bool:
    """Это ошибка разбора HTML-разметки Telegram?

    :param exc: исключение от Telegram API (обычно ``TelegramBadRequest``).
    """
    message = str(exc).lower()
    return any(marker in message for marker in _PARSE_ERROR_MARKERS)
