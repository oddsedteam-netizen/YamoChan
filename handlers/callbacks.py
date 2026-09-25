"""Все обработчики инлайн-кнопок бота.

Маршрутизация построена на префиксах ``callback_data``:
    * ``profile:`` — профиль и «Возможности»;
    * ``chats:`` — список чатов пользователя;
    * ``chat_detail:`` — карточка конкретного чата;
    * ``settings:`` — настройки чата (только владельцу);
    * ``back:`` — возврат в главное меню, список чатов или профиль.

Каждый обработчик обёрнут в ``try/except`` и при любой ошибке просто
показывает мягкое сообщение, а бот продолжает работать.
"""

from __future__ import annotations

import logging
from typing import Final, Optional

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
from database import queries
from database.db import Database
from database.models import ChatInfo
from keyboards import inline
from services import antiraid as antiraid_service
from services import permissions, profile as profile_service
from utils import error_handler, html_utils, telegram

logger = logging.getLogger(__name__)

#: Роутер инлайн-кнопок.
router: Final[Router] = Router(name="callbacks")

#: Соответствие коротких ключей кнопок и полей в настройках чата.
TOGGLE_KEYS: Final[dict[str, str]] = {
    "nsfw": "nsfw_commands",
    "antiraid": "antiraid",
    "call": "call_mode",
    "service": config.DELETE_SERVICE_MESSAGES_KEY,
}

#: Человекочитаемые названия переключателей для текста ответа.
TOGGLE_TITLES: Final[dict[str, str]] = {
    "nsfw": "🔞 18+ команды",
    "antiraid": "🛡 Антирейд",
    "call": "📞 Call-режим",
    "service": "🧹 Служебные сообщения",
}

#: Текст «Возможности» недоступен / доступа нет.
NO_ACCESS_TEXT: Final[str] = "Настройки видит только владелец чата~ 🌸"


async def _render(
    callback: CallbackQuery,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Показать новый экран вместо текущего сообщения.

    Если сообщение нельзя отредактировать (например, оно старое или
    недоступно), отправляем новое сообщение. Текст заранее чинится
    :func:`yamochan.utils.html_utils.safe_html_text`, поэтому один неверный
    ``<`` в данных не ломает экран целиком.

    :param callback: нажатие на инлайн-кнопку.
    :param text: текст нового экрана.
    :param markup: клавиатура нового экрана.
    """
    message: Optional[Message] = callback.message if isinstance(callback.message, Message) else None
    if message is None:
        if callback.from_user is not None:
            try:
                await callback.bot.send_message(
                    callback.from_user.id,
                    html_utils.safe_html_text(text),
                    reply_markup=markup,
                )
            except Exception as exc:  # noqa: BLE001
                error_handler.log_exception("отправке сообщения вместо редактирования", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:  # noqa: BLE001 - экран не важнее работы бота
        error_handler.log_exception("обновлении экрана кнопок", exc)


async def _is_chat_owner(
    bot: Bot,
    db: Database,
    chat: Optional[ChatInfo],
    user_id: int,
) -> bool:
    """Проверить, что пользователь владеет чатом.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat: информация о чате из базы.
    :param user_id: идентификатор проверяемого пользователя.
    """
    if user_id in config.SUPERADMIN_IDS:
        return True
    if chat is None:
        return False
    if chat.owner_id is not None:
        return chat.owner_id == user_id
    # Владелец ещё не определён — уточняем у Telegram и сохраняем результат.
    try:
        owner_id, _ = await permissions.sync_chat_owner(bot, db, chat.chat_id)
        if owner_id is not None:
            return owner_id == user_id
        member = await permissions.get_chat_member(bot, chat.chat_id, user_id)
        return member is not None and member.status == "creator"
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("проверке владельца чата", exc)
        return False


async def _show_main_menu(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Показать главное меню с кнопками снятия активной защиты.

    :param callback: нажатие на кнопку «Назад».
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    """
    user = callback.from_user
    guarded: list[ChatInfo] = []
    if user is not None:
        await queries.ensure_user(db, user.id, user.username, user.first_name)
        try:
            guarded = await antiraid_service.active_protection_chats(db, user.id)
        except Exception as exc:  # noqa: BLE001 - меню важнее списка защиты
            error_handler.log_exception("поиске чатов под защитой", exc)
    await _render(
        callback,
        profile_service.build_main_menu_text(len(guarded)),
        inline.main_menu_keyboard(guarded),
    )


async def _show_profile(callback: CallbackQuery, db: Database) -> None:
    """Показать карточку профиля пользователя."""
    user = callback.from_user
    if user is None:
        return
    profile = await queries.ensure_user(db, user.id, user.username, user.first_name)
    stats = await queries.get_user_global_stats(db, user.id)
    warns_by_chat = await queries.get_user_warns_by_chat(db, user.id)
    pairs = await queries.get_user_chats(db, user.id)
    chat_titles = {chat.chat_id: chat.display_title for chat, _ in pairs}
    active_punishments = await queries.count_active_punishments_global(db, user.id)

    await _render(
        callback,
        profile_service.build_profile_text(
            profile, stats, warns_by_chat, chat_titles, active_punishments
        ),
        inline.profile_keyboard(),
    )


async def _show_chats(callback: CallbackQuery, db: Database) -> None:
    """Показать список чатов, где бот видел пользователя."""
    user = callback.from_user
    if user is None:
        return
    pairs = await queries.get_user_chats(db, user.id)
    chats = [chat for chat, _ in pairs]
    await _render(
        callback,
        profile_service.build_chats_list_text(chats),
        inline.chats_keyboard(chats),
    )


async def _show_chat_detail(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> None:
    """Показать карточку конкретного чата."""
    user = callback.from_user
    if user is None:
        return
    chat = await queries.get_chat(db, chat_id)
    if chat is None:
        await callback.answer("Этот чат мне ещё не знаком~ 🌸", show_alert=True)
        await _show_chats(callback, db)
        return

    stats = await queries.get_chat_stats(db, chat_id)
    chat_user = await queries.get_chat_user(db, chat_id, user.id)
    is_owner = await _is_chat_owner(bot, db, chat, user.id)
    await _render(
        callback,
        profile_service.build_chat_card_text(chat, stats, is_owner, chat_user),
        inline.chat_detail_keyboard(chat_id, is_owner),
    )


async def _show_settings(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> None:
    """Показать меню настроек бота для владельца чата."""
    user = callback.from_user
    if user is None:
        return
    chat = await queries.get_chat(db, chat_id)
    if chat is None or not await _is_chat_owner(bot, db, chat, user.id):
        await callback.answer(NO_ACCESS_TEXT, show_alert=True)
        if chat is not None:
            await _show_chat_detail(callback, db, bot, chat_id)
        return

    settings = await queries.get_chat_settings(db, chat_id)
    await _render(
        callback,
        profile_service.build_settings_text(chat, settings),
        inline.settings_keyboard(chat_id, settings),
    )


async def _show_commands_settings(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> None:
    """Показать подменю «Настройка команд модератора»."""
    user = callback.from_user
    if user is None:
        return
    chat = await queries.get_chat(db, chat_id)
    if chat is None or not await _is_chat_owner(bot, db, chat, user.id):
        await callback.answer(NO_ACCESS_TEXT, show_alert=True)
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await _render(
        callback,
        profile_service.build_commands_settings_text(chat, settings),
        inline.commands_settings_keyboard(chat_id, settings),
    )


@router.callback_query(F.data.startswith(inline.CB_PROFILE))
async def on_profile_callbacks(callback: CallbackQuery, db: Database) -> None:
    """Обработать кнопки профиля и «Возможности»."""
    try:
        action = (callback.data or "").split(":", 1)[-1]
        if action == "capabilities":
            await _render(
                callback,
                profile_service.build_capabilities_text(),
                inline.back_to_menu_keyboard(),
            )
        else:
            await _show_profile(callback, db)
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке профиля", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data.startswith(inline.CB_CHATS))
async def on_chats_callbacks(callback: CallbackQuery, db: Database) -> None:
    """Обработать кнопку «Мои чаты»."""
    try:
        await _show_chats(callback, db)
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке «Мои чаты»", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data.startswith(inline.CB_CHAT_DETAIL))
async def on_chat_detail_callbacks(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Обработать нажатие на конкретный чат в списке."""
    try:
        raw_id = (callback.data or "").split(":", 1)[-1]
        if not raw_id.lstrip("-").isdigit():
            await callback.answer("Не поняла, о каком чате речь~ 🤔", show_alert=True)
            return
        await _show_chat_detail(callback, db, bot, int(raw_id))
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке чата", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data.startswith(inline.CB_SETTINGS))
async def on_settings_callbacks(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Обработать все кнопки настроек чата.

    Поддерживаемые форматы ``callback_data``:
        * ``settings:open:{chat_id}`` — открыть меню настроек;
        * ``settings:toggle:{key}:{chat_id}`` — переключить режим;
        * ``settings:commands:{chat_id}`` — подменю команд;
        * ``settings:command:{command}:{chat_id}`` — включить/выключить команду.
    """
    try:
        parts = (callback.data or "").split(":")
        # parts[0] == "settings"
        if len(parts) < 3:
            await callback.answer()
            return
        action = parts[1]
        user = callback.from_user

        if user is None:
            await callback.answer()
            return

        if action == "commands":
            await _show_commands_settings(callback, db, bot, _chat_id_from(parts[2]))
            await callback.answer("Подменю команд открыто~ 🛠")
            return

        if action == "command" and len(parts) >= 4:
            await _toggle_command(callback, db, bot, user.id, parts[2], _chat_id_from(parts[3]))
            return

        if action == "toggle" and len(parts) >= 4:
            await _toggle_setting(callback, db, bot, user.id, parts[2], _chat_id_from(parts[3]))
            return

        # По умолчанию — просто открыть настройки: settings:open:{chat_id}.
        await _show_settings(callback, db, bot, _chat_id_from(parts[-1]))
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке настроек", exc)
        await error_handler.notify_user_softly(callback)


def _chat_id_from(raw: str) -> int:
    """Разобрать идентификатор чата из ``callback_data``.

    :param raw: строка с числом (может быть отрицательной).
    :returns: идентификатор чата или ``0`` при ошибке разбора.
    """
    cleaned = raw.strip()
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return 0


async def _toggle_setting(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    user_id: int,
    key: str,
    chat_id: int,
) -> None:
    """Переключить один из режимов чата (18+, антирейд, Call).

    :param callback: нажатие на кнопку режима.
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    :param user_id: идентификатор нажавшего пользователя.
    :param key: короткий ключ кнопки (``nsfw``/``antiraid``/``call``).
    :param chat_id: идентификатор чата.
    """
    field = TOGGLE_KEYS.get(key)
    chat = await queries.get_chat(db, chat_id)
    if field is None or chat is None:
        await callback.answer("Не поняла, что переключать~ 🤔", show_alert=True)
        return
    if not await _is_chat_owner(bot, db, chat, user_id):
        await callback.answer(NO_ACCESS_TEXT, show_alert=True)
        return

    settings = await queries.get_chat_settings(db, chat_id)
    new_value = not bool(settings.get(field))
    settings = await queries.update_chat_setting(db, chat_id, field, new_value)
    logger.info(
        "Настройка %s в чате %s переключена в %s пользователем %s.",
        field,
        chat_id,
        new_value,
        user_id,
    )
    await _render(
        callback,
        profile_service.build_settings_text(chat, settings),
        inline.settings_keyboard(chat_id, settings),
    )
    title = TOGGLE_TITLES.get(key, key)
    state = "включено ✅" if new_value else "выключено ❌"
    await callback.answer(f"{title}: {state}")


async def _toggle_command(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    user_id: int,
    command: str,
    chat_id: int,
) -> None:
    """Включить или выключить одну команду модерации в чате.

    :param callback: нажатие на кнопку команды.
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    :param user_id: идентификатор нажавшего пользователя.
    :param command: название команды без префикса.
    :param chat_id: идентификатор чата.
    """
    chat = await queries.get_chat(db, chat_id)
    if chat is None or command not in config.MODERATION_COMMANDS:
        await callback.answer("Такой команды у меня нет~ 🤔", show_alert=True)
        return
    if not await _is_chat_owner(bot, db, chat, user_id):
        await callback.answer(NO_ACCESS_TEXT, show_alert=True)
        return

    settings = await queries.get_chat_settings(db, chat_id)
    enabled = bool((settings.get("commands") or {}).get(command, True))
    settings = await queries.update_chat_command(db, chat_id, command, not enabled)
    await _render(
        callback,
        profile_service.build_commands_settings_text(chat, settings),
        inline.commands_settings_keyboard(chat_id, settings),
    )
    title = config.COMMAND_TITLES.get(command, f".{command}")
    await callback.answer(f"{title}: {'выключена ❌' if enabled else 'включена ✅'}")


@router.callback_query(F.data.startswith(inline.CB_BACK))
async def on_back_callbacks(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Обработать кнопки «Назад»: меню, список чатов, профиль."""
    try:
        target = (callback.data or "").split(":", 1)[-1]
        if target == inline.BACK_CHATS:
            await _show_chats(callback, db)
        elif target == inline.BACK_PROFILE:
            await _show_profile(callback, db)
        else:
            await _show_main_menu(callback, db, bot)
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке «Назад»", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query()
async def on_unknown_callback(callback: CallbackQuery) -> None:
    """Ответить на устаревшую или неизвестную кнопку."""
    try:
        logger.info("Неизвестный callback_data: %r", callback.data)
        await callback.answer(
            "Эта кнопка устарела~ Открой меню заново: /start ",
            show_alert=False,
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("неизвестной кнопке", exc)
