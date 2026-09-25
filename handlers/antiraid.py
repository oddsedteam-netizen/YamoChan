"""Интерфейс антирейда и антиспама: кнопки, ввод порогов и уведомления.

Роутер регистрируется раньше остальных, но перехватывает только своё:
callback-префиксы ``antiraid:`` и ``shield:``, а также сообщения в состоянии
ожидания ввода порога. Остальные сценарии личных сообщений не затрагиваются.
"""

from __future__ import annotations

import logging
from typing import Final, Optional, Sequence

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import config
from ..database import queries
from ..database.db import Database
from ..database.models import ChatInfo
from ..keyboards import inline
from ..services import antiraid as antiraid_service
from ..services import permissions, profile as profile_service, time_parser
from ..utils import error_handler, html_utils, telegram

logger = logging.getLogger(__name__)

#: Роутер антирейда (кнопки меню и ввод порогов).
router: Final[Router] = Router(name="antiraid")

#: Сообщение, если владелец ввёл что-то непонятное.
BAD_INPUT_TEXT: Final[str] = (
    "Не поняла~ Пришли два числа: количество и время. 🤔\n"
    "Например: <code>5 5м</code> или <code>7 10с</code>"
)


class AntiraidStates(StatesGroup):
    """Ожидание нового порога от владельца чата."""

    waiting_threshold = State()
    waiting_spam_threshold = State()


async def _render(
    callback: CallbackQuery,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
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
                error_handler.log_exception("отправке экрана антирейда", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:  # noqa: BLE001 - экран не важнее работы бота
        error_handler.log_exception("обновлении экрана антирейда", exc)


async def _owner_chat(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> Optional[ChatInfo]:
    """Проверить права владельца и вернуть чат (иначе ``None``)."""
    user = callback.from_user
    if user is None:
        return None
    chat = await queries.get_chat(db, chat_id)
    if chat is None:
        await callback.answer("Этот чат мне ещё не знаком~ 🌸", show_alert=True)
        return None
    if not await permissions.is_chat_owner_of(bot, db, chat, user.id):
        await callback.answer(profile_service.build_no_access_to_antiraid_text(), show_alert=True)
        return None
    return chat


async def notify_raid(
    bot: Bot,
    db: Database,
    chat: ChatInfo,
    suspects: Sequence[int],
    timeframe: int,
) -> bool:
    """Отправить владельцу уведомление о рейде с кнопками решения.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat: информация о чате из базы.
    :param suspects: идентификаторы подозрительных аккаунтов.
    :param timeframe: за какое окно они зашли.
    :returns: удалось ли доставить уведомление.
    """
    if chat.owner_id is None:
        logger.warning("У чата %s не определён владелец — уведомление не отправлено.", chat.chat_id)
        return False
    try:
        await bot.send_message(
            chat_id=chat.owner_id,
            text=profile_service.build_raid_alert_text(chat, len(suspects), timeframe),
            reply_markup=inline.raid_alert_keyboard(chat.chat_id),
        )
        return True
    except TelegramForbiddenError:
        logger.info(
            "Владелец %s не начинал диалог с ботом — уведомление о рейде не доставлено.",
            chat.owner_id,
        )
    except TelegramAPIError as exc:
        logger.error("Не удалось уведомить владельца чата %s: %s", chat.chat_id, exc)
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка уведомления владельца", exc_info=True)
    return False


async def suspects_report(db: Database, user_ids: Sequence[int]) -> list[tuple[int, str, Optional[str]]]:
    """Собрать ``(user_id, имя, юзернейм)`` по идентификаторам подозрительных."""
    report: list[tuple[int, str, Optional[str]]] = []
    for user_id in user_ids:
        try:
            profile = await queries.get_user(db, user_id)
        except Exception:  # noqa: BLE001
            profile = None
        if profile is None:
            report.append((user_id, f"ID {user_id}", None))
        else:
            report.append((user_id, profile.display_name, profile.username))
    return report


def _chat_id_from(raw: Optional[str]) -> int:
    """Разобрать идентификатор чата из ``callback_data``."""
    cleaned = (raw or "").strip()
    return int(cleaned) if cleaned.lstrip("-").isdigit() else 0


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------
async def _show_menu(callback: CallbackQuery, db: Database, bot: Bot, chat_id: int) -> None:
    """Показать меню настроек антирейда."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await _render(
        callback,
        profile_service.build_antiraid_menu_text(chat, settings),
        inline.antiraid_keyboard(chat_id, settings),
    )


async def _toggle_antiraid(callback: CallbackQuery, db: Database, bot: Bot, chat_id: int) -> None:
    """Включить или выключить антирейд (оба ключа настроек — синхронно)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    new_value = not bool(settings.get("antiraid_enabled"))
    settings = await queries.update_chat_setting(db, chat_id, "antiraid_enabled", new_value)
    settings = await queries.update_chat_setting(db, chat_id, "antiraid", new_value)
    if not new_value:
        antiraid_service.antiraid_manager.forget_chat(chat_id)
    logger.info("Антирейд в чате %s переключён в %s.", chat_id, new_value)
    await _render(
        callback,
        profile_service.build_antiraid_menu_text(chat, settings),
        inline.antiraid_keyboard(chat_id, settings),
    )
    await callback.answer(f"Антирейд: {'включён ✅' if new_value else 'выключен ❌'}")


async def _set_spam_mode(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
    mode: str,
) -> None:
    """Переключить режим антиспама (с закрытием чата или без)."""
    if mode not in config.ANTISPAM_MODES:
        await callback.answer("Не поняла такой режим~ 🤔", show_alert=True)
        return
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.update_chat_setting(db, chat_id, "antispam_mode", mode)
    await _render(
        callback,
        profile_service.build_antiraid_menu_text(chat, settings),
        inline.antiraid_keyboard(chat_id, settings),
    )
    await callback.answer(f"Антиспам: {config.ANTISPAM_MODE_TITLES.get(mode, mode)} ✅")


async def _ask_threshold(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
    kind: str,
) -> None:
    """Попросить владельца прислать новый порог (через FSM)."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    settings = await queries.get_chat_settings(db, chat_id)
    await state.set_state(
        AntiraidStates.waiting_spam_threshold
        if kind == "spam"
        else AntiraidStates.waiting_threshold
    )
    await state.update_data(chat_id=chat_id, kind=kind)
    await _render(
        callback,
        profile_service.build_antiraid_prompt_text(chat, settings, kind),
        inline.antiraid_prompt_keyboard(chat_id),
    )
    await callback.answer()


async def _apply_lift(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
    answer: Optional[str] = None,
) -> None:
    """Снять защиту чата, сообщить об этом в чат и показать подтверждение."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    await antiraid_service.lift_protection(bot, db, chat_id)
    try:
        await bot.send_message(chat_id=chat_id, text=profile_service.build_chat_unlocked_text())
    except Exception as exc:  # noqa: BLE001 - чат мог стать недоступен
        logger.info("Не удалось сообщить чату %s о снятии защиты: %s", chat_id, exc)
    await _render(
        callback,
        answer or profile_service.build_protection_lifted_text(),
        inline.back_to_menu_keyboard(),
    )
    await callback.answer()


async def _confirm_raid(callback: CallbackQuery, db: Database, bot: Bot, chat_id: int) -> None:
    """Показать список подозрительных для подтверждённого рейда."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    suspects = antiraid_service.antiraid_manager.get_suspects(chat_id)
    if not suspects:
        suspects = await queries.get_raid_suspects(db, chat_id)
    report = await suspects_report(db, suspects)
    await _render(
        callback,
        profile_service.build_raid_confirmed_text(report),
        inline.raid_confirmed_keyboard(chat_id),
    )
    await callback.answer()


async def _keep_protection(callback: CallbackQuery, db: Database, bot: Bot, chat_id: int) -> None:
    """Оставить защиту включённой, но дать кнопку снятия."""
    chat = await _owner_chat(callback, db, bot, chat_id)
    if chat is None:
        return
    await _render(
        callback,
        profile_service.build_raid_kept_text(),
        inline.lift_protection_keyboard(chat_id),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Маршрутизация кнопок
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith(inline.CB_ANTIRAID))
async def on_antiraid_callbacks(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Обработать все кнопки антирейда."""
    try:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        chat_id = _chat_id_from(parts[-1])
        logger.info("Кнопка антирейда %s (чат %s)", callback.data, chat_id)

        if action == "menu":
            await _show_menu(callback, db, bot, chat_id)
            await callback.answer()
        elif action == "toggle":
            await _toggle_antiraid(callback, db, bot, chat_id)
        elif action == "spam" and len(parts) >= 4:
            await _set_spam_mode(callback, db, bot, chat_id, parts[2])
        elif action in {"threshold", "spam_threshold"}:
            await _ask_threshold(
                callback,
                db,
                bot,
                state,
                chat_id,
                "spam" if action == "spam_threshold" else "antiraid",
            )
        elif action == "lift":
            await _apply_lift(callback, db, bot, chat_id)
        elif action == "false_alarm":
            await _apply_lift(
                callback, db, bot, chat_id, answer=profile_service.build_false_alarm_text()
            )
        elif action == "confirm_raid":
            await _confirm_raid(callback, db, bot, chat_id)
        elif action == "keep":
            await _keep_protection(callback, db, bot, chat_id)
        else:
            await callback.answer("Не знаю такую кнопку~ 🤔")
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке антирейда", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data.startswith(inline.CB_SHIELD))
async def on_shield_callbacks(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Снять активную защиту кнопкой из главного меню."""
    try:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        chat_id = _chat_id_from(parts[-1])
        if action == "lift":
            await _apply_lift(callback, db, bot, chat_id)
        else:
            await callback.answer("Не знаю такую кнопку~ 🤔")
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке снятия защиты", exc)
        await error_handler.notify_user_softly(callback)


# ---------------------------------------------------------------------------
# Ввод порогов (FSM)
# ---------------------------------------------------------------------------
async def _save_threshold(
    message: Message,
    state: FSMContext,
    db: Database,
    kind: str,
) -> None:
    """Разобрать ввод владельца и сохранить новый порог."""
    data = await state.get_data()
    chat_id = int(data.get("chat_id") or 0)
    chat = await queries.get_chat(db, chat_id) if chat_id else None
    if chat is None:
        await state.clear()
        await message.answer(
            "Чат потерялся~ Открой настройки антирейда заново 🌸",
            reply_markup=inline.back_to_menu_keyboard(),
        )
        return

    parsed = antiraid_service.parse_threshold_input(message.text)
    if parsed is None:
        await message.answer(
            BAD_INPUT_TEXT,
            reply_markup=inline.antiraid_prompt_keyboard(chat_id),
        )
        return

    amount, seconds = parsed
    if kind == "spam":
        await queries.update_chat_setting(db, chat_id, "spam_msg_threshold", amount)
        settings = await queries.update_chat_setting(db, chat_id, "spam_msg_timeframe", seconds)
        summary = f"📊 Порог антиспама: {amount} сооб за {time_parser.human_duration(seconds)}"
        minimum_amount, minimum_seconds = config.SPAM_MIN_THRESHOLD, config.SPAM_MIN_TIMEFRAME
    else:
        await queries.update_chat_setting(db, chat_id, "antiraid_threshold", amount)
        settings = await queries.update_chat_setting(db, chat_id, "antiraid_timeframe", seconds)
        summary = f"👥 Порог антирейда: {amount} человек за {time_parser.human_duration(seconds)}"
        minimum_amount = config.ANTIRAID_MIN_THRESHOLD
        minimum_seconds = config.ANTIRAID_MIN_TIMEFRAME

    # Значения сохраняются как ввёл владелец; о слишком низких просто предупреждаем.
    if amount < minimum_amount or seconds < minimum_seconds:
        logger.warning(
            "В чате %s очень низкий порог %s: %s за %s сек (рекомендуется от %s за %s сек).",
            chat_id,
            kind,
            amount,
            seconds,
            minimum_amount,
            minimum_seconds,
        )

    logger.info("Порог %s в чате %s: %s за %s сек.", kind, chat_id, amount, seconds)
    await state.clear()
    await message.answer(
        f"✅ Готово!\n{summary}",
        reply_markup=inline.antiraid_keyboard(chat_id, settings),
    )


async def _handle_threshold_message(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    kind: str,
) -> None:
    """Общая проверка прав и запуск сохранения порога."""
    user = message.from_user
    if user is None:
        return
    if (message.text or "").startswith("/"):
        await state.clear()
        await message.answer("Хорошо, отменила настройку~ Открой меню заново: /start 🌸")
        return

    data = await state.get_data()
    chat = await queries.get_chat(db, int(data.get("chat_id") or 0))
    if chat is None or not await permissions.is_chat_owner_of(bot, db, chat, user.id):
        await state.clear()
        await message.answer(profile_service.build_no_access_to_antiraid_text())
        return
    await _save_threshold(message, state, db, kind)


@router.message(AntiraidStates.waiting_threshold, F.chat.type == ChatType.PRIVATE)
async def on_threshold_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять новое значение порога антирейда."""
    try:
        await _handle_threshold_message(message, state, db, bot, "antiraid")
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("вводе порога антирейда", exc)
        await error_handler.notify_user_softly(message)


@router.message(AntiraidStates.waiting_spam_threshold, F.chat.type == ChatType.PRIVATE)
async def on_spam_threshold_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять новое значение порога антиспама."""
    try:
        await _handle_threshold_message(message, state, db, bot, "spam")
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("вводе порога антиспама", exc)
        await error_handler.notify_user_softly(message)
