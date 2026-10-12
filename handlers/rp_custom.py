"""Свои (пользовательские) РП-команды владельца чата.

Владелец может добавить в чат собственные РП-команды: название, эмодзи и текст
действия (с пометкой 18+). Они работают так же, как встроенные, но живут в
настройках чата (``custom_rp_commands``) и подхватываются
:data:`yamochan.services.rp_commands.rp_command_store`.

Кнопка «🎭 РП-команды» в меню настроек чата открывает это меню. Префикс
``callback_data`` — ``rpc:``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
from database import queries
from database.db import Database
from database.models import ChatInfo
from handlers import roleplay as roleplay_ui
from keyboards import inline
from services import permissions, profile as profile_service
from services import rp_commands as rp_commands_service
from utils import error_handler, html_utils, telegram

logger = logging.getLogger(__name__)

router: Final[Router] = Router(name="rp_custom")

CANCEL_WORDS: Final[frozenset[str]] = frozenset({"/cancel", "отмена", "cancel"})

NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-zа-яё_\-]{2,32}$")


class RpCustomStates(StatesGroup):
    """Состояния пошагового ввода своей РП-команды."""

    waiting_name = State()
    waiting_emoji = State()
    waiting_action = State()


def reserved_names() -> set[str]:
    """Имена, которые нельзя занять своей РП-командой."""
    names: set[str] = set(config.MODERATION_COMMANDS)
    names.update(config.CALL_COMMANDS)
    names.update(config.ADMIN_COMMANDS)
    names.update(
        {
            config.COMMAND_SYNC,
            config.COMMAND_DIAGNOSTICS,
            config.COMMAND_RULES,
            config.COMMAND_GREETING,
            "rules",
            "greeting",
        }
    )
    names.update(roleplay_ui.RP_COMMANDS.keys())
    return names


def sanitize_name(raw: Any) -> Optional[str]:
    """Проверить и нормализовать имя своей РП-команды.

    :param raw: текст от владельца (можно с префиксом ``.``).
    :returns: чистое имя или ``None``, если оно недопустимо.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    first = text.split()[0]
    cleaned = first.lstrip(".!/")
    if not cleaned or len(cleaned) > config.MAX_CUSTOM_RP_NAME_LENGTH:
        return None
    if not NAME_RE.match(cleaned):
        return None
    return cleaned


async def _render(
    callback: CallbackQuery,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Показать новый экран вместо текущего сообщения."""
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
            except Exception as exc:
                error_handler.log_exception("отправке экрана РП-команд", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:
        error_handler.log_exception("обновлении экрана РП-команд", exc)


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
    text = (message.text or "").strip()
    if text.startswith("/") and text.lower() not in CANCEL_WORDS:
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


def _chat_id_from(raw: Optional[str]) -> int:
    """Разобрать идентификатор чата из ``callback_data``."""
    cleaned = (raw or "").strip()
    return int(cleaned) if cleaned.lstrip("-").isdigit() else 0


def _custom_commands(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Список своих команд чата из настроек."""
    return queries.normalize_custom_rp_commands(
        settings.get(config.CUSTOM_RP_COMMANDS_KEY)
    )


async def _save_commands(
    db: Database,
    chat_id: int,
    commands: list[dict[str, Any]],
) -> None:
    """Сохранить свои команды чата и обновить реестр в памяти."""
    await queries.update_chat_setting(
        db, chat_id, config.CUSTOM_RP_COMMANDS_KEY, commands
    )
    rp_commands_service.rp_command_store.set_all(chat_id, commands)


async def _show_menu(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
    note: Optional[str] = None,
) -> None:
    """Показать меню «Свои РП-команды»."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    commands = _custom_commands(settings)
    await _render(
        callback,
        profile_service.build_rp_custom_menu_text(chat, commands),
        inline.rp_custom_keyboard(chat_id, commands),
    )
    await callback.answer(note or "")


async def _remove(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
    index: int,
) -> None:
    """Удалить одну свою РП-команду по её позиции в списке."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    commands = _custom_commands(settings)
    removed = ""
    if 0 <= index < len(commands):
        removed = str(commands[index].get("name") or "")
        commands = [item for pos, item in enumerate(commands) if pos != index]
    await _save_commands(db, chat_id, commands)
    await _render(
        callback,
        profile_service.build_rp_custom_menu_text(chat, commands),
        inline.rp_custom_remove_keyboard(chat_id, commands),
    )
    if removed:
        await callback.answer(f"Команда .{removed} удалена 🗑")
    else:
        await callback.answer("Такой команды уже нет~ 🌸")


async def _save_new(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
    nsfw: bool,
) -> None:
    """Сохранить новую команду после выбора «обычная/18+»."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    data = await state.get_data()
    emoji = str(data.get("emoji") or "🎭")
    action = str(data.get("action") or "")
    clean_name = str(data.get("name") or "").strip().lower()
    if not action or not clean_name:
        await callback.answer("Что-то потерялось~ Начни заново 🌸", show_alert=True)
        await state.clear()
        return
    settings = await queries.get_chat_settings(db, chat_id)
    commands = [
        item for item in _custom_commands(settings) if item["name"] != clean_name
    ]
    commands.append(
        {"name": clean_name, "emoji": emoji, "action": action, "nsfw": nsfw}
    )
    commands = queries.normalize_custom_rp_commands(commands)
    await _save_commands(db, chat_id, commands)
    await state.clear()
    await _render(
        callback,
        profile_service.build_rp_custom_menu_text(chat, commands),
        inline.rp_custom_keyboard(chat_id, commands),
    )
    await callback.answer(f"Команда .{clean_name} сохранена ✅")


@router.callback_query(F.data.startswith(inline.CB_RPC))
async def on_rpc_callbacks(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Обработать кнопки меню «Свои РП-команды».

    Формат ``callback_data``: ``rpc:{menu|add|remove_menu|remove|clear|
    nsfw}:...:{chat_id}``.
    """
    try:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "nsfw" and len(parts) >= 4:
            await _save_new(
                callback, db, bot, state, _chat_id_from(parts[-1]), parts[2] == "1"
            )
            return

        if action == "remove" and len(parts) >= 4 and parts[2].isdigit():
            await _remove(
                callback, db, bot, _chat_id_from(parts[-1]), int(parts[2])
            )
            return

        chat_id = _chat_id_from(parts[-1])
        chat = await _owner_chat(callback, db, bot, chat_id)
        if chat is None:
            return

        if action == "add":
            await state.clear()
            await state.set_state(RpCustomStates.waiting_name)
            await state.update_data(chat_id=chat_id)
            await _render(
                callback,
                profile_service.build_rp_custom_name_prompt_text(),
                inline.rp_custom_prompt_keyboard(chat_id),
            )
            await callback.answer()
            return

        if action == "remove_menu":
            settings = await queries.get_chat_settings(db, chat_id)
            commands = _custom_commands(settings)
            await _render(
                callback,
                profile_service.build_rp_custom_menu_text(chat, commands),
                inline.rp_custom_remove_keyboard(chat_id, commands),
            )
            await callback.answer("Выбери команду для удаления~ 🗑")
            return

        if action == "clear":
            await _save_commands(db, chat_id, [])
            await _show_menu(
                callback, db, bot, chat_id, "Все свои РП-команды удалены 🧹"
            )
            return

        await _show_menu(callback, db, bot, chat_id)
    except Exception as exc:
        error_handler.log_exception("кнопке РП-команд", exc)
        await error_handler.notify_user_softly(callback)


async def _back_to_menu(message: Message, db: Database, chat: ChatInfo) -> None:
    """Показать меню «Свои РП-команды» новым сообщением в личке."""
    settings = await queries.get_chat_settings(db, chat.chat_id)
    commands = _custom_commands(settings)
    await message.answer(
        profile_service.build_rp_custom_menu_text(chat, commands),
        reply_markup=inline.rp_custom_keyboard(chat.chat_id, commands),
    )


@router.message(RpCustomStates.waiting_name, F.chat.type == ChatType.PRIVATE)
async def on_rpc_name_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять название новой РП-команды."""
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return
        if (message.text or "").strip().lower() in CANCEL_WORDS:
            await state.clear()
            await message.answer("Хорошо, отменила~ 🌸")
            await _back_to_menu(message, db, chat)
            return
        name = sanitize_name(message.text)
        if name is None:
            await message.answer(
                "Не подходит~ Пришли название одним словом "
                f"(до {config.MAX_CUSTOM_RP_NAME_LENGTH} символов: буквы, цифры, _ или -) 🌸"
            )
            return
        if name in reserved_names():
            await message.answer(
                f"Имя «.{name}» уже занято другой командой~ Пришли другое 🌸"
            )
            return
        settings = await queries.get_chat_settings(db, chat.chat_id)
        if len(_custom_commands(settings)) >= config.MAX_CUSTOM_RP_COMMANDS:
            await message.answer(
                f"Уже {config.MAX_CUSTOM_RP_COMMANDS} своих команд — "
                "удали лишнюю~ 🌸"
            )
            return
        await state.update_data(name=name)
        await state.set_state(RpCustomStates.waiting_emoji)
        await message.answer(
            profile_service.build_rp_custom_emoji_prompt_text(name),
            reply_markup=inline.rp_custom_prompt_keyboard(chat.chat_id),
        )
    except Exception as exc:
        error_handler.log_exception("вводе названия РП-команды", exc)
        await error_handler.notify_user_softly(message)


@router.message(RpCustomStates.waiting_emoji, F.chat.type == ChatType.PRIVATE)
async def on_rpc_emoji_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять эмодзи новой РП-команды."""
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return
        if (message.text or "").strip().lower() in CANCEL_WORDS:
            await state.clear()
            await message.answer("Хорошо, отменила~ 🌸")
            await _back_to_menu(message, db, chat)
            return
        raw = (message.text or "").strip()
        if not raw:
            await message.answer("Пришли эмодзи 🌸")
            return
        emoji = raw.split()[0][:8]
        await state.update_data(emoji=emoji)
        await state.set_state(RpCustomStates.waiting_action)
        data = await state.get_data()
        name = str(data.get("name") or "")
        await message.answer(
            profile_service.build_rp_custom_action_prompt_text(name),
            reply_markup=inline.rp_custom_prompt_keyboard(chat.chat_id),
        )
    except Exception as exc:
        error_handler.log_exception("вводе эмодзи РП-команды", exc)
        await error_handler.notify_user_softly(message)


@router.message(RpCustomStates.waiting_action, F.chat.type == ChatType.PRIVATE)
async def on_rpc_action_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять текст действия и спросить про 18+."""
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return
        if (message.text or "").strip().lower() in CANCEL_WORDS:
            await state.clear()
            await message.answer("Хорошо, отменила~ 🌸")
            await _back_to_menu(message, db, chat)
            return
        action = (message.text or "").strip()
        if not action:
            await message.answer("Пришли текст действия 🌸")
            return
        action = action[: config.MAX_CUSTOM_RP_ACTION_LENGTH]
        await state.update_data(action=action)
        data = await state.get_data()
        name = str(data.get("name") or "")
        await message.answer(
            f"Команда .{name}: действие «{action}»\n\n"
            "Сделать её 18+ (работает только при включённых 18+ командах)?",
            reply_markup=inline.rp_custom_nsfw_keyboard(chat.chat_id),
        )
    except Exception as exc:
        error_handler.log_exception("вводе действия РП-команды", exc)
        await error_handler.notify_user_softly(message)
