"""Call режим: зов всех участников чата одной командой.

Модуль отвечает за:
    * сбор участников (все, кто не забанен в этом чате);
    * формирование упоминаний (имя или эмодзи) и разбивку по пачкам
      по :data:`config.CALL_BATCH_SIZE` — лимит Telegram на сообщение;
    * отправку вызова с текстом и без;
    * отложенные вызовы: задачи ``asyncio`` + хранение в
      ``settings.call_scheduled``, чтобы переживать перезапуск бота.

Правила приватности (важно и обязательно):
    * бот НИКОГДА не отмечает самого себя, владельца чата и автора вызова;
    * имена администраторов не раскрываются публично: если админы не
      исключаются настройкой, их имена заменяются нейтральной подписью
      «Администрация» (но лучше — исключать их совсем);
    * в текстах бота нет ``@username``: только ``first_name`` внутри ссылки
      ``tg://user?id=…`` или эмодзи-заглушка.

Хендлеры и меню живут в :mod:`yamochan.handlers.call`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final, Iterable, Mapping, Optional, Sequence

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

import config
from database import queries
from database.db import Database
from database.models import escape_text
from services import permissions as permissions_service
from services import profile as profile_service
from services import richtext, time_parser

logger = logging.getLogger(__name__)

#: Активные задачи отложенных вызовов: ключ → задача.
_scheduled_tasks: dict[str, asyncio.Task[Any]] = {}


def _task_key(chat_id: int, moment: float, author_id: int) -> str:
    """Ключ задачи отложенного вызова."""
    return f"{int(chat_id)}:{int(moment)}:{int(author_id)}"


def normalize_emoji(text: Optional[str]) -> Optional[str]:
    """Взять первый символ из сообщения владельца (для замены имён).

    :param text: то, что прислал владелец.
    :returns: один эмодзи/символ или ``None``, если прислали пустоту.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    first = raw[0]
    # Некоторые эмодзи состоят из символа + модификатора (например, ❤️).
    if len(raw) > 1 and raw[1] in ("\ufe0f", "\ufe0e"):
        return raw[:2]
    return first


async def is_call_allowed(
    bot: Bot,
    chat_id: int,
    user_id: Optional[int],
    settings: Mapping[str, Any],
    *,
    message: Optional[Message] = None,
) -> tuple[bool, str]:
    """Разрешён ли вызов: включён ли режим и кому он доступен.

    Проверки по порядку:
        * режим выключен в чате → ``(False, "")``: бот молчит;
        * включён ``call_admins_only`` и автор не админ (в том числе не
          анонимный) → ``(False, причина)``: отвечаем текстом-отказом.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    :param user_id: идентификатор автора вызова (может быть ``None``).
    :param settings: настройки чата.
    :param message: сообщение с командой (для распознавания анонимных админов).
    :returns: пара ``(можно_вызывать, причина_отказа)``.
    """
    if not bool(settings.get("call_enabled")):
        logger.info("Call режим выключен в чате %s — команда проигнорирована.", chat_id)
        return False, ""

    if bool(settings.get(config.CALL_ADMINS_ONLY_KEY)):
        if not await permissions_service.is_admin_or_anon(
            bot, chat_id, user_id, message
        ):
            logger.info("Call только для админов: в чате %s отказано.", chat_id)
            return False, config.CALL_ADMINS_ONLY_MESSAGE

    return True, ""


def parse_schedule_input(text: Optional[str]) -> Optional[tuple[int, str, int]]:
    """Разобрать «30м | Всем привет!» в тройку ``(секунды, текст, позиция)``.

    Позиция — индекс начала текста в исходной строке: она нужна, чтобы
    перенести сущности форматирования (премиум-эмодзи, жирный, ссылки) из
    сообщения владельца в текст отложенного вызова.

    :param text: сообщение владельца (или подпись к фото).
    :returns: ``(секунды, текст, позиция)`` или ``None``, если формат неверный.
    """
    raw = text or ""
    pipe = raw.find("|")
    if pipe == -1:
        return None
    seconds = time_parser.parse_time_token(raw[:pipe].strip())
    if not seconds or seconds < config.CALL_SCHEDULED_MIN_DELAY:
        return None
    if seconds > config.CALL_SCHEDULED_MAX_DELAY:
        return None
    tail = raw[pipe + 1 :]
    leading = len(tail) - len(tail.lstrip())
    body = tail.strip()
    if not body:
        return None
    return seconds, body, pipe + 1 + leading


def scheduled_content(entry: Mapping[str, Any]) -> richtext.RichContent:
    """Содержимое отложенного вызова: текст + сущности + фото.

    :param entry: запись из ``settings.call_scheduled``.
    """
    photo = str(entry.get("photo") or "")
    return richtext.RichContent(
        text=str(entry.get("text") or ""),
        entities=richtext.entities_to_json(entry.get("entities") or []),
        photo=photo or None,
    )


def split_batches(
    items: Sequence[str],
    size: int = config.CALL_BATCH_SIZE,
) -> list[list[str]]:
    """Разбить упоминания на пачки (Telegram не любит больше ~50 в тексте)."""
    step = max(1, int(size))
    return [list(items[index : index + step]) for index in range(0, len(items), step)]


async def admin_ids(bot: Bot, chat_id: int) -> set[int]:
    """Идентификаторы неанонимных админов чата (кэшируются на 5 минут).

    Тонкая обёртка над :func:`services.permissions.get_chat_admin_ids`:
    вся работа с кэшем и приватностью живёт в модуле прав.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    """
    return await permissions_service.get_chat_admin_ids(bot, chat_id)


def build_mention(name: str, user_id: int, settings: dict[str, Any]) -> str:
    """Собрать одно упоминание: имя или эмодзи-заглушка внутри ссылки.

    Юзернейм здесь не используется никогда: в подписи оказывается только
    ``first_name`` (или эмодзи), а ссылка ведёт по ``tg://user?id=…``.

    :param name: имя участника (без ``@``).
    :param user_id: идентификатор участника.
    :param settings: настройки чата (режим эмодзи).
    """
    if settings.get("call_use_emoji"):
        label = str(settings.get("call_emoji") or config.CALL_EMOJI_DEFAULT)
    else:
        label = profile_service.public_name(name, user_id)
    return f'<a href="tg://user?id={int(user_id)}">{escape_text(label)}</a>'


async def collect_mentions(
    bot: Bot,
    db: Database,
    chat_id: int,
    settings: dict[str, Any],
    *,
    excluded: Optional[set[int]] = None,
) -> list[str]:
    """Собрать упоминания участников чата с учётом приватности.

    Из списка гарантированно исчезают: сам бот, владелец чата, автор вызова
    и (в зависимости от настройки ``call_mention_admins``) администраторы.
    Имена берутся только из ``first_name`` — без ``@username``.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param settings: настройки чата.
    :param excluded: готовый набор «не отмечать» (если уже посчитан).
    """
    members = await queries.get_active_chat_members(db, chat_id)
    if not members:
        return []

    if excluded is None:
        excluded = await permissions_service.get_mention_exclusions(
            bot,
            db,
            chat_id,
            include_admins=not settings.get("call_mention_admins", True),
        )

    profiles = await queries.get_users_by_ids(db, [member.user_id for member in members])
    mentions: list[str] = []
    skipped = 0
    for member in members:
        if int(member.user_id) in excluded:
            skipped += 1
            continue
        profile = profiles.get(member.user_id)
        name = profile.first_name if profile is not None else ""
        mentions.append(build_mention(name or "", member.user_id, settings))
    if skipped:
        logger.info(
            "Вызов в чате %s: скрыто упоминаний %s (бот, владелец, админы, автор).",
            chat_id,
            skipped,
        )
    return mentions


async def send_call(
    bot: Bot,
    db: Database,
    chat_id: int,
    *,
    text: str = "",
    author_id: Optional[int] = None,
    author_name: Optional[str] = None,
    settings: Optional[dict[str, Any]] = None,
    content: Optional[richtext.RichContent] = None,
) -> int:
    """Отправить вызов в чат и вернуть число отправленных сообщений.

    Первое сообщение содержит шапку «кто зовёт всех», текст автора и первую
    пачку упоминаний; остальные пачки уходят следующими сообщениями.

    Если передан ``content`` (текст с премиум-эмодзи и/или фото), он уходит
    отдельным сообщением перед вызовом — так форматирование сохраняется
    целиком.

    Приватность: бот не отмечает себя, владельца и автора вызова; админы
    исключаются настройкой, а если она оставляет их в списке — шапка вызова
    для админа/владельца становится нейтральной («Администрация»).

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param text: текст, который передал автор вызова (уже без префикса).
    :param author_id: идентификатор автора (для шапки и логов).
    :param author_name: имя автора для шапки.
    :param settings: настройки чата (если уже прочитаны).
    :param content: текст с форматированием/фото для отдельного сообщения.
    """
    settings = settings or await queries.get_chat_settings(db, chat_id)
    mention_admins = bool(settings.get("call_mention_admins", True))

    # Кого нельзя упоминать: бот, владелец, автор вызова и (по настройке) админы.
    excluded = await permissions_service.get_mention_exclusions(
        bot,
        db,
        chat_id,
        sender_id=author_id,
        include_admins=not mention_admins,
    )
    mentions = await collect_mentions(bot, db, chat_id, settings, excluded=excluded)
    batches = split_batches(mentions) or [[]]

    # Имя автора-админа (или владельца) публично не раскрываем: в шапке
    # появится нейтральная подпись «Администрация».
    admin_ids = await permissions_service.get_chat_admin_ids(bot, chat_id)
    owner_id = await _owner_id_safe(bot, db, chat_id)
    hide_author = bool(author_id) and (
        int(author_id) in admin_ids
        or (owner_id is not None and int(author_id) == int(owner_id))
    )
    header = profile_service.build_call_header_text(author_name, author_id, neutral=hide_author)
    body_text = escape_text(text) if text else ""

    # Текст владельца с форматированием и/или фото уходит отдельным сообщением,
    # чтобы премиум-эмодзи и разметка не терялись в HTML-шапке вызова.
    if content is not None and content.has_content:
        if await richtext.send_content(bot, chat_id, content):
            body_text = ""

    sent = 0
    for index, batch in enumerate(batches):
        parts: list[str] = []
        if index == 0:
            parts.append(header)
            if body_text:
                parts.append(body_text)
        if batch:
            parts.append(" ".join(batch))
        body = "\n\n".join(part for part in parts if part)
        if not body:
            continue
        try:
            await bot.send_message(chat_id=chat_id, text=body, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramAPIError as exc:
            logger.error("Не удалось отправить вызов в чат %s: %s", chat_id, exc)
            break
        except Exception:  # noqa: BLE001
            logger.error("Неожиданная ошибка вызова в чате %s", chat_id, exc_info=True)
            break

    logger.info(
        "Вызов в чате %s: участников %s, сообщений %s (автор %s, скрыто %s).",
        chat_id,
        len(mentions),
        sent,
        author_id,
        len(excluded),
    )
    return sent


async def _owner_id_safe(bot: Bot, db: Database, chat_id: int) -> Optional[int]:
    """Владелец чата без исключений (внутренний помощник вызова)."""
    try:
        return await permissions_service.get_chat_owner_id(bot, db, chat_id)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось получить владельца чата %s", chat_id, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Отложенные вызовы
# ---------------------------------------------------------------------------
async def schedule_call(
    bot: Bot,
    db: Database,
    chat_id: int,
    delay_seconds: int,
    text: str,
    author_id: int,
    *,
    entities: Optional[Iterable[Any]] = None,
    photo: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Создать отложенный вызов: запись в настройках + задача asyncio.

    Текст сохраняется вместе с сущностями (премиум-эмодзи) и ``file_id``
    фото, поэтому отложенный вызов выглядит так же, как его набрал владелец.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param delay_seconds: через сколько секунд отправить вызов.
    :param text: текст вызова.
    :param author_id: автор (владелец чата).
    :param entities: сущности форматирования текста.
    :param photo: ``file_id`` фото, если владелец приложил его.
    :returns: созданная запись или ``None``, если время вне границ или
        достигнут лимит отложенных вызовов.
    """
    if delay_seconds < config.CALL_SCHEDULED_MIN_DELAY:
        return None
    if delay_seconds > config.CALL_SCHEDULED_MAX_DELAY:
        return None

    settings = await queries.get_chat_settings(db, chat_id)
    scheduled = [dict(item) for item in (settings.get("call_scheduled") or [])]
    if len(scheduled) >= config.CALL_MAX_SCHEDULED:
        logger.info("В чате %s достигнут лимит отложенных вызовов.", chat_id)
        return None

    entry: dict[str, Any] = {
        "time": time.time() + int(delay_seconds),
        "text": text,
        "entities": richtext.entities_to_json(entities or []),
        "photo": photo or None,
        "created_by": int(author_id),
    }
    scheduled.append(entry)
    await queries.update_chat_setting(db, chat_id, "call_scheduled", scheduled)
    start_task(bot, db, chat_id, entry)
    return entry


def start_task(bot: Bot, db: Database, chat_id: int, entry: dict[str, Any]) -> None:
    """Запустить задачу отложенного вызова (повторно не создаётся)."""
    moment = float(entry.get("time") or 0)
    author_id = int(entry.get("created_by") or 0)
    key = _task_key(chat_id, moment, author_id)
    existing = _scheduled_tasks.get(key)
    if existing is not None and not existing.done():
        return
    _scheduled_tasks[key] = asyncio.create_task(
        _run_scheduled(bot, db, chat_id, dict(entry), key)
    )


async def _run_scheduled(
    bot: Bot,
    db: Database,
    chat_id: int,
    entry: dict[str, Any],
    key: str,
) -> None:
    """Дождаться времени и отправить отложенный вызов."""
    moment = float(entry.get("time") or 0)
    author_id = int(entry.get("created_by") or 0)
    try:
        delay = max(0.0, moment - time.time())
        if delay:
            await asyncio.sleep(delay)

        settings = await queries.get_chat_settings(db, chat_id)
        if not settings.get("call_enabled"):
            logger.info("Отложенный вызов в чате %s пропущен: Call режим выключен.", chat_id)
        else:
            profile = await queries.get_user(db, author_id) if author_id else None
            name = profile.display_name if profile is not None else f"ID {author_id}"
            await send_call(
                bot,
                db,
                chat_id,
                text=str(entry.get("text") or ""),
                author_id=author_id,
                author_name=name,
                settings=settings,
                content=scheduled_content(entry),
            )
    except asyncio.CancelledError:
        logger.info("Отложенный вызов в чате %s отменён.", chat_id)
        raise
    except Exception:  # noqa: BLE001
        logger.error("Отложенный вызов в чате %s упал", chat_id, exc_info=True)
    finally:
        _scheduled_tasks.pop(key, None)
        await remove_scheduled(db, chat_id, moment, author_id)


async def remove_scheduled(
    db: Database,
    chat_id: int,
    moment: float,
    author_id: int,
) -> bool:
    """Убрать запись отложенного вызова из настроек чата."""
    settings = await queries.get_chat_settings(db, chat_id)
    scheduled = [dict(item) for item in (settings.get("call_scheduled") or [])]
    remaining = [
        item
        for item in scheduled
        if not (
            int(float(item.get("time") or 0)) == int(moment)
            and int(item.get("created_by") or 0) == int(author_id)
        )
    ]
    if len(remaining) == len(scheduled):
        return False
    await queries.update_chat_setting(db, chat_id, "call_scheduled", remaining)
    return True


async def cancel_scheduled(db: Database, chat_id: int, index: int) -> bool:
    """Отменить отложенный вызов по его позиции в списке.

    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param index: позиция записи (как она показана владельцу).
    """
    settings = await queries.get_chat_settings(db, chat_id)
    scheduled = [dict(item) for item in (settings.get("call_scheduled") or [])]
    if index < 0 or index >= len(scheduled):
        return False

    entry = scheduled.pop(index)
    await queries.update_chat_setting(db, chat_id, "call_scheduled", scheduled)
    moment = float(entry.get("time") or 0)
    author_id = int(entry.get("created_by") or 0)
    task = _scheduled_tasks.pop(_task_key(chat_id, moment, author_id), None)
    if task is not None and not task.done():
        task.cancel()
    logger.info("Отложенный вызов в чате %s отменён владельцем.", chat_id)
    return True


async def restore_scheduled(bot: Bot, db: Database) -> int:
    """Восстановить отложенные вызовы после перезапуска бота.

    Просроченные записи удаляются, живые — снова получают задачу.

    :returns: сколько вызовов восстановлено.
    """
    restored = 0
    now = time.time()
    try:
        chat_ids = await queries.get_known_chat_ids(db)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось прочитать чаты для отложенных вызовов", exc_info=True)
        return 0

    for chat_id in chat_ids:
        try:
            settings = await queries.get_chat_settings(db, chat_id)
            scheduled = [dict(item) for item in (settings.get("call_scheduled") or [])]
            if not scheduled:
                continue

            alive: list[dict[str, Any]] = []
            for entry in scheduled:
                if float(entry.get("time") or 0) <= now:
                    logger.info("Просроченный отложенный вызов в чате %s пропущен.", chat_id)
                    continue
                alive.append(entry)
                start_task(bot, db, chat_id, entry)
                restored += 1

            if len(alive) != len(scheduled):
                await queries.update_chat_setting(db, chat_id, "call_scheduled", alive)
        except Exception:  # noqa: BLE001
            logger.error("Ошибка восстановления вызовов чата %s", chat_id, exc_info=True)

    if restored:
        logger.info("Восстановлено отложенных вызовов: %s.", restored)
    return restored