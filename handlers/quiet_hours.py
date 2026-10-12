"""«Тихое время»: меню настроек, ввод интервала и текстов (владелец чата).

Кнопка «🌙 Тихое время» в меню настроек чата открывает это меню. Префикс
``callback_data`` — ``quiet:``. Владелец может включить режим, задать начало и
конец интервала (по МСК), выбрать дни недели, отредактировать текст закрытия и
сообщение об открытии, посмотреть превью.

Закрытием и открытием чата занимается :mod:`yamochan.services.quiet_hours`
(фоновая проверка), здесь — только интерфейс.
"""

from __future__ import annotations

import logging
from typing import Final, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
from database import queries
from database.db import Database
from database.models import ChatInfo
from keyboards import inline
from services import permissions, profile as profile_service, richtext
from services import quiet_hours as quiet_hours_service
from utils import error_handler, html_utils, telegram

logger = logging.getLogger(__name__)

router: Final[Router] = Router(name="quiet_hours")

CANCEL_WORDS: Final[frozenset[str]] = frozenset({"/cancel", "отмена", "cancel"})


class QuietStates(StatesGroup):
    """Состояния ввода настроек «тихого времени»."""

    waiting_start = State()
    waiting_end = State()
    waiting_text = State()
    waiting_open_text = State()


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
                error_handler.log_exception("отправке экрана тихого времени", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:
        error_handler.log_exception("обновлении экрана тихого времени", exc)


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


async def _show_menu(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
    note: Optional[str] = None,
) -> None:
    """Показать меню «Тихое время»."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await _render(
        callback,
        profile_service.build_quiet_hours_menu_text(chat, settings),
        inline.quiet_hours_keyboard(chat_id, settings),
    )
    await callback.answer(note or "")


async def _back_to_menu(message: Message, db: Database, chat: ChatInfo) -> None:
    """Показать меню «Тихое время» новым сообщением в личке."""
    settings = await queries.get_chat_settings(db, chat.chat_id)
    await message.answer(
        profile_service.build_quiet_hours_menu_text(chat, settings),
        reply_markup=inline.quiet_hours_keyboard(chat.chat_id, settings),
    )


async def _toggle_day(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
    day: int,
) -> None:
    """Включить/выключить один день недели и обновить экран."""
    settings = await queries.get_chat_settings(db, chat_id)
    days = set(
        quiet_hours_service.normalize_days(
            settings.get(config.QUIET_HOURS_DAYS_KEY)
        )
    )
    if day in days:
        days.discard(day)
    else:
        days.add(day)
    if not days:
        days = set(config.QUIET_HOURS_DEFAULT_DAYS)
    await queries.update_chat_setting(
        db, chat_id, config.QUIET_HOURS_DAYS_KEY, sorted(days)
    )
    try:
        await quiet_hours_service.apply_for_chat(db, bot, chat_id)
    except Exception as exc:
        error_handler.log_exception("применении тихого времени", exc)
    chat = await queries.get_chat(db, chat_id)
    settings = await queries.get_chat_settings(db, chat_id)
    await _render(
        callback,
        profile_service.build_quiet_days_text(
            chat if chat is not None else ChatInfo(chat_id=chat_id)
        ),
        inline.quiet_days_keyboard(chat_id, settings),
    )
    await callback.answer()


async def _preview(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> None:
    """Отправить владельцу в личку сообщение о закрытии."""
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    settings = await queries.get_chat_settings(db, chat_id)
    content = quiet_hours_service.quiet_message(
        settings, config.QUIET_HOURS_MESSAGE_PREFIX
    )
    if not content.has_content:
        await callback.answer(
            "Пока пусто~ Задай текст сообщения о закрытии 🌸", show_alert=True
        )
        return
    try:
        await bot.send_message(
            user.id, profile_service.build_quiet_hours_preview_text(settings)
        )
    except Exception as exc:
        logger.info("Превью тихого времени в личку не ушло: %s", exc)
        await callback.answer("Не смогла написать тебе в личку~ 🌸", show_alert=True)
        return
    if await richtext.send_content(bot, user.id, content, html_fallback=True):
        await callback.answer("Отправила превью в личку 👀")
    else:
        await callback.answer("Не смогла отправить превью~ 🌸", show_alert=True)


@router.callback_query(F.data.startswith(inline.CB_QUIET))
async def on_quiet_callbacks(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Обработать кнопки меню «Тихое время».

    Формат ``callback_data``: ``quiet:{menu|toggle|start|end|days|day|text|
    open_text|preview}:{chat_id}`` (у ``day`` перед ``chat_id`` идёт номер дня).
    """
    try:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        chat_id = _chat_id_from(parts[-1])
        chat = await _owner_chat(callback, db, bot, chat_id)
        if chat is None:
            return

        if action == "menu":
            await _show_menu(callback, db, bot, chat_id)
            return

        if action == "toggle":
            settings = await queries.get_chat_settings(db, chat_id)
            enabled = not bool(settings.get(config.QUIET_HOURS_ENABLED_KEY))
            await queries.update_chat_setting(
                db, chat_id, config.QUIET_HOURS_ENABLED_KEY, enabled
            )
            if not enabled:
                await queries.update_chat_setting(
                    db, chat_id, config.QUIET_HOURS_ACTIVE_KEY, False
                )
            try:
                await quiet_hours_service.apply_for_chat(db, bot, chat_id)
            except Exception as exc:
                error_handler.log_exception("применении тихого времени", exc)
            await _show_menu(
                callback,
                db,
                bot,
                chat_id,
                "Тихое время включено ✅" if enabled else "Тихое время выключено ❌",
            )
            return

        if action == "days":
            settings = await queries.get_chat_settings(db, chat_id)
            await _render(
                callback,
                profile_service.build_quiet_days_text(chat),
                inline.quiet_days_keyboard(chat_id, settings),
            )
            await callback.answer()
            return

        if action == "day" and len(parts) >= 4 and parts[2].isdigit():
            await _toggle_day(callback, db, bot, chat_id, int(parts[2]))
            return

        if action == "preview":
            await _preview(callback, db, bot, chat_id)
            return

        if action in {"start", "end"}:
            await state.set_state(
                QuietStates.waiting_start
                if action == "start"
                else QuietStates.waiting_end
            )
            await state.update_data(chat_id=chat_id)
            settings = await queries.get_chat_settings(db, chat_id)
            await _render(
                callback,
                profile_service.build_quiet_time_prompt_text(action),
                inline.quiet_hours_keyboard(chat_id, settings),
            )
            await callback.answer()
            return

        if action in {"text", "open_text"}:
            what = "open" if action == "open_text" else "close"
            await state.set_state(
                QuietStates.waiting_open_text
                if what == "open"
                else QuietStates.waiting_text
            )
            await state.update_data(chat_id=chat_id)
            settings = await queries.get_chat_settings(db, chat_id)
            await _render(
                callback,
                profile_service.build_quiet_message_prompt_text(what),
                inline.quiet_hours_keyboard(chat_id, settings),
            )
            await callback.answer()
            return

        await _show_menu(callback, db, bot, chat_id)
    except Exception as exc:
        error_handler.log_exception("кнопке тихого времени", exc)
        await error_handler.notify_user_softly(callback)


@router.message(QuietStates.waiting_start, F.chat.type == ChatType.PRIVATE)
async def on_quiet_start_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять время начала «тихого времени»."""
    await _save_time(message, state, db, bot, "start")


@router.message(QuietStates.waiting_end, F.chat.type == ChatType.PRIVATE)
async def on_quiet_end_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять время конца «тихого времени»."""
    await _save_time(message, state, db, bot, "end")


@router.message(QuietStates.waiting_text, F.chat.type == ChatType.PRIVATE)
async def on_quiet_text_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять текст сообщения о закрытии чата."""
    await _save_message(message, state, db, bot, "close")


@router.message(QuietStates.waiting_open_text, F.chat.type == ChatType.PRIVATE)
async def on_quiet_open_text_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять текст сообщения об открытии чата."""
    await _save_message(message, state, db, bot, "open")


async def _save_time(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    what: str,
) -> None:
    """Сохранить время начала или конца интервала."""
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return
        if (message.text or "").strip().lower() in CANCEL_WORDS:
            await state.clear()
            await message.answer("Хорошо, отменила~ 🌸")
            await _back_to_menu(message, db, chat)
            return
        parsed = quiet_hours_service.parse_hhmm(message.text)
        if parsed is None:
            await message.answer(
                "Не поняла время~ Пришли в формате ЧЧ:ММ (по МСК), "
                "например 23:00 🌸"
            )
            return
        key = (
            config.QUIET_HOURS_START_KEY
            if what == "start"
            else config.QUIET_HOURS_END_KEY
        )
        await queries.update_chat_setting(
            db, chat.chat_id, key, quiet_hours_service.format_hhmm(parsed)
        )
        try:
            await quiet_hours_service.apply_for_chat(db, bot, chat.chat_id)
        except Exception as exc:
            error_handler.log_exception("применении тихого времени", exc)
        await state.clear()
        await message.answer("⏰ Время сохранено~")
        await _back_to_menu(message, db, chat)
    except Exception as exc:
        error_handler.log_exception("сохранении времени тихого времени", exc)
        await error_handler.notify_user_softly(message)


async def _save_message(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    what: str,
) -> None:
    """Сохранить текст сообщения закрытия (``close``) или открытия (``open``)."""
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return
        if (message.text or "").strip().lower() in CANCEL_WORDS:
            await state.clear()
            await message.answer("Хорошо, отменила~ 🌸")
            await _back_to_menu(message, db, chat)
            return
        content = richtext.content_from_message(message)
        if not content.has_content:
            await message.answer("Не вижу текста~ Пришли сообщение 🌸")
            return
        prefix = (
            config.QUIET_HOURS_OPEN_MESSAGE_PREFIX
            if what == "open"
            else config.QUIET_HOURS_MESSAGE_PREFIX
        )
        await queries.update_chat_setting(
            db, chat.chat_id, f"{prefix}_text", content.text
        )
        await queries.update_chat_setting(
            db, chat.chat_id, f"{prefix}_entities", content.entities
        )
        await queries.update_chat_setting(
            db, chat.chat_id, f"{prefix}_photo", content.photo
        )
        await state.clear()
        await message.answer("✅ Текст сохранён~")
        await _back_to_menu(message, db, chat)
    except Exception as exc:
        error_handler.log_exception("сохранении текста тихого времени", exc)
        await error_handler.notify_user_softly(message)
