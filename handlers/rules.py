"""Правила чата и своё приветствие: настройки владельца и показ в чате.

Кнопки настроек живут в меню чата (префиксы ``rules:`` и ``greeting:``).
Текст владельца сохраняется вместе с сущностями (премиум-эмодзи) и фото —
за это отвечает :mod:`yamochan.services.richtext`.

Возможности:
    * правила: отправка при входе свёрнутой цитатой (``expandable_blockquote``);
    * приветствие: текст с фото, инлайн-кнопки (до
      :data:`config.MAX_GREETING_BUTTONS`), показ профиля новичка;
    * превентивные муты помеченным аккаунтам (``marked_mute_*``);
    * спец-команды ``{name}``, ``{id}``, ``{mention}``, ``{chat}``, ``{count}``
      — подставляются данными вошедшего участника, а не админа.

Приветствие отправляется ровно таким, каким его задал владелец: бот не
добавляет «от себя» ни шапки «Привет, имя», ни других строк. Предпросмотр
(кнопка в меню и команда ``.приветствие``) показывает тот же текст, поэтому
владелец видит именно то, что увидят новички.

В группе работают команды:
    * ``.правила`` / ``/правила`` — показать правила (доступна всем);
    * ``.приветствие`` / ``/приветствие`` — предпросмотр приветствия: админу
      в личку, чтобы не спамить в чат.

Приватность: бот не упоминает админов и владельца по имени, а юзернеймы
не показывает вообще — только ``first_name`` внутри ``tg://user`` ссылок.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
from database import queries
from database.db import Database
from database.models import ChatInfo
from keyboards import inline
from services import permissions, profile as profile_service, richtext, time_parser
from utils import error_handler, html_utils, telegram

logger = logging.getLogger(__name__)

#: Роутер правил и приветствия.
router: Final[Router] = Router(name="rules")

#: Групповые типы чатов.
GROUP_CHAT_TYPES: Final[set[str]] = {ChatType.GROUP, ChatType.SUPERGROUP}

#: Префикс полей настроек для каждого блока.
FIELD_PREFIXES: Final[dict[str, str]] = {"rules": "rules", "greeting": "greeting"}

#: Флаг включения блока в настройках чата.
ENABLED_KEYS: Final[dict[str, str]] = {
    "rules": "rules_enabled",
    "greeting": "greeting_enabled",
}

#: Альтернативные (старые) имена флагов — читаем их тоже.
ENABLED_ALIASES: Final[dict[str, str]] = {"greeting": "welcome_enabled"}

#: Настройка «показывать правила при входе».
RULES_JOIN_KEY: Final[str] = "rules_show_on_join"
#: Настройка «показывать профиль новичка при входе».
GREETING_PROFILE_KEY: Final[str] = "greeting_show_profile"
#: Настройка с сохранёнными кнопками приветствия.
GREETING_BUTTONS_KEY: Final[str] = "greeting_buttons"
#: Настройки превентивного мута помеченным.
MARKED_MUTE_KEY: Final[str] = "marked_mute_enabled"
MARKED_MUTE_DURATION_KEY: Final[str] = "marked_mute_duration"
#: Настройка «анонимное приветствие»: не раскрывать данные новичка.
GREETING_ANONYMOUS_KEY: Final[str] = config.WELCOME_ANONYMOUS_KEY
#: Настройка «длительность проверочного мута» в анонимном режиме.
WELCOME_CHECK_DURATION_KEY: Final[str] = config.WELCOME_CHECK_DURATION_KEY
#: Настройки мута при входе для ВСЕХ новых участников.
JOIN_MUTE_KEY: Final[str] = config.JOIN_MUTE_ENABLED_KEY
JOIN_MUTE_DURATION_KEY: Final[str] = config.JOIN_MUTE_DURATION_KEY

#: Подписи блоков для ответов.
KIND_TITLES: Final[dict[str, str]] = {"rules": "Правила", "greeting": "Приветствие"}

#: С каких символов начинаются команды правил.
COMMAND_PREFIXES: Final[tuple[str, ...]] = (config.COMMAND_PREFIX, "/")


class ContentStates(StatesGroup):
    """Ожидание содержимого и настроек от владельца чата."""

    waiting_rules = State()
    waiting_greeting = State()
    waiting_greeting_buttons = State()
    waiting_button_text = State()
    waiting_button_url = State()
    waiting_mute_duration = State()
    waiting_check_duration = State()
    waiting_join_mute_duration = State()


def state_for(kind: str) -> State:
    """Состояние FSM для ожидания текста нужного блока."""
    return (
        ContentStates.waiting_rules
        if kind == "rules"
        else ContentStates.waiting_greeting
    )


def setting_flag(settings: dict[str, Any], key: str, alias: str = "") -> bool:
    """Прочитать булев флаг настройки с поддержкой старого имени.

    :param settings: настройки чата.
    :param key: основное имя настройки.
    :param alias: альтернативное имя (например, ``welcome_enabled``).
    """
    if settings.get(key) is not None:
        return bool(settings.get(key))
    return bool(settings.get(alias)) if alias else False


def enabled(settings: dict[str, Any], kind: str) -> bool:
    """Включён ли блок и есть ли в нём что показывать."""
    if not setting_flag(
        settings,
        ENABLED_KEYS.get(kind, ""),
        ENABLED_ALIASES.get(kind, ""),
    ):
        return False
    return content(settings, kind).has_content


def content(settings: dict[str, Any], kind: str) -> richtext.RichContent:
    """Содержимое блока (текст + сущности + фото) из настроек чата."""
    return richtext.content_from_settings(settings, FIELD_PREFIXES.get(kind, kind))


def saved_buttons(settings: dict[str, Any]) -> list[dict[str, str]]:
    """Сохранённые кнопки приветствия (с поддержкой старого имени настройки)."""
    raw = settings.get(GREETING_BUTTONS_KEY)
    if raw is None:
        raw = settings.get("welcome_buttons")
    return richtext.parse_saved_buttons(raw)


def parse_info_command(text: Optional[str]) -> Optional[str]:
    """Разобрать ``.правила``/``/приветствие`` в имя блока.

    Поддерживаются точка и слэш, а также обращение к боту
    (``.правила@YamoChanBot``).

    :param text: текст сообщения.
    :returns: ``"rules"``, ``"greeting"`` или ``None``.
    """
    raw = (text or "").strip()
    if not raw or raw[0] not in COMMAND_PREFIXES:
        return None
    body = raw[1:].strip()
    if not body:
        return None
    head = body.split()[0].lower().split("@", 1)[0].strip()
    if head in (config.COMMAND_RULES, "rules"):
        return "rules"
    if head in (config.COMMAND_GREETING, "greeting"):
        return "greeting"
    return None


def _chat_id_from(raw: Optional[str]) -> int:
    """Разобрать идентификатор чата из ``callback_data``."""
    cleaned = (raw or "").strip()
    return int(cleaned) if cleaned.lstrip("-").isdigit() else 0


async def _render(
    callback: CallbackQuery,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Показать новый экран вместо текущего сообщения.

    Текст проходит через :func:`yamochan.utils.html_utils.safe_html_text`,
    а отправка — через :func:`yamochan.utils.telegram.render_screen`: случайный
    символ ``<`` в названии чата или в подписи настроек больше не ломает весь
    экран ошибкой «Unsupported start tag».
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
                error_handler.log_exception("отправке экрана правил", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:  # noqa: BLE001 - экран не важнее работы бота
        error_handler.log_exception("обновлении экрана правил", exc)


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


def _trim(content_value: richtext.RichContent) -> richtext.RichContent:
    """Обрезать слишком длинный текст (обёртка над сервисом)."""
    return richtext.trim_content(content_value)


async def _save_content(
    db: Database,
    chat_id: int,
    kind: str,
    content_value: richtext.RichContent,
) -> dict[str, Any]:
    """Сохранить содержимое блока в настройках чата и вернуть настройки."""
    prefix = FIELD_PREFIXES.get(kind, kind)
    trimmed = _trim(content_value)
    await queries.update_chat_setting(db, chat_id, f"{prefix}_text", trimmed.text)
    await queries.update_chat_setting(db, chat_id, f"{prefix}_entities", trimmed.entities)
    settings = await queries.update_chat_setting(db, chat_id, f"{prefix}_photo", trimmed.photo)
    logger.info(
        "%s чата %s обновлены: %s символов, сущностей %s, фото %s.",
        KIND_TITLES.get(kind, kind),
        chat_id,
        len(trimmed.text),
        len(trimmed.entities),
        bool(trimmed.photo),
    )
    return settings


async def _show_menu(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    kind: str,
    chat_id: int,
    note: Optional[str] = None,
) -> None:
    """Показать меню блока «правила» или «приветствие»."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    if kind == "rules":
        text = profile_service.build_rules_menu_text(chat, settings)
        markup = inline.rules_keyboard(chat_id, settings)
    else:
        text = profile_service.build_greeting_menu_text(chat, settings)
        markup = inline.greeting_keyboard(chat_id, settings)
    await _render(callback, text, markup)
    await callback.answer(note or "")


async def _toggle(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    kind: str,
    chat_id: int,
) -> None:
    """Включить или выключить блок и обновить экран."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    key = ENABLED_KEYS.get(kind, kind)
    if not bool(settings.get(key)) and not content(settings, kind).has_content:
        await callback.answer(
            "Сначала добавь текст~ 📝", show_alert=True
        )
        return
    new_value = not bool(settings.get(key))
    settings = await queries.update_chat_setting(db, chat_id, key, new_value)
    if kind == "rules":
        await _render(
            callback,
            profile_service.build_rules_menu_text(chat, settings),
            inline.rules_keyboard(chat_id, settings),
        )
    else:
        await _render(
            callback,
            profile_service.build_greeting_menu_text(chat, settings),
            inline.greeting_keyboard(chat_id, settings),
        )
    await callback.answer(
        f"{KIND_TITLES.get(kind, kind)}: {'включено ✅' if new_value else 'выключено ❌'}"
    )


async def _ask_content(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    kind: str,
    chat_id: int,
) -> None:
    """Попросить владельца прислать текст (и/или фото) для блока."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    await state.set_state(state_for(kind))
    await state.update_data(chat_id=chat_id, kind=kind)
    await _render(
        callback,
        profile_service.build_content_prompt_text(kind),
        inline.content_prompt_keyboard(chat_id, kind),
    )
    await callback.answer()


async def _drop_photo(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    kind: str,
    chat_id: int,
) -> None:
    """Убрать фото из блока, сохранив текст."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    prefix = FIELD_PREFIXES.get(kind, kind)
    settings = await queries.update_chat_setting(db, chat_id, f"{prefix}_photo", None)
    if kind == "rules":
        await _render(
            callback,
            profile_service.build_rules_menu_text(chat, settings),
            inline.rules_keyboard(chat_id, settings),
        )
    else:
        await _render(
            callback,
            profile_service.build_greeting_menu_text(chat, settings),
            inline.greeting_keyboard(chat_id, settings),
        )
    await callback.answer("Фото убрано 🖼")


async def _clear(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    kind: str,
    chat_id: int,
) -> None:
    """Очистить содержимое блока и выключить его."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    prefix = FIELD_PREFIXES.get(kind, kind)
    await queries.update_chat_setting(db, chat_id, f"{prefix}_text", "")
    await queries.update_chat_setting(db, chat_id, f"{prefix}_entities", [])
    await queries.update_chat_setting(db, chat_id, f"{prefix}_photo", None)
    if kind == "greeting":
        # Кнопки без текста приветствия бессмысленны — убираем вместе с ним.
        await queries.update_chat_setting(db, chat_id, GREETING_BUTTONS_KEY, [])
    settings = await queries.update_chat_setting(db, chat_id, ENABLED_KEYS[kind], False)
    if kind == "rules":
        await _render(
            callback,
            profile_service.build_rules_menu_text(chat, settings),
            inline.rules_keyboard(chat_id, settings),
        )
    else:
        await _render(
            callback,
            profile_service.build_greeting_menu_text(chat, settings),
            inline.greeting_keyboard(chat_id, settings),
        )
    await callback.answer(profile_service.build_content_cleared_text(kind))


async def _preview(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    kind: str,
    chat_id: int,
) -> None:
    """Показать владельцу сохранённый блок в личке (с премиум-эмодзи)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    user = callback.from_user
    if user is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    content_value = content(settings, kind)
    if not content_value.has_content:
        await callback.answer("Пока пусто~ Сначала пришли текст 📝", show_alert=True)
        return

    title, count = await chat_placeholders(
        bot,
        db,
        chat_id,
        fallback_title=chat.display_title,
    )
    markup: Optional[InlineKeyboardMarkup] = None
    values = richtext.placeholder_values(user, chat_title=title, member_count=count)
    # Предпросмотр показывает РОВНО контент владельца: спец-команды
    # подставляются данными того, кто нажал кнопку. Бот ничего не добавляет
    # «от себя» — ни шапки «Привет, имя», ни приветственной строки.
    content_value = richtext.apply_placeholders(content_value, values, user=user)
    if kind == "greeting":
        # Кнопки владельца видно только у приветствия.
        markup = inline.saved_buttons_keyboard(settings.get(GREETING_BUTTONS_KEY))

    sent = await richtext.send_content(
        bot,
        user.id,
        content_value,
        reply_markup=markup,
        html_fallback=False,
    )
    if sent:
        await callback.answer("Отправила в личку 👀")
    else:
        await callback.answer(
            profile_service.build_greeting_dm_failed_text(), show_alert=True
        )


async def _toggle_setting(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    kind: str,
    chat_id: int,
    key: str,
    title: str,
) -> None:
    """Переключить одну настройку блока и обновить экран.

    :param callback: нажатие на кнопку.
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    :param kind: ``rules`` или ``greeting``.
    :param chat_id: идентификатор чата.
    :param key: имя настройки в ``chats.settings``.
    :param title: подпись для всплывающего ответа.
    """
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    new_value = not bool(settings.get(key))
    await queries.update_chat_setting(db, chat_id, key, new_value)
    state = "включено ✅" if new_value else "выключено ❌"
    await _show_menu(callback, db, bot, kind, chat_id, note=f"{title}: {state}")


async def _show_mute_menu(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> None:
    """Показать подменю «Муты помеченным»."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await _render(
        callback,
        profile_service.build_marked_mute_menu_text(chat, settings),
        inline.marked_mute_keyboard(chat_id, settings),
    )
    await callback.answer()


async def _ask_mute_duration(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
) -> None:
    """Попросить новую длительность превентивного мута (FSM)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await state.set_state(ContentStates.waiting_mute_duration)
    await state.update_data(chat_id=chat_id)
    await _render(
        callback,
        profile_service.build_marked_mute_duration_prompt_text(settings),
        inline.marked_mute_keyboard(chat_id, settings),
    )
    await callback.answer()


async def _ask_check_duration(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
) -> None:
    """Попросить длительность проверочного мута (FSM, анонимный режим)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await state.set_state(ContentStates.waiting_check_duration)
    await state.update_data(chat_id=chat_id)
    await _render(
        callback,
        profile_service.build_welcome_check_duration_prompt_text(settings),
        inline.greeting_keyboard(chat_id, settings),
    )
    await callback.answer()


async def _show_join_mute_menu(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> None:
    """Показать подменю «Мут при входе» (для всех новых участников)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await _render(
        callback,
        profile_service.build_join_mute_menu_text(chat, settings),
        inline.join_mute_keyboard(chat_id, settings),
    )
    await callback.answer()


async def _ask_join_mute_duration(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
) -> None:
    """Попросить длительность мута при входе (FSM)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await state.set_state(ContentStates.waiting_join_mute_duration)
    await state.update_data(chat_id=chat_id)
    await _render(
        callback,
        profile_service.build_join_mute_duration_prompt_text(settings),
        inline.join_mute_keyboard(chat_id, settings),
    )
    await callback.answer()


async def _ask_button_name(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
) -> None:
    """Попросить название инлайн-кнопки приветствия (FSM)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    buttons = saved_buttons(settings)
    if len(buttons) >= config.MAX_GREETING_BUTTONS:
        await callback.answer(
            f"Уже {config.MAX_GREETING_BUTTONS} кнопок — больше нельзя~ 🔘",
            show_alert=True,
        )
        return
    await state.set_state(ContentStates.waiting_button_text)
    await state.update_data(chat_id=chat_id)
    await _render(
        callback,
        profile_service.build_greeting_button_name_prompt_text(
            len(buttons) + 1, config.MAX_GREETING_BUTTONS
        ),
        inline.greeting_button_prompt_keyboard(chat_id),
    )
    await callback.answer()


async def _finish_buttons(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
) -> None:
    """Закончить настройку кнопок приветствия и вернуться в меню."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    await state.clear()
    settings = await queries.get_chat_settings(db, chat_id)
    count = len(saved_buttons(settings))
    await _show_menu(
        callback,
        db,
        bot,
        "greeting",
        chat_id,
        note=profile_service.build_greeting_buttons_done_text(count),
    )


async def _drop_buttons(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> None:
    """Убрать все инлайн-кнопки приветствия."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    await queries.update_chat_setting(db, chat_id, GREETING_BUTTONS_KEY, [])
    await _show_menu(
        callback,
        db,
        bot,
        "greeting",
        chat_id,
        note=profile_service.build_buttons_cleared_text(),
    )


async def _dispatch(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    kind: str,
    action: str,
    chat_id: int,
) -> None:
    """Развести действия кнопок блока по обработчикам."""
    if action == "menu":
        await _show_menu(callback, db, bot, kind, chat_id)
    elif action == "toggle":
        await _toggle(callback, db, bot, kind, chat_id)
    elif action == "edit":
        await _ask_content(callback, db, bot, state, kind, chat_id)
    elif action == "preview":
        await _preview(callback, db, bot, kind, chat_id)
    elif action == "drop_photo":
        await _drop_photo(callback, db, bot, kind, chat_id)
    elif action == "clear":
        await _clear(callback, db, bot, kind, chat_id)
    elif action == "join":
        await _toggle_setting(
            callback, db, bot, "rules", chat_id, RULES_JOIN_KEY, "🚪 Правила при входе"
        )
    elif action == "profile":
        await _toggle_setting(
            callback,
            db,
            bot,
            "greeting",
            chat_id,
            GREETING_PROFILE_KEY,
            "📋 Показ профиля",
        )
    elif action == "anonymous":
        await _toggle_setting(
            callback,
            db,
            bot,
            "greeting",
            chat_id,
            GREETING_ANONYMOUS_KEY,
            "🔒 Анонимный привет",
        )
    elif action == "check_duration":
        await _ask_check_duration(callback, db, bot, state, chat_id)
    elif action == "mute_menu":
        await _show_mute_menu(callback, db, bot, chat_id)
    elif action == "mute_toggle":
        await _toggle_setting(
            callback, db, bot, "greeting", chat_id, MARKED_MUTE_KEY, "🔇 Автомут"
        )
    elif action == "mute_duration":
        await _ask_mute_duration(callback, db, bot, state, chat_id)
    elif action == "mute_back":
        await _show_menu(callback, db, bot, "greeting", chat_id)
    elif action == "join_mute_menu":
        await _show_join_mute_menu(callback, db, bot, chat_id)
    elif action == "join_mute_toggle":
        await _toggle_setting(
            callback,
            db,
            bot,
            "greeting",
            chat_id,
            JOIN_MUTE_KEY,
            "🔔 Мут при входе",
        )
    elif action == "join_mute_duration":
        await _ask_join_mute_duration(callback, db, bot, state, chat_id)
    elif action == "join_mute_back":
        await _show_menu(callback, db, bot, "greeting", chat_id)
    elif action in ("buttons", "buttons_yes", "button_more"):
        await _ask_button_name(callback, db, bot, state, chat_id)
    elif action == "buttons_done":
        await _finish_buttons(callback, db, bot, state, chat_id)
    elif action == "drop_buttons":
        await _drop_buttons(callback, db, bot, chat_id)
    else:
        await callback.answer("Не знаю такую кнопку~ 🤔")


# ---------------------------------------------------------------------------
# Использование содержимого: правила в чате и приветствие новичка
# ---------------------------------------------------------------------------
async def member_count(bot: Bot, chat_id: int) -> Optional[int]:
    """Сколько участников в чате (для ``{count}``) — без падения при ошибке.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    """
    try:
        return int(await bot.get_chat_member_count(chat_id))
    except Exception as exc:  # noqa: BLE001 - счётчик не важнее сообщения
        logger.debug("Не удалось узнать число участников чата %s: %s", chat_id, exc)
        return None


async def chat_placeholders(
    bot: Bot,
    db: Database,
    chat_id: int,
    *,
    fallback_title: str = "",
) -> tuple[str, Optional[int]]:
    """Название и количество участников чата для спец-команд.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param fallback_title: название на случай, если чата нет в базе.
    """
    chat_info = await queries.get_chat(db, chat_id)
    title = chat_info.display_title if chat_info is not None else (fallback_title or "")
    count = await member_count(bot, chat_id)
    if count is None and chat_info is not None:
        count = chat_info.members_count
    return title, count


async def send_rules_message(
    bot: Bot,
    chat_id: int,
    settings: dict[str, Any],
    *,
    member: Any = None,
    chat_title: str = "",
    member_count_value: Optional[int] = None,
    quoted: bool = False,
) -> bool:
    """Отправить правила чата (фото и/или текст с премиум-эмодзи).

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    :param settings: настройки чата.
    :param member: тот, чьими данными подставляются спец-команды.
    :param chat_title: название чата для ``{chat}``.
    :param member_count_value: число участников для ``{count}``.
    :param quoted: обернуть текст в сворачиваемую цитату (при входе).
    """
    content_value = content(settings, "rules")
    if not content_value.has_content:
        return False
    if member is not None:
        values = richtext.placeholder_values(
            member,
            chat_title=chat_title,
            member_count=member_count_value,
        )
        content_value = richtext.apply_placeholders(content_value, values, user=member)
    if quoted:
        content_value = richtext.build_quoted_content(content_value)
    return await richtext.send_content(
        bot,
        chat_id,
        content_value,
        html_fallback=member is None,
    )


async def send_rules_on_join(
    bot: Bot,
    db: Database,
    chat_id: int,
    settings: dict[str, Any],
    member: Any,
) -> bool:
    """Отправить правила новому участнику, если это включено.

    Правила уходят свёрнутой цитатой и с подставленными данными именно
    вошедшего участника (а не админа).

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param settings: настройки чата.
    :param member: вошедший пользователь.
    :returns: ``True``, если сообщение ушло.
    """
    if not bool(settings.get(RULES_JOIN_KEY)):
        return False
    title, count = await chat_placeholders(bot, db, chat_id)
    return await send_rules_message(
        bot,
        chat_id,
        settings,
        member=member,
        chat_title=title,
        member_count_value=count,
        quoted=True,
    )


async def send_custom_greeting(
    bot: Bot,
    db: Database,
    chat_id: int,
    settings: dict[str, Any],
    member: Any,
) -> bool:
    """Отправить своё приветствие новому участнику.

    Используется обработчиком входов (:func:`handle_user_join`): если
    приветствие настроено и включено, в чат уходит ровно текст владельца —
    бот ничего не добавляет «от себя» (ни «Привет, имя», ни другой шапки).
    Все спец-команды (``{name}``, ``{mention}``, ``{chat}``, …) подставляются
    данными вошедшего участника, к сообщению добавляются сохранённые кнопки.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param settings: настройки чата.
    :param member: вошедший пользователь.
    :returns: ``True``, если сообщение ушло.
    """
    if not enabled(settings, "greeting"):
        return False
    title, count = await chat_placeholders(bot, db, chat_id)
    values = richtext.placeholder_values(member, chat_title=title, member_count=count)
    personal = richtext.apply_placeholders(
        content(settings, "greeting"), values, user=member
    )
    markup = inline.saved_buttons_keyboard(settings.get(GREETING_BUTTONS_KEY))
    return await richtext.send_content(
        bot,
        chat_id,
        personal,
        reply_markup=markup,
        html_fallback=False,
    )


# ---------------------------------------------------------------------------
# Команды в группе
# ---------------------------------------------------------------------------
class InfoCommandFilter(BaseFilter):
    """Фильтр: сообщение является командой правил или приветствия."""

    def __init__(self, kind: str) -> None:
        """Запомнить, какую команду ждём (``rules`` или ``greeting``)."""
        self._kind = kind

    async def __call__(self, message: Message) -> Any:
        """Проверить текст сообщения."""
        return parse_info_command(message.text) == self._kind


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), InfoCommandFilter("rules"))
async def handle_rules_command(message: Message, db: Database, bot: Bot) -> None:
    """Показать правила чата по команде ``.правила`` / ``/правила``."""
    try:
        chat_id = message.chat.id
        settings = await queries.get_chat_settings(db, chat_id)
        if not setting_flag(settings, ENABLED_KEYS["rules"]):
            await message.reply(profile_service.build_rules_disabled_text())
            return
        title, count = await chat_placeholders(
            bot,
            db,
            chat_id,
            fallback_title=message.chat.title or "",
        )
        if not await send_rules_message(
            bot,
            chat_id,
            settings,
            member=message.from_user,
            chat_title=title,
            member_count_value=count,
        ):
            await message.reply(profile_service.build_rules_not_found_text())
    except Exception as exc:  # noqa: BLE001 - команда не должна ронять бота
        error_handler.log_exception("команде правил", exc)
        await error_handler.notify_user_softly(message)


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), InfoCommandFilter("greeting"))
async def handle_greeting_command(message: Message, db: Database, bot: Bot) -> None:
    """Показать предпросмотр приветствия администратору в личке.

    В чат ничего не отправляется: приветствие должно появляться только при
    входе новичка, а не по запросу.
    """
    try:
        user = message.from_user
        if user is None:
            return
        chat_id = message.chat.id
        if not await permissions.is_chat_administrator(bot, chat_id, user.id):
            await message.reply(config.NOT_ADMIN_MESSAGE)
            return

        settings = await queries.get_chat_settings(db, chat_id)
        if not enabled(settings, "greeting"):
            await message.reply(profile_service.build_greeting_disabled_text())
            return

        title, count = await chat_placeholders(
            bot,
            db,
            chat_id,
            fallback_title=message.chat.title or "",
        )
        # Предпросмотр — ровно текст владельца со спец-командами: бот не
        # дописывает «Привет, имя» от себя.
        values = richtext.placeholder_values(user, chat_title=title, member_count=count)
        personal = richtext.apply_placeholders(
            content(settings, "greeting"), values, user=user
        )
        try:
            await bot.send_message(user.id, profile_service.build_greeting_preview_note_text())
        except Exception as exc:  # noqa: BLE001 - личка может быть закрыта
            logger.info("Предпросмотр приветствия в личку не ушёл: %s", exc)
            await message.reply(profile_service.build_greeting_dm_failed_text())
            return
        if await richtext.send_content(bot, user.id, personal, html_fallback=False):
            await message.reply("👀 Отправила предпросмотр тебе в личку~")
        else:
            await message.reply(profile_service.build_greeting_dm_failed_text())
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("команде приветствия", exc)
        await error_handler.notify_user_softly(message)


# ---------------------------------------------------------------------------
# Кнопки меню настроек
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith(inline.CB_RULES))
async def on_rules_callbacks(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Обработать кнопки меню «Правила чата».

    Формат ``callback_data``: ``rules:{menu|toggle|edit|preview|drop_photo|clear}:{chat_id}``.
    """
    try:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        chat_id = _chat_id_from(parts[-1])
        await _dispatch(callback, db, bot, state, "rules", action, chat_id)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке правил", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data.startswith(inline.CB_GREETING))
async def on_greeting_callbacks(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Обработать кнопки меню «Приветствие новичков».

    Формат ``callback_data``: ``greeting:{menu|toggle|edit|preview|drop_photo|
    clear|profile|anonymous|check_duration|mute_menu|mute_toggle|
    mute_duration|mute_back|join_mute_menu|join_mute_toggle|
    join_mute_duration|join_mute_back|...}:{chat_id}``.
    """
    try:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        chat_id = _chat_id_from(parts[-1])
        await _dispatch(callback, db, bot, state, "greeting", action, chat_id)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке приветствия", exc)
        await error_handler.notify_user_softly(callback)


# ---------------------------------------------------------------------------
# Приём текста (и фото) от владельца
# ---------------------------------------------------------------------------
async def _owner_context(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> Optional[ChatInfo]:
    """Проверить владельца чата из состояния FSM (или сбросить состояние).

    :param message: сообщение владельца в личке.
    :param state: состояние FSM текущего диалога.
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    """
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


async def _save_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    kind: str,
) -> None:
    """Сохранить присланный владельцем текст/фото в настройках чата.

    Правила сохраняются вместе с сущностями (премиум-эмодзи) и фото. После
    приветствия владельцу предлагается шаг 2 — добавить инлайн-кнопки.

    :param message: сообщение с текстом и/или фото.
    :param state: состояние FSM.
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    :param kind: ``rules`` или ``greeting``.
    """
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return
        chat_id = chat.chat_id

        content_value = richtext.content_from_message(message)
        if not content_value.has_content:
            await message.answer(
                "Не вижу текста или фото~ Пришли текст, фото или и то и другое 🌸",
                reply_markup=inline.content_prompt_keyboard(chat_id, kind),
            )
            return

        settings = await _save_content(db, chat_id, kind, content_value)
        # Сохранили содержимое — сразу включаем блок, чтобы не жать тумблер.
        settings = await queries.update_chat_setting(db, chat_id, ENABLED_KEYS[kind], True)
        if kind == "greeting":
            # Шаг 2/2: предложить добавить инлайн-кнопки.
            await state.set_state(ContentStates.waiting_greeting_buttons)
            await state.update_data(chat_id=chat_id)
            await message.answer(
                profile_service.build_greeting_buttons_question_text(),
                reply_markup=inline.greeting_buttons_choice_keyboard(chat_id),
            )
            return

        await state.clear()
        await message.answer(
            profile_service.build_content_saved_text(kind),
            reply_markup=inline.content_saved_keyboard(chat_id, kind),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception(f"сохранении содержимого «{kind}»", exc)
        await error_handler.notify_user_softly(message)


@router.message(ContentStates.waiting_rules, F.chat.type == ChatType.PRIVATE)
async def on_rules_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять текст (и/или фото) правил от владельца."""
    await _save_input(message, state, db, bot, "rules")


@router.message(ContentStates.waiting_greeting, F.chat.type == ChatType.PRIVATE)
async def on_greeting_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять текст (и/или фото) приветствия от владельца."""
    await _save_input(message, state, db, bot, "greeting")


# ---------------------------------------------------------------------------
# Шаг 2: инлайн-кнопки приветствия и длительность превентивного мута
# ---------------------------------------------------------------------------
@router.message(ContentStates.waiting_greeting_buttons, F.chat.type == ChatType.PRIVATE)
async def on_greeting_buttons_choice(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Шаг 2/2: владелец прислал новый текст вместо ответа про кнопки.

    Это удобно: он может поправить приветствие прямо здесь, а вопрос про
    кнопки зададим снова.
    """
    await _save_input(message, state, db, bot, "greeting")


@router.message(ContentStates.waiting_button_text, F.chat.type == ChatType.PRIVATE)
async def on_button_text_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять название инлайн-кнопки и попросить ссылку."""
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return
        text = richtext.normalize_button_text(message.text or message.caption or "")
        if not text:
            await message.answer(
                "Не вижу названия~ Пришли текст кнопки (до "
                f"{config.MAX_BUTTON_TEXT_LENGTH} символов) 🌸",
                reply_markup=inline.greeting_button_prompt_keyboard(chat.chat_id),
            )
            return
        await state.update_data(button_text=text, chat_id=chat.chat_id)
        await state.set_state(ContentStates.waiting_button_url)
        await message.answer(
            profile_service.build_greeting_button_url_prompt_text(),
            reply_markup=inline.greeting_button_prompt_keyboard(chat.chat_id),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("вводе названия кнопки приветствия", exc)
        await error_handler.notify_user_softly(message)


@router.message(ContentStates.waiting_button_url, F.chat.type == ChatType.PRIVATE)
async def on_button_url_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять ссылку кнопки, сохранить её и спросить про следующую."""
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return

        url = richtext.normalize_button_url(message.text or message.caption or "")
        if url is None:
            await message.answer(
                profile_service.build_invalid_button_url_text(),
                reply_markup=inline.greeting_button_prompt_keyboard(chat.chat_id),
            )
            return

        data = await state.get_data()
        button_text = str(data.get("button_text") or "")
        if not button_text:
            # Состояние потерялось (например, после перезапуска) — просим заново.
            await state.set_state(ContentStates.waiting_button_text)
            await message.answer(
                profile_service.build_greeting_button_name_prompt_text(
                    1, config.MAX_GREETING_BUTTONS
                ),
                reply_markup=inline.greeting_button_prompt_keyboard(chat.chat_id),
            )
            return

        settings = await queries.get_chat_settings(db, chat.chat_id)
        buttons = richtext.append_saved_button(
            settings.get(GREETING_BUTTONS_KEY),
            button_text,
            url,
        )
        settings = await queries.update_chat_setting(
            db, chat.chat_id, GREETING_BUTTONS_KEY, buttons
        )
        count = len(buttons)
        logger.info(
            "Приветствие чата %s: добавлена кнопка «%s» (всего %s).",
            chat.chat_id,
            button_text,
            count,
        )

        if count >= config.MAX_GREETING_BUTTONS:
            await state.clear()
            await message.answer(
                f"✅ Кнопка добавлена!\n\n"
                f"Всего кнопок: [{count}/{config.MAX_GREETING_BUTTONS}]\n\n"
                "Достигнут максимум~ Готово! 💕",
                reply_markup=inline.greeting_keyboard(chat.chat_id, settings),
            )
            return

        await state.set_state(ContentStates.waiting_button_text)
        await state.update_data(button_text="", chat_id=chat.chat_id)
        await message.answer(
            profile_service.build_greeting_button_added_text(
                count, config.MAX_GREETING_BUTTONS
            ),
            reply_markup=inline.greeting_button_more_keyboard(chat.chat_id),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("вводе ссылки кнопки приветствия", exc)
        await error_handler.notify_user_softly(message)


@router.message(ContentStates.waiting_mute_duration, F.chat.type == ChatType.PRIVATE)
async def on_mute_duration_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять новую длительность превентивного мута."""
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return

        settings = await queries.get_chat_settings(db, chat.chat_id)
        seconds = time_parser.parse_time_token((message.text or "").strip())
        in_bounds = seconds is not None and (
            config.MARKED_MUTE_MIN_SECONDS <= seconds <= config.MARKED_MUTE_MAX_SECONDS
        )
        if not in_bounds:
            await message.answer(
                "Не поняла время~ Напиши, например: <b>30м</b>, <b>2ч</b> или <b>1д</b>.",
                reply_markup=inline.marked_mute_keyboard(chat.chat_id, settings),
            )
            return

        settings = await queries.update_chat_setting(
            db, chat.chat_id, MARKED_MUTE_DURATION_KEY, int(seconds)
        )
        await state.clear()
        logger.info(
            "Превентивный мут в чате %s: длительность %s секунд.",
            chat.chat_id,
            seconds,
        )
        await message.answer(
            profile_service.build_marked_mute_menu_text(chat, settings),
            reply_markup=inline.marked_mute_keyboard(chat.chat_id, settings),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("вводе длительности превентивного мута", exc)
        await error_handler.notify_user_softly(message)


@router.message(ContentStates.waiting_check_duration, F.chat.type == ChatType.PRIVATE)
async def on_check_duration_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять длительность проверочного мута (анонимное приветствие).

    Время принимается в любом формате :mod:`yamochan.services.time_parser`:
    ``30с``, ``1м``, ``15мин``, ``1ч``. Число без букв считается минутами.
    """
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return

        settings = await queries.get_chat_settings(db, chat.chat_id)
        seconds = time_parser.parse_time_token((message.text or "").strip())
        in_bounds = seconds is not None and (
            config.WELCOME_CHECK_MIN_SECONDS <= seconds <= config.WELCOME_CHECK_MAX_SECONDS
        )
        if not in_bounds:
            await message.answer(
                "Не поняла время~ Напиши, например: <b>30с</b>, <b>2м</b> или <b>1ч</b>.\n"
                f"От {time_parser.human_duration(config.WELCOME_CHECK_MIN_SECONDS)} "
                f"до {time_parser.human_duration(config.WELCOME_CHECK_MAX_SECONDS)}",
                reply_markup=inline.greeting_keyboard(chat.chat_id, settings),
            )
            return

        settings = await queries.update_chat_setting(
            db, chat.chat_id, WELCOME_CHECK_DURATION_KEY, int(seconds)
        )
        await state.clear()
        logger.info(
            "Анонимный привет чата %s: проверочный мут %s секунд.",
            chat.chat_id,
            seconds,
        )
        await message.answer(
            "✅ Время проверочного мута обновлено: "
            f"<b>{time_parser.format_duration(int(seconds))}</b>\n\n"
            + profile_service.build_greeting_menu_text(chat, settings),
            reply_markup=inline.greeting_keyboard(chat.chat_id, settings),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("вводе времени проверочного мута", exc)
        await error_handler.notify_user_softly(message)


@router.message(ContentStates.waiting_join_mute_duration, F.chat.type == ChatType.PRIVATE)
async def on_join_mute_duration_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять длительность мута при входе для всех новых участников.

    Время принимается в любом формате :mod:`yamochan.services.time_parser`
    (``30с``, ``5м``, ``1ч``, ``1ч30м``, ``2д``); число без букв считается
    минутами. Диапазон: от 30 секунд до 30 дней (ограничение Telegram).
    При некорректном вводе состояние сохраняется — бот просит время снова.
    """
    try:
        chat = await _owner_context(message, state, db, bot)
        if chat is None:
            return

        settings = await queries.get_chat_settings(db, chat.chat_id)
        seconds = time_parser.parse_time_token((message.text or "").strip())
        in_bounds = seconds is not None and (
            config.JOIN_MUTE_MIN_SECONDS <= seconds <= config.JOIN_MUTE_MAX_SECONDS
        )
        if not in_bounds:
            await message.answer(
                "Не поняла время~ Напиши, например: <b>30с</b>, <b>5м</b> или <b>1ч30м</b>.\n"
                f"От {time_parser.human_duration(config.JOIN_MUTE_MIN_SECONDS)} "
                f"до {time_parser.human_duration(config.JOIN_MUTE_MAX_SECONDS)}",
                reply_markup=inline.join_mute_keyboard(chat.chat_id, settings),
            )
            return

        settings = await queries.update_chat_setting(
            db, chat.chat_id, JOIN_MUTE_DURATION_KEY, int(seconds)
        )
        await state.clear()
        logger.info(
            "Мут при входе в чате %s: длительность %s секунд.",
            chat.chat_id,
            seconds,
        )
        await message.answer(
            "✅ Время мута при входе обновлено: "
            f"<b>{time_parser.human_duration(int(seconds))}</b>\n\n"
            + profile_service.build_join_mute_menu_text(chat, settings),
            reply_markup=inline.join_mute_keyboard(chat.chat_id, settings),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("вводе времени мута при входе", exc)
        await error_handler.notify_user_softly(message)

