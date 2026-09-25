"""Call режим: команды зова, меню владельца и отложенные вызовы.

Команды ``.калл``/``.call``/``/калл``/``/call``/``!калл`` и просто ``калл``
или ``call`` доступны **любому** участнику чата; настройки меняет только
владелец в личке (префикс ``call:``).

Отложенные вызовы живут в настройках чата (``call_scheduled``) и задачах
``asyncio``: после перезапуска бота их восстанавливает
:func:`yamochan.services.call.restore_scheduled`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import config
from ..database import queries
from ..database.db import Database
from ..database.models import ChatInfo, escape_text
from ..keyboards import inline
from ..services import call as call_service
from ..services import permissions, profile as profile_service, richtext, time_parser
from ..utils import command_filter, error_handler, html_utils, telegram

logger = logging.getLogger(__name__)

#: Роутер Call режима.
router: Final[Router] = Router(name="call")

#: Групповые типы чатов, где работают команды вызова.
GROUP_CHAT_TYPES: Final[set[str]] = {ChatType.GROUP, ChatType.SUPERGROUP}

#: С каких символов может начинаться команда вызова (плюс вариант без префикса).
CALL_PREFIXES: Final[tuple[str, ...]] = command_filter.COMMAND_PREFIXES


class CallStates(StatesGroup):
    """Ожидание ввода от владельца чата."""

    waiting_emoji = State()
    waiting_schedule = State()


@dataclass(slots=True)
class ParsedCall:
    """Разобранная команда вызова: текст, который передал автор."""

    text: str


def parse_call(text: Optional[str]) -> Optional[ParsedCall]:
    """Разобрать сообщение в команду вызова.

    Команда работает с префиксом ``.``, ``/``, ``!`` и без него, поэтому
    ``.калл``, ``!call`` и просто ``калл`` — равнозначные варианты.

    :param text: например ``".калл всем подписаться!"``, ``"/call привет"``
        или ``"калл"``.
    :returns: :class:`ParsedCall` или ``None``, если это не вызов.
    """
    command = command_filter.split_command(text)
    if command is None:
        return None
    name, tail = command
    if name not in config.CALL_COMMANDS:
        return None
    return ParsedCall(text=tail)


class CallFilter(BaseFilter):
    """Фильтр: сообщение является командой вызова."""

    async def __call__(self, message: Message) -> Any:
        """Разобрать текст и передать команду в хендлер."""
        parsed = parse_call(message.text)
        if parsed is None:
            return False
        return {"call_command": parsed}


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), CallFilter())
async def handle_call(
    message: Message,
    call_command: ParsedCall,
    db: Database,
    bot: Bot,
) -> None:
    """Позвать всех участников чата (доступно любому участнику).

    :param message: сообщение с командой.
    :param call_command: разобранная команда (прокидывается фильтром).
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    """
    try:
        chat_id = message.chat.id
        settings = await queries.get_chat_settings(db, chat_id)

        author = message.from_user
        author_id: Optional[int] = author.id if author is not None else None
        # Режим выключен — молчим; включён «только для админов» — отказываем.
        allowed, denial = await call_service.is_call_allowed(
            bot, chat_id, author_id, settings, message=message
        )
        if not allowed:
            if denial:
                await message.reply(denial)
            return

        author_name: Optional[str] = author.full_name if author is not None else None
        if author is not None:
            await queries.ensure_user(db, author.id, author.username, author.first_name)

        if settings.get("call_delete_messages"):
            try:
                await message.delete()
            except Exception as exc:  # noqa: BLE001 - прав на удаление может не быть
                logger.info("Не удалось удалить сообщение вызова в чате %s: %s", chat_id, exc)

        sent = await call_service.send_call(
            bot,
            db,
            chat_id,
            text=call_command.text,
            author_id=author_id,
            author_name=author_name,
            settings=settings,
        )
        if sent == 0:
            await message.answer("Не получилось никого позвать~ Проверь мои права 🙏")
    except Exception as exc:  # noqa: BLE001 - вызов не должен ронять бота
        error_handler.log_exception("команде вызова", exc)
        await error_handler.notify_user_softly(message)


# ---------------------------------------------------------------------------
# Экран настроек Call
# ---------------------------------------------------------------------------
async def _render(callback: CallbackQuery, text: str, markup=None) -> None:
    """Показать новый экран вместо текущего сообщения.

    Текст чинится :func:`yamochan.utils.html_utils.safe_html_text`, а показ
    экрана делает :func:`yamochan.utils.telegram.render_screen`.
    """
    message: Optional[Message] = (
        callback.message if isinstance(callback.message, Message) else None
    )
    if message is None:
        if callback.from_user is not None:
            try:
                await callback.bot.send_message(
                    callback.from_user.id,
                    html_utils.safe_html_text(text),
                    reply_markup=markup,
                )
            except Exception as exc:  # noqa: BLE001
                error_handler.log_exception("отправке экрана Call", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:  # noqa: BLE001 - экран не важнее работы бота
        error_handler.log_exception("обновлении экрана Call", exc)


def _chat_id_from(raw: Optional[str]) -> int:
    """Разобрать идентификатор чата из ``callback_data``."""
    cleaned = (raw or "").strip()
    return int(cleaned) if cleaned.lstrip("-").isdigit() else 0


async def _owner_chat(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> Optional[ChatInfo]:
    """Проверить права владельца чата и вернуть чат."""
    user = callback.from_user
    if user is None:
        return None
    chat = await queries.get_chat(db, chat_id)
    if chat is None:
        await callback.answer("Этот чат мне ещё не знаком~ 🌸", show_alert=True)
        return None
    if not await permissions.is_chat_owner_of(bot, db, chat, user.id):
        await callback.answer("Настройки видит только владелец чата~ 🌸", show_alert=True)
        return None
    return chat


async def _show_menu(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
    note: Optional[str] = None,
) -> None:
    """Показать меню Call режима."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await _render(
        callback,
        profile_service.build_call_menu_text(chat, settings),
        inline.call_keyboard(chat_id, settings),
    )
    await callback.answer(note or "")


async def _toggle_setting(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
    field: str,
    title: str,
) -> None:
    """Переключить один флаг Call режима и обновить экран."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    new_value = not bool(settings.get(field))
    settings = await queries.update_chat_setting(db, chat_id, field, new_value)
    logger.info("Call: %s в чате %s переключён в %s.", field, chat_id, new_value)
    await _render(
        callback,
        profile_service.build_call_menu_text(chat, settings),
        inline.call_keyboard(chat_id, settings),
    )
    await callback.answer(f"{title}: {'включено ✅' if new_value else 'выключено ❌'}")


async def _show_scheduled(callback: CallbackQuery, db: Database, bot: Bot, chat_id: int) -> None:
    """Показать список отложенных вызовов."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    entries = [
        dict(item)
        for item in (settings.get("call_scheduled") or [])
        if isinstance(item, dict)
    ]
    await _render(
        callback,
        profile_service.build_call_scheduled_list_text(entries),
        inline.call_scheduled_keyboard(chat_id, entries),
    )
    await callback.answer()


async def _cancel_scheduled(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
    index: int,
) -> None:
    """Отменить отложенный вызов по его номеру."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    cancelled = await call_service.cancel_scheduled(db, chat_id, index)
    await _show_scheduled(callback, db, bot, chat_id)
    await callback.answer(
        "Отложенный вызов отменён ✅" if cancelled else "Такого вызова уже нет~ 🤔",
        show_alert=not cancelled,
    )


async def _ask_emoji(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
) -> None:
    """Попросить владельца прислать новый эмодзи (FSM)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await state.set_state(CallStates.waiting_emoji)
    await state.update_data(chat_id=chat_id)
    await _render(
        callback,
        profile_service.build_call_emoji_prompt_text(settings),
        inline.call_prompt_keyboard(chat_id),
    )
    await callback.answer()


async def _ask_schedule(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
) -> None:
    """Попросить владельца настроить отложенный вызов (FSM)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    await state.set_state(CallStates.waiting_schedule)
    await state.update_data(chat_id=chat_id)
    await _render(
        callback,
        profile_service.build_call_schedule_prompt_text(),
        inline.call_prompt_keyboard(chat_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(inline.CB_CALL))
async def on_call_callbacks(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Обработать все кнопки Call режима."""
    try:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""

        # У действий с индексом (cancel_scheduled) чат — предпоследняя часть.
        index = -1
        if action == "cancel_scheduled" and len(parts) >= 4:
            chat_id = _chat_id_from(parts[-2])
            if parts[-1].lstrip("-").isdigit():
                index = int(parts[-1])
        else:
            chat_id = _chat_id_from(parts[-1])
        logger.info("Кнопка Call %s (чат %s, индекс %s)", callback.data, chat_id, index)

        if action == "menu":
            await _show_menu(callback, db, bot, chat_id)
        elif action == "toggle":
            await _toggle_setting(callback, db, bot, chat_id, "call_enabled", "🔔 Call режим")
        elif action == "admins":
            await _toggle_setting(
                callback, db, bot, chat_id, "call_mention_admins", "👑 Отмечать админов"
            )
        elif action == "emoji_toggle":
            await _toggle_setting(
                callback, db, bot, chat_id, "call_use_emoji", "🎭 Эмодзи вместо имён"
            )
        elif action == "delete":
            await _toggle_setting(
                callback, db, bot, chat_id, "call_delete_messages", "🗑 Удаление сообщения"
            )
        elif action == "admins_only":
            await _toggle_setting(
                callback, db, bot, chat_id, "call_admins_only", "🔒 Call только для админов"
            )
        elif action == "emoji":
            await _ask_emoji(callback, db, bot, state, chat_id)
        elif action == "schedule":
            await _ask_schedule(callback, db, bot, state, chat_id)
        elif action == "list":
            await _show_scheduled(callback, db, bot, chat_id)
        elif action == "cancel_scheduled":
            await _cancel_scheduled(callback, db, bot, chat_id, index)
        else:
            await callback.answer("Не знаю такую кнопку~ 🤔")
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке Call", exc)
        await error_handler.notify_user_softly(callback)


# ---------------------------------------------------------------------------
# Ввод от владельца (FSM)
# ---------------------------------------------------------------------------
async def _owner_context(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> Optional[ChatInfo]:
    """Проверить владельца чата из состояния FSM (или сбросить состояние)."""
    user = message.from_user
    if user is None:
        return None
    if (message.text or "").startswith("/"):
        await state.clear()
        await message.answer("Хорошо, отменила~ Открой меню заново: /start 🌸")
        return None

    data = await state.get_data()
    chat = await queries.get_chat(db, int(data.get("chat_id") or 0))
    if chat is None or not await permissions.is_chat_owner_of(bot, db, chat, user.id):
        await state.clear()
        await message.answer("Настройки видит только владелец чата~ 🌸")
        return None
    return chat


@router.message(CallStates.waiting_emoji, F.chat.type == ChatType.PRIVATE)
async def on_emoji_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Сохранить эмодзи, который прислал владелец."""
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return

        emoji = call_service.normalize_emoji(message.text)
        if emoji is None:
            await message.answer(
                "Не вижу эмодзи~ Пришли один символ 🤔",
                reply_markup=inline.call_prompt_keyboard(chat.chat_id),
            )
            return

        settings = await queries.update_chat_setting(db, chat.chat_id, "call_emoji", emoji)
        await state.clear()
        await message.answer(
            f"✅ Эмодзи для вызовов обновлён: {escape_text(emoji)}",
            reply_markup=inline.call_keyboard(chat.chat_id, settings),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("вводе эмодзи вызова", exc)
        await error_handler.notify_user_softly(message)


@router.message(CallStates.waiting_schedule, F.chat.type == ChatType.PRIVATE)
async def on_schedule_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Создать отложенный вызов из сообщения «время | текст».

    Поддерживается и фото с подписью: текст сохраняется вместе с сущностями
    (премиум-эмодзи, форматирование), а картинка — как ``file_id``.
    """
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return

        parsed = call_service.parse_schedule_input(message.text or message.caption)
        if parsed is None:
            await message.answer(
                "Не поняла формат~ Нужно так: <b>30м | Всем привет!</b>",
                reply_markup=inline.call_prompt_keyboard(chat.chat_id),
            )
            return

        seconds, text, start = parsed
        # Из «30м | текст» вырезаем только текст, сохраняя его форматирование.
        content = richtext.slice_content(richtext.content_from_message(message), start)
        author_id = message.from_user.id if message.from_user else 0
        entry = await call_service.schedule_call(
            bot,
            db,
            chat.chat_id,
            seconds,
            text,
            author_id,
            entities=content.entities,
            photo=content.photo,
        )
        if entry is None:
            await message.answer(
                "Не смогла запланировать~ Проверь время "
                f"(от {time_parser.human_duration(config.CALL_SCHEDULED_MIN_DELAY)}) "
                f"и лимит ({config.CALL_MAX_SCHEDULED} вызовов).",
                reply_markup=inline.call_prompt_keyboard(chat.chat_id),
            )
            return

        settings = await queries.get_chat_settings(db, chat.chat_id)
        await state.clear()
        await message.answer(
            f"✅ Запланировала вызов через {time_parser.human_duration(seconds)}:\n"
            f"«{escape_text(text)}»\n\n"
            "Форматирование и фото сохранила~ 💕",
            reply_markup=inline.call_keyboard(chat.chat_id, settings),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("вводе отложенного вызова", exc)
        await error_handler.notify_user_softly(message)