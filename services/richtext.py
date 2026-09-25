"""Форматированный текст бота: премиум-эмодзи (custom emoji) и фото.

Telegram принимает форматирование не только разметкой (``parse_mode``), но и
списком сущностей (``entities``). Поэтому текст владельца сохраняется вместе с
сущностями: так в сообщениях бота живут премиум-эмодзи и жирный/курсив,
которые админ набрал в своём клиенте.

Модуль умеет:
    * сохранять текст владельца вместе с сущностями и фото (:class:`RichContent`);
    * подставлять спец-команды ``{name}``, ``{id}``, ``{mention}``, ``{chat}``
      и ``{count}``, пересчитывая оффсеты сущностей;
    * собирать персональное сообщение с упоминанием новичка (``text_mention``);
    * сворачивать текст в цитату (``expandable_blockquote``) — для правил при входе;
    * отправлять содержимое с фото (как подпись) или без него;
    * аккуратно откатываться к обычному тексту, если Telegram отклонил
      премиум-эмодзи или разметку;
    * проверять и хранить инлайн-кнопки, которые владелец добавил к приветствию.

Настройки чата хранят тройку полей на каждый блок: ``<prefix>_text``,
``<prefix>_entities`` (список сериализованных сущностей) и ``<prefix>_photo``
(``file_id``). Префиксы: ``rules`` (правила) и ``greeting`` (приветствие).
Кнопки приветствия лежат отдельно в ``greeting_buttons`` — список
``{"text": ..., "url": ...}``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    PhotoSize,
    User,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from ..utils import html_utils

logger = logging.getLogger(__name__)

#: Тип сущности премиум-эмодзи в Bot API.
CUSTOM_EMOJI_TYPE: str = "custom_emoji"

#: Тип сущности «упоминание по id» (``tg://user?id=…``).
TEXT_MENTION_TYPE: str = "text_mention"

#: Тип сущности «сворачиваемая цитата» — правила при входе.
EXPANDABLE_BLOCKQUOTE_TYPE: str = "expandable_blockquote"

#: Поля сущности, которые сохраняем в настройках чата.
ENTITY_FIELDS: tuple[str, ...] = (
    "type",
    "offset",
    "length",
    "url",
    "user",
    "language",
    "custom_emoji_id",
)


@dataclass(slots=True)
class RichContent:
    """Текст с форматированием и (необязательным) фото.

    :param text: текст (или подпись к фото).
    :param entities: сериализованные сущности ``MessageEntity``.
    :param photo: ``file_id`` фото, если оно есть.
    """

    text: str = ""
    entities: list[dict[str, Any]] = field(default_factory=list)
    photo: Optional[str] = None

    @property
    def has_text(self) -> bool:
        """Есть ли в содержимом непустой текст."""
        return bool(self.text.strip())

    @property
    def has_photo(self) -> bool:
        """Есть ли в содержимом фото."""
        return bool(self.photo)

    @property
    def has_content(self) -> bool:
        """Есть ли что отправлять вообще."""
        return self.has_text or self.has_photo

    @property
    def has_premium_emoji(self) -> bool:
        """Есть ли среди сущностей премиум-эмодзи."""
        return any(entity.get("type") == CUSTOM_EMOJI_TYPE for entity in self.entities)


def entity_to_dict(entity: Any) -> dict[str, Any]:
    """Сериализовать сущность (``MessageEntity`` или словарь) в словарь.

    Из объекта берутся только нужные Bot API поля; лишние (вроде ``_raw``)
    отбрасываются, чтобы настройки чата не пухли.

    :param entity: сущность из aiogram или уже готовый словарь.
    """
    if isinstance(entity, Mapping):
        return {key: entity[key] for key in ENTITY_FIELDS if key in entity}
    data: dict[str, Any] = {}
    for key in ENTITY_FIELDS:
        value = getattr(entity, key, None)
        if value is None:
            continue
        if key == "user" and value is not None:
            data[key] = {
                "id": getattr(value, "id", None),
                "is_bot": bool(getattr(value, "is_bot", False)),
                "first_name": getattr(value, "first_name", None),
                "username": getattr(value, "username", None),
            }
        else:
            data[key] = value
    return data


def entities_to_json(entities: Iterable[Any]) -> list[dict[str, Any]]:
    """Сериализовать список сущностей для хранения в настройках чата."""
    result: list[dict[str, Any]] = []
    for entity in entities or []:
        data = entity_to_dict(entity)
        if data.get("type") and isinstance(data.get("offset"), int):
            result.append(data)
    return result


def entities_from_json(raw: Optional[Iterable[Any]]) -> list[MessageEntity]:
    """Восстановить сущности из настроек чата.

    Битые записи (например, оставшиеся от старых версий) молча пропускаются:
    сообщение всё равно должно уйти, пусть и без части форматирования.
    """
    result: list[MessageEntity] = []
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        try:
            result.append(MessageEntity(**dict(item)))
        except Exception:  # noqa: BLE001 - форматирование не важнее текста
            logger.debug("Пропущена некорректная сущность: %r", item)
    return result


# ---------------------------------------------------------------------------
# Спец-подстановки: {name}, {id}, {mention}, {chat}, {count}
# ---------------------------------------------------------------------------
def escape_html(text: str) -> str:
    """Экранировать спец-символы HTML в данных пользователя.

    Тонкая обёртка над :func:`yamochan.utils.html_utils.h`, чтобы в проекте
    было ровно одно правило экранирования.

    :param text: произвольный текст (имя участника, название чата и т.п.).
    """
    return html_utils.h(text)


def mention_html(user_id: int, name: str) -> str:
    """HTML-ссылка на пользователя (``tg://user``), без юзернейма.

    :param user_id: идентификатор пользователя.
    :param name: подпись ссылки (экранируется).
    """
    return f'<a href="tg://user?id={int(user_id)}">{escape_html(name)}</a>'


def user_display_name(user: Any) -> str:
    """Имя пользователя для подстановки — всегда без ``@username``."""
    first_name = getattr(user, "first_name", None)
    if first_name:
        return str(first_name).strip()
    full_name = getattr(user, "full_name", None)
    if full_name:
        return str(full_name).strip()
    user_id = int(getattr(user, "id", 0) or 0)
    return f"ID {user_id}" if user_id else "Незнакомец"


def placeholder_values(
    user: Any,
    *,
    chat_title: str = "",
    member_count: Optional[int] = None,
) -> dict[str, str]:
    """Собрать значения спец-команд для подстановки.

    :param user: участник (объект aiogram или что-то с ``id``/``first_name``).
    :param chat_title: название чата для ``{chat}``.
    :param member_count: количество участников для ``{count}``.
    """
    name = user_display_name(user)
    return {
        config.NAME_PLACEHOLDER: name,
        config.ID_PLACEHOLDER: str(int(getattr(user, "id", 0) or 0)),
        config.MENTION_PLACEHOLDER: name,
        config.CHAT_PLACEHOLDER: str(chat_title or ""),
        config.COUNT_PLACEHOLDER: "" if member_count is None else str(int(member_count)),
    }


def placeholder_positions_many(
    source: str,
    placeholders: Sequence[str],
) -> list[tuple[int, str]]:
    """Найти все вхождения спец-команд в порядке появления.

    Если на одной позиции начинаются несколько команд, побеждает самая
    длинная — так подстановка не «режет» незнакомые конструкции.

    :param source: исходный текст владельца.
    :param placeholders: список поддерживаемых команд.
    :returns: пары ``(индекс в тексте, команда)``.
    """
    found: list[tuple[int, str]] = []
    ordered = sorted(placeholders, key=len, reverse=True)
    index = 0
    length = len(source)
    while index < length:
        matched: Optional[str] = None
        for placeholder in ordered:
            if placeholder and source.startswith(placeholder, index):
                matched = placeholder
                break
        if matched is None:
            index += 1
            continue
        found.append((index, matched))
        index += len(matched)
    return found


def _remap_entity_offsets(
    offset: int,
    length: int,
    shifts: Sequence[tuple[int, int, int]],
) -> tuple[int, int]:
    """Пересчитать позицию сущности после нескольких замен.

    :param offset: старый оффсет (в UTF-16-единицах).
    :param length: старая длина (в UTF-16-единицах).
    :param shifts: тройки ``(начало замены, длина команды, длина значения)``.
    """
    new_offset = int(offset)
    new_length = int(length)
    end = int(offset) + int(length)
    for token_start, token_length, replacement_length in shifts:
        delta = replacement_length - token_length
        token_end = token_start + token_length
        if token_end <= int(offset):
            new_offset += delta
            continue
        if token_start >= end:
            break
        if int(offset) <= token_start and end >= token_end:
            new_length += delta  # сущность покрывает замену целиком
        elif int(offset) > token_start and end < token_end:
            # Сущность оказалась внутри спец-команды — прижимаем её к замене.
            new_length = max(1, replacement_length)
    return new_offset, max(1, new_length)


def substitute_placeholders(
    text: str,
    entities: Iterable[dict[str, Any]],
    values: Mapping[str, str],
    *,
    user: Any = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Заменить спец-команды в тексте, не ломая форматирование.

    Оффсеты сущностей пересчитываются в UTF-16-единицах — именно так их
    считает Telegram. Если передан ``user``, каждое ``{mention}`` становится
    упоминанием-ссылкой (``text_mention``): это работает без юзернейма.

    :param text: исходный текст владельца.
    :param entities: сущности исходного текста (словари из настроек).
    :param values: значения спец-команд (см. :func:`placeholder_values`).
    :param user: участник, к которому относится ``{mention}``.
    :returns: ``(новый текст, новые сущности)``.
    """
    source = text or ""
    prepared = [dict(entity) for entity in entities or []]
    if not source:
        return source, prepared

    tokens = placeholder_positions_many(source, tuple(values.keys()))
    if not tokens:
        return source, prepared

    pieces: list[str] = []
    shifts: list[tuple[int, int, int]] = []
    mention_offsets: list[int] = []
    produced = 0
    last_index = 0
    for index, placeholder in tokens:
        chunk = source[last_index:index]
        pieces.append(chunk)
        produced += utf16_length(chunk)

        replacement = str(values.get(placeholder, placeholder))
        if placeholder == config.MENTION_PLACEHOLDER and user is not None and replacement:
            mention_offsets.append(produced)
        pieces.append(replacement)
        produced += utf16_length(replacement)

        shifts.append(
            (
                utf16_length(source[:index]),
                utf16_length(placeholder),
                utf16_length(replacement),
            )
        )
        last_index = index + len(placeholder)
    pieces.append(source[last_index:])
    new_text = "".join(pieces)

    updated: list[dict[str, Any]] = []
    for entity in prepared:
        item = dict(entity)
        item["offset"], item["length"] = _remap_entity_offsets(
            int(item.get("offset") or 0),
            int(item.get("length") or 0),
            shifts,
        )
        updated.append(item)

    if mention_offsets:
        name_length = utf16_length(str(values.get(config.MENTION_PLACEHOLDER) or ""))
        for start in mention_offsets:
            updated.append(mention_entity(start, name_length, user))

    updated.sort(key=lambda item: (int(item.get("offset") or 0), int(item.get("length") or 0)))
    return new_text, updated


# ---------------------------------------------------------------------------
# Сохранение и сборка содержимого
# ---------------------------------------------------------------------------
def largest_photo(photos: Optional[Sequence[PhotoSize]]) -> Optional[str]:
    """``file_id`` самого крупного фото из сообщения."""
    if not photos:
        return None
    best = max(photos, key=lambda item: (item.width or 0) * (item.height or 0))
    return best.file_id


def content_from_message(message: Message) -> RichContent:
    """Собрать содержимое из сообщения владельца (текст, сущности, фото).

    Поддерживаются и обычные сообщения с текстом, и подписи к фото: владелец
    может прислать «текст + фото» одним сообщением, а может только фото или
    только текст.

    :param message: сообщение из лички бота.
    """
    return RichContent(
        text=message.text or message.caption or "",
        entities=entities_to_json(message.entities or message.caption_entities or []),
        photo=largest_photo(message.photo),
    )


def content_from_settings(settings: Mapping[str, Any], prefix: str) -> RichContent:
    """Достать сохранённое содержимое блока из настроек чата.

    :param settings: настройки чата.
    :param prefix: префикс полей (``rules``, ``greeting``).
    """
    photo = settings.get(f"{prefix}_photo")
    raw_entities = settings.get(f"{prefix}_entities")
    entities = (
        [dict(item) for item in raw_entities if isinstance(item, Mapping)]
        if isinstance(raw_entities, (list, tuple))
        else []
    )
    return RichContent(
        text=str(settings.get(f"{prefix}_text") or ""),
        entities=entities,
        photo=str(photo) if photo else None,
    )


def strip_premium_emoji(entities: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убрать премиум-эмодзи из сущностей (для отката к обычному тексту)."""
    return [dict(entity) for entity in entities if entity.get("type") != CUSTOM_EMOJI_TYPE]


def shift_entities(entities: Iterable[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    """Сдвинуть оффсеты сущностей на ``offset`` символов.

    Нужно, когда к тексту владельца спереди добавляется шапка: иначе
    форматирование «уедет» влево.

    :param entities: сущности текста владельца.
    :param offset: на сколько символов текст сдвинулся.
    """
    if not offset:
        return [dict(entity) for entity in entities]
    shifted: list[dict[str, Any]] = []
    for entity in entities:
        item = dict(entity)
        item["offset"] = int(item.get("offset") or 0) + int(offset)
        shifted.append(item)
    return shifted


def utf16_length(text: str) -> int:
    """Длина строки в UTF-16-единицах.

    Именно так Telegram считает оффсеты сущностей: эмодзи вне BMP (например,
    премиум-эмодзи и многие обычные) занимают две единицы, а не одну.
    """
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text or "")


def truncate_utf16(text: str, limit: int) -> str:
    """Обрезать текст до ``limit`` UTF-16-единиц (без разрыва суррогатов)."""
    if utf16_length(text) <= limit:
        return text
    used = 0
    result: list[str] = []
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if used + size > limit:
            break
        result.append(char)
        used += size
    return "".join(result)


def replace_placeholder(
    text: str,
    entities: Iterable[dict[str, Any]],
    placeholder: str,
    replacement: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Заменить одну спец-команду, сохранив форматирование вокруг.

    Оффсеты сущностей пересчитываются в UTF-16-единицах (так их считает
    Telegram), поэтому замена любой длины ничего не «сдвигает».

    :param text: исходный текст (может быть пустым).
    :param entities: сущности исходного текста.
    :param placeholder: что заменяем (например, ``{name}``).
    :param replacement: на что заменяем.
    """
    return substitute_placeholders(text, entities, {placeholder: replacement})


def mention_entity(offset: int, length: int, user: Any) -> dict[str, Any]:
    """Сущность «упоминание по id» (``text_mention``) для участника.

    Юзернейм не нужен: работает и без него, а публичный ник не светится.

    :param offset: позиция имени в тексте.
    :param length: длина имени.
    :param user: объект пользователя aiogram или кортеж ``(id, имя)``.
    """
    user_id = int(getattr(user, "id", 0) or (user[0] if isinstance(user, tuple) else 0))
    first_name = str(
        getattr(user, "first_name", None)
        or (user[1] if isinstance(user, tuple) else "")
        or f"ID {user_id}"
    )
    return {
        "type": TEXT_MENTION_TYPE,
        "offset": int(offset),
        "length": int(length),
        "user": {"id": user_id, "is_bot": False, "first_name": first_name},
    }


def build_personal_content(
    content: RichContent,
    user: User,
    *,
    header: str = "👋 Привет, ",
    footer: str = "",
    chat_title: str = "",
    member_count: Optional[int] = None,
) -> RichContent:
    """Собрать персональное сообщение: шапка с упоминанием + текст владельца.

    Внутри текста подставляются все спец-команды: ``{name}``, ``{id}``,
    ``{mention}``, ``{chat}`` и ``{count}``. Данные берутся от вошедшего
    участника, а не от админа. Сам участник отмечается в шапке через
    ``text_mention`` — это работает без юзернейма.

    :param content: текст и фото владельца.
    :param user: участник, к которому обращаемся.
    :param header: текст шапки перед именем.
    :param footer: текст в самом конце.
    :param chat_title: название чата для ``{chat}``.
    :param member_count: количество участников для ``{count}``.
    """
    name = user_display_name(user)
    values = placeholder_values(user, chat_title=chat_title, member_count=member_count)
    body, body_entities = substitute_placeholders(
        content.text or "",
        content.entities,
        values,
        user=user,
    )
    head = f"{header}{name}~\n\n" if header else ""
    tail = f"\n\n{footer}" if footer else ""
    entities: list[dict[str, Any]] = []
    if head:
        entities.append(mention_entity(utf16_length(header), utf16_length(name), user))
    entities.extend(shift_entities(body_entities, utf16_length(head)))
    entities.sort(key=lambda item: (int(item.get("offset") or 0), int(item.get("length") or 0)))
    return RichContent(text=f"{head}{body}{tail}", entities=entities, photo=content.photo)


def apply_placeholders(
    content: RichContent,
    values: Mapping[str, str],
    *,
    user: Any = None,
) -> RichContent:
    """Подставить спец-команды в содержимое (без шапки и упоминания).

    Используется, когда сообщение уходит не новичку, а в общий чат
    (например, правила по команде) — тогда ``{name}`` подставляется
    данными того, кто запросил текст.

    :param content: текст и фото владельца.
    :param values: значения спец-команд.
    :param user: участник для ``{mention}``.
    """
    text, entities = substitute_placeholders(
        content.text or "",
        content.entities,
        values,
        user=user,
    )
    return RichContent(text=text, entities=entities, photo=content.photo)


def build_quoted_content(content: RichContent) -> RichContent:
    """Обернуть текст в сворачиваемую цитату Telegram.

    Так отправляются правила при входе: ``expandable_blockquote`` прячет
    длинный текст под «развернуть». Подписи к фото цитату не поддерживают,
    поэтому фото остаётся без неё.

    :param content: текст владельца с сущностями (и, возможно, фото).
    """
    if content.has_photo or not content.has_text:
        return content
    entities = [dict(entity) for entity in content.entities]
    entities.append(
        {
            "type": EXPANDABLE_BLOCKQUOTE_TYPE,
            "offset": 0,
            "length": utf16_length(content.text),
        }
    )
    # Внешняя сущность (цитата) идёт первой, вложенное форматирование — за ней.
    entities.sort(key=lambda item: (int(item.get("offset") or 0), -int(item.get("length") or 0)))
    return RichContent(text=content.text, entities=entities, photo=None)


def trim_content(
    content: RichContent,
    limit: Optional[int] = None,
) -> RichContent:
    """Обрезать слишком длинный текст, выбросив сущности за его границей.

    :param content: содержимое владельца.
    :param limit: предел длины (по умолчанию :data:`config.RICH_TEXT_LIMIT`).
    """
    max_length = int(limit or config.RICH_TEXT_LIMIT)
    if utf16_length(content.text) <= max_length:
        return content
    text = truncate_utf16(content.text, max_length)
    entities = [
        dict(item)
        for item in content.entities
        if int(item.get("offset") or 0) < max_length
    ]
    logger.info("Текст обрезан до %s символов (было %s).", max_length, len(content.text))
    return RichContent(text=text, entities=entities, photo=content.photo)


def slice_content(content: RichContent, start: int, end: Optional[int] = None) -> RichContent:
    """Вырезать часть текста, пересчитав оффсеты сущностей.

    Нужно для отложенного вызова: владелец присылает «30м | текст», а боту
    нужен только текст с его форматированием (и фото).

    :param content: исходное содержимое сообщения владельца.
    :param start: индекс начала нужной части (в символах Python).
    :param end: индекс конца (по умолчанию — до конца текста).
    """
    text = content.text or ""
    start_index = max(0, min(int(start), len(text)))
    end_index = len(text) if end is None else max(start_index, min(int(end), len(text)))
    start_u = utf16_length(text[:start_index])
    end_u = utf16_length(text[:end_index])

    entities: list[dict[str, Any]] = []
    for entity in content.entities:
        item = dict(entity)
        offset = int(item.get("offset") or 0)
        length = int(item.get("length") or 0)
        entity_end = offset + length
        if entity_end <= start_u or offset >= end_u:
            continue
        left = max(offset, start_u)
        right = min(entity_end, end_u)
        if right <= left:
            continue
        item["offset"] = left - start_u
        item["length"] = right - left
        entities.append(item)
    return RichContent(
        text=text[start_index:end_index],
        entities=entities,
        photo=content.photo,
    )


def content_preview(content: RichContent, limit: int = 180) -> str:
    """Короткое описание содержимого для меню настроек."""
    parts: list[str] = []
    if content.has_photo:
        parts.append("🖼 фото есть")
    if content.has_premium_emoji:
        parts.append("✨ премиум-эмодзи")
    snippet = " ".join((content.text or "").split())
    if snippet:
        parts.append(snippet[:limit] + ("…" if len(snippet) > limit else ""))
    return " · ".join(parts) if parts else "пока пусто"


# ---------------------------------------------------------------------------
# Инлайн-кнопки приветствия (сохранённое содержимое)
# ---------------------------------------------------------------------------
def normalize_button_text(raw: str) -> Optional[str]:
    """Нормализовать подпись кнопки: одна строка и лимит Telegram.

    :param raw: то, что прислал владелец.
    :returns: подпись или ``None``, если она пустая.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return None
    return truncate_utf16(text, config.MAX_BUTTON_TEXT_LENGTH)


def normalize_button_url(raw: str) -> Optional[str]:
    """Проверить и нормализовать ссылку кнопки.

    Разрешены только ``http://``, ``https://`` и ``tg://``; короткие адреса
    вида ``t.me/…`` получают схему ``https://`` автоматически.

    :param raw: то, что прислал владелец.
    :returns: готовая ссылка или ``None``, если схема недопустима.
    """
    url = str(raw or "").strip()
    if not url or any(char.isspace() for char in url):
        return None
    lowered = url.lower()
    if lowered.startswith(config.BUTTON_URL_SCHEMES):
        return url
    if lowered.startswith(config.BUTTON_URL_SHORT_PREFIXES):
        return f"https://{url}"
    return None


def parse_saved_buttons(raw: Any) -> list[dict[str, str]]:
    """Прочитать сохранённые кнопки из настроек чата.

    Битые записи молча пропускаются: приветствие должно уйти даже с
    частично испорченным списком кнопок.

    :param raw: значение настройки ``greeting_buttons``.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    buttons: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        text = normalize_button_text(str(item.get("text") or ""))
        url = normalize_button_url(str(item.get("url") or ""))
        if text and url:
            buttons.append({"text": text, "url": url})
        if len(buttons) >= config.MAX_GREETING_BUTTONS:
            break
    return buttons


def append_saved_button(
    raw: Any,
    text: str,
    url: str,
) -> list[dict[str, str]]:
    """Добавить кнопку к списку (с учётом лимита) и вернуть новый список."""
    buttons = parse_saved_buttons(raw)
    if len(buttons) >= config.MAX_GREETING_BUTTONS:
        return buttons
    buttons.append({"text": text, "url": url})
    return buttons


def button_rows(count: int) -> tuple[int, ...]:
    """Раскладка кнопок: по :data:`config.BUTTONS_PER_ROW` в ряд.

    Если кнопок нечётное число, последняя занимает отдельный ряд.

    :param count: количество кнопок.
    """
    total = max(0, int(count))
    per_row = max(1, int(config.BUTTONS_PER_ROW))
    rows = [per_row] * (total // per_row)
    if total % per_row:
        rows.append(total % per_row)
    return tuple(rows) or (1,)


def saved_buttons_markup(buttons: Any) -> Optional[InlineKeyboardMarkup]:
    """Собрать клавиатуру из сохранённых кнопок (или ``None``)."""
    items = parse_saved_buttons(buttons)
    if not items:
        return None
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.button(text=item["text"], url=item["url"])
    builder.adjust(*button_rows(len(items)))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------
async def _send_once(
    bot: Bot,
    chat_id: int,
    content: RichContent,
    entities: Optional[list[MessageEntity]],
    parse_mode: Optional[ParseMode],
    reply_markup: Optional[InlineKeyboardMarkup],
    reply_to_message_id: Optional[int],
) -> None:
    """Отправить содержимое один раз с заданными сущностями и режимом."""
    if content.photo:
        await bot.send_photo(
            chat_id=chat_id,
            photo=content.photo,
            caption=content.text or None,
            caption_entities=entities or None,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
        )
        return
    await bot.send_message(
        chat_id=chat_id,
        text=content.text,
        entities=entities or None,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        reply_to_message_id=reply_to_message_id,
        disable_web_page_preview=True,
    )


async def send_content(
    bot: Bot,
    chat_id: int,
    content: RichContent,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    reply_to_message_id: Optional[int] = None,
    html_fallback: bool = True,
) -> bool:
    """Отправить содержимое в чат или в личку.

    Если Telegram не принимает сущности (например, премиум-эмодзи недоступны
    для бота), сообщение уходит без форматирования: премиум-эмодзи в тексте
    автоматически показываются своими обычными эмодзи-заменами, так что текст
    не теряется.

    :param bot: экземпляр бота.
    :param chat_id: чат или личка получателя.
    :param content: текст, сущности и фото.
    :param reply_markup: клавиатура сообщения.
    :param reply_to_message_id: на что отвечаем.
    :param html_fallback: разрешать ли отправку текста с ``parse_mode=HTML``,
        когда сущностей нет. Для персональных текстов (``{name}`` и другие
        спец-команды) лучше ``False``: имя пользователя — это данные, а не
        разметка.
    :returns: ``True``, если сообщение доставлено.
    """
    if not content.has_content:
        return False

    stored = entities_from_json(content.entities)
    entities = stored or None
    attempts: list[tuple[Optional[list[MessageEntity]], Optional[ParseMode]]] = []
    if entities:
        # Сначала как есть (с премиум-эмодзи), затем — без форматирования.
        attempts.append((entities, None))
        attempts.append((None, None))
    elif html_fallback:
        # Текст владельца мог быть набран с HTML-разметкой вручную.
        attempts.append((None, ParseMode.HTML))
        attempts.append((None, None))
    else:
        attempts.append((None, None))

    for index, (entities_attempt, parse_mode) in enumerate(attempts):
        try:
            await _send_once(
                bot,
                chat_id,
                content,
                entities_attempt,
                parse_mode,
                reply_markup,
                reply_to_message_id,
            )
            if index:
                logger.info(
                    "Сообщение в %s ушло без форматирования (fallback №%s).", chat_id, index
                )
            return True
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить содержимое в %s: %s", chat_id, exc)
        except Exception:  # noqa: BLE001 - отправка не должна ронять бота
            logger.error("Неожиданная ошибка отправки в %s", chat_id, exc_info=True)
            return False

    # Последняя попытка: если фото не принято, возможно, «протух» его file_id
    # (бывает редко) — тогда отправляем только текст, чтобы сообщение не потерялось.
    if content.photo:
        logger.warning(
            "Фото «%s» в чат %s не принято — отправляю текст без фото.",
            content.photo,
            chat_id,
        )
        text_only = RichContent(text=content.text, entities=content.entities, photo=None)
        try:
            await _send_once(
                bot,
                chat_id,
                text_only,
                entities,
                None,
                reply_markup,
                reply_to_message_id,
            )
            return True
        except TelegramAPIError as exc:
            logger.error("Текст без фото тоже не ушёл в %s: %s", chat_id, exc)
        except Exception:  # noqa: BLE001
            logger.error("Неожиданная ошибка отправки текста в %s", chat_id, exc_info=True)

    logger.error("Содержимое в %s не отправлено ни в одном варианте.", chat_id)
    return False


async def edit_content(
    message: Message,
    content: RichContent,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """Заменить содержимое сообщения бота (текст с премиум-эмодзи).

    :param message: сообщение бота, которое нужно отредактировать.
    :param content: новое содержимое.
    :param reply_markup: новая клавиатура.
    """
    entities = entities_from_json(content.entities)
    try:
        await message.edit_text(
            content.text or "…",
            entities=entities or None,
            parse_mode=None if entities else ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return True
    except TelegramAPIError as exc:
        logger.warning("Не удалось изменить сообщение %s: %s", message.message_id, exc)
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка редактирования сообщения", exc_info=True)
    if message.caption is not None:
        return False
    try:
        await message.edit_text(content.text or "…", reply_markup=reply_markup)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Повторное редактирование не сработало: %s", exc)
    return False
