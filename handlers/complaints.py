"""Жалобы пользователей: кнопка «📩 Оставить жалобу» и пошаговый диалог в ЛС.

Поток (только личные сообщения):
    1. ``complaint:open`` — выбор категории жалобы;
    2. ``complaint:reason:{ключ}`` — пользователь пишет описание;
    3. необязательное фото (``complaint:skip_photo`` — пропустить);
    4. превью и подтверждение (``complaint:confirm``) — жалоба сохраняется,
       владелец бота получает карточку с кнопками решения.

Отменить диалог можно кнопкой ``complaint:cancel`` или командой ``/cancel``.
Все тексты живут в :mod:`yamochan.services.complaints`.
"""

from __future__ import annotations

import logging
from typing import Final, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
from ..database import queries
from ..database.db import Database
from ..database.models import Complaint
from ..keyboards import inline
from ..services import admin as admin_service
from ..services import complaints as complaints_service
from ..services import profile as profile_service, richtext
from ..utils import error_handler, html_utils, telegram

logger = logging.getLogger(__name__)

#: Роутер жалоб.
router: Final[Router] = Router(name="complaints")

#: Текст, если описание слишком короткое.
SHORT_DESCRIPTION_TEXT: Final[str] = (
    "Слишком коротко~ Опиши ситуацию подробнее (минимум "
    f"{config.COMPLAINT_MIN_LENGTH} символов) 🌸"
)

#: Текст, если на шаге фото пришло что-то непонятное.
PHOTO_HINT_TEXT: Final[str] = "Пришли фото одним сообщением или нажми «⏭ Пропустить» 🌸"

#: Текст, если пользователь пишет вместо нажатия «Отправить».
CONFIRM_HINT_TEXT: Final[str] = "Нажми «✅ Отправить» или «❌ Отмена» 🌸"

#: Команды и слова, которыми пользователь отменяет диалог.
CANCEL_WORDS: Final[frozenset[str]] = frozenset({"/cancel", "отмена", "cancel"})


class ComplaintStates(StatesGroup):
    """Шаги заполнения жалобы пользователем."""

    waiting_description = State()
    waiting_photo = State()
    waiting_confirm = State()


class ComplaintAdminStates(StatesGroup):
    """Шаги разбора жалобы владельцем бота."""

    waiting_decision = State()
    waiting_reply = State()


#: Ответ администрации не длиннее этого значения.
RESPONSE_LIMIT: Final[int] = 1000

#: Слова отмены диалога.
CANCEL_WORDS: Final[frozenset[str]] = frozenset({"/cancel", "отмена", "cancel"})

#: Слова «ответить стандартно» вместо текста.
SKIP_WORDS: Final[frozenset[str]] = frozenset({"/skip", "skip", "пропустить", "-"})


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
                error_handler.log_exception("отправке экрана жалобы", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:  # noqa: BLE001 - экран не важнее работы бота
        error_handler.log_exception("обновлении экрана жалобы", exc)


async def _show_main_menu(message: Message) -> None:
    """Вернуть пользователя в главное меню бота."""
    try:
        await message.answer(
            profile_service.build_main_menu_text(),
            reply_markup=inline.main_menu_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("возврате в меню после жалобы", exc)


def _is_cancel(text: Optional[str]) -> bool:
    """Является ли сообщение командой отмены."""
    return (text or "").strip().lower() in CANCEL_WORDS


# ---------------------------------------------------------------------------
# Инлайн-кнопки жалобы
# ---------------------------------------------------------------------------
@router.callback_query(F.data == f"{inline.CB_COMPLAINT}open")
async def complaint_open(callback: CallbackQuery, state: FSMContext) -> None:
    """Открыть экран выбора категории жалобы."""
    try:
        await state.clear()
        await _render(
            callback,
            complaints_service.build_intro_text(),
            inline.complaint_reasons_keyboard(),
        )
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("открытии экрана жалобы", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data.startswith(f"{inline.CB_COMPLAINT}reason:"))
async def complaint_reason(callback: CallbackQuery, state: FSMContext) -> None:
    """Запомнить категорию и попросить описание."""
    try:
        key = (callback.data or "").split(":", 2)[-1]
        if key not in config.COMPLAINT_REASONS:
            await callback.answer("Не поняла категорию~ 🤔", show_alert=True)
            return
        await state.clear()
        await state.update_data(reason=key)
        await state.set_state(ComplaintStates.waiting_description)
        await _render(
            callback,
            complaints_service.build_description_prompt_text(key),
            inline.complaint_prompt_keyboard(),
        )
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("выборе категории жалобы", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data == f"{inline.CB_COMPLAINT}cancel")
async def complaint_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменить заполнение жалобы."""
    try:
        await state.clear()
        await _render(
            callback,
            complaints_service.build_cancelled_text(),
            inline.back_to_menu_keyboard(),
        )
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("отмене жалобы", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data == f"{inline.CB_COMPLAINT}skip_photo")
async def complaint_skip_photo(callback: CallbackQuery, state: FSMContext) -> None:
    """Пропустить шаг с фото и показать превью."""
    try:
        data = await state.get_data()
        if not data.get("reason") or not data.get("description"):
            await callback.answer("Жалоба устарела~ Открой меню заново", show_alert=True)
            await state.clear()
            return
        await state.update_data(photo=None)
        await state.set_state(ComplaintStates.waiting_confirm)
        await _render(
            callback,
            complaints_service.build_preview_text(
                str(data.get("reason")),
                str(data.get("description") or ""),
                False,
            ),
            inline.complaint_confirm_keyboard(),
        )
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("пропуске фото жалобы", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data == f"{inline.CB_COMPLAINT}confirm")
async def complaint_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Сохранить жалобу, уведомить владельца бота и подтвердить автору."""
    try:
        user = callback.from_user
        data = await state.get_data()
        reason = str(data.get("reason") or "")
        description = str(data.get("description") or "").strip()
        if user is None or not reason or not description:
            await callback.answer("Жалоба устарела~ Открой меню заново", show_alert=True)
            await state.clear()
            return

        complaint_id = await complaints_service.submit_complaint(
            db,
            user_id=user.id,
            reason=reason,
            description=description,
            username=user.username,
            first_name=user.first_name,
            photo_file_id=data.get("photo"),
        )
        await state.clear()

        complaint = await queries.get_complaint(db, complaint_id)
        if complaint is not None:
            stats = await queries.count_complaints_by_status(db, complaint.user_id)
            await complaints_service.notify_owner(
                bot,
                db,
                complaint,
                reply_markup=inline.admin_complaint_card_keyboard(
                    complaint_id,
                    is_open=True,
                    user_id=complaint.user_id,
                    has_history=int(stats.get("total", 0)) > 0,
                ),
            )

        await _render(
            callback,
            complaints_service.build_saved_text(complaint_id),
            inline.back_to_menu_keyboard(),
        )
        await callback.answer("Жалоба отправлена~ 💕")
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("отправке жалобы", exc)
        await error_handler.notify_user_softly(callback)


# ---------------------------------------------------------------------------
# Ввод текста и фото
# ---------------------------------------------------------------------------
@router.message(ComplaintStates.waiting_description, F.chat.type == ChatType.PRIVATE)
async def complaint_description(message: Message, state: FSMContext) -> None:
    """Принять описание жалобы и перейти к шагу фото."""
    try:
        if _is_cancel(message.text):
            await state.clear()
            await message.answer(complaints_service.build_cancelled_text())
            await _show_main_menu(message)
            return

        text = (message.text or message.caption or "").strip()
        if len(text) < config.COMPLAINT_MIN_LENGTH:
            await message.answer(
                SHORT_DESCRIPTION_TEXT,
                reply_markup=inline.complaint_prompt_keyboard(),
            )
            return
        if len(text) > config.COMPLAINT_MAX_LENGTH:
            text = text[: config.COMPLAINT_MAX_LENGTH]

        await state.update_data(description=text, photo=None)
        await state.set_state(ComplaintStates.waiting_photo)
        await message.answer(
            complaints_service.build_photo_prompt_text(),
            reply_markup=inline.complaint_photo_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("приёме описания жалобы", exc)
        await error_handler.notify_user_softly(message)


@router.message(ComplaintStates.waiting_photo, F.chat.type == ChatType.PRIVATE)
async def complaint_photo(message: Message, state: FSMContext) -> None:
    """Принять фото жалобы (или подсказать, как пропустить шаг)."""
    try:
        if _is_cancel(message.text):
            await state.clear()
            await message.answer(complaints_service.build_cancelled_text())
            await _show_main_menu(message)
            return

        photo = richtext.largest_photo(message.photo or [])
        if message.text and message.text.strip().lower() in {"пропустить", "skip"}:
            photo = None
        elif not photo:
            await message.answer(
                PHOTO_HINT_TEXT,
                reply_markup=inline.complaint_photo_keyboard(),
            )
            return

        data = await state.get_data()
        await state.update_data(photo=photo)
        await state.set_state(ComplaintStates.waiting_confirm)
        await message.answer(
            complaints_service.build_preview_text(
                str(data.get("reason") or ""),
                str(data.get("description") or ""),
                bool(photo),
            ),
            reply_markup=inline.complaint_confirm_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("приёме фото жалобы", exc)
        await error_handler.notify_user_softly(message)


@router.message(ComplaintStates.waiting_confirm, F.chat.type == ChatType.PRIVATE)
async def complaint_waiting_confirm(message: Message, state: FSMContext) -> None:
    """Подсказать, что жалобу нужно подтвердить кнопкой."""
    try:
        if _is_cancel(message.text):
            await state.clear()
            await message.answer(complaints_service.build_cancelled_text())
            await _show_main_menu(message)
            return
        await message.answer(
            CONFIRM_HINT_TEXT,
            reply_markup=inline.complaint_confirm_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("подсказке о подтверждении жалобы", exc)


# ---------------------------------------------------------------------------
# Разбор жалоб владельцем бота (callback_data ``adm:complaint:…``)
# ---------------------------------------------------------------------------
#: Пометки решения, которые добавляются в карточку после разбора.
DECISION_MARKS: Final[dict[str, str]] = {
    config.COMPLAINT_STATUS_ACCEPTED: "✅ ПРИНЯТА",
    config.COMPLAINT_STATUS_REJECTED: "❌ ОТКЛОНЕНА",
}


def _int(raw: str, default: int = 0) -> int:
    """Разобрать число из ``callback_data`` (мусор → значение по умолчанию)."""
    cleaned = (raw or "").strip().lstrip("#")
    return int(cleaned) if cleaned.isdigit() else default


def _is_owner(user_id: Optional[int]) -> bool:
    """Является ли пользователь владельцем бота."""
    return admin_service.is_owner(user_id)


def _card_keyboard(complaint: Complaint, *, has_history: bool) -> InlineKeyboardMarkup:
    """Клавиатура карточки жалобы для владельца бота."""
    return inline.admin_complaint_card_keyboard(
        int(complaint.id or 0),
        is_open=complaint.is_open,
        user_id=complaint.user_id,
        has_history=has_history,
    )


async def _render_card(
    callback: CallbackQuery,
    db: Database,
    complaint: Complaint,
) -> None:
    """Показать карточку жалобы (со статистикой автора и кнопками решения)."""
    stats = await queries.count_complaints_by_status(db, complaint.user_id)
    await _render(
        callback,
        complaints_service.build_card_text(complaint),
        _card_keyboard(complaint, has_history=int(stats.get("total", 0)) > 0),
    )


async def _show_complaint_card(callback: CallbackQuery, db: Database, complaint_id: int) -> None:
    """Открыть карточку жалобы по её номеру."""
    complaint = await queries.get_complaint(db, complaint_id)
    if complaint is None:
        await callback.answer("Жалоба не найдена~ 🤔", show_alert=True)
        return
    await _render_card(callback, db, complaint)


async def _show_user_complaints(callback: CallbackQuery, db: Database, user_id: int) -> None:
    """Показать все жалобы конкретного пользователя."""
    profile = await queries.get_user(db, user_id)
    name = profile.display_name if profile is not None else f"ID {user_id}"
    stats = await queries.count_complaints_by_status(db, user_id)
    complaints = await queries.get_complaints(db, user_id=user_id, limit=20)
    items = [(int(item.id or 0), f"📩 Жалоба #{item.id}") for item in complaints]
    await _render(
        callback,
        complaints_service.build_user_list_text(name, user_id, stats, complaints),
        inline.admin_user_complaints_keyboard(user_id, items),
    )


async def _ask_decision(
    callback: CallbackQuery,
    state: FSMContext,
    complaint_id: int,
    *,
    accepted: bool,
) -> None:
    """Спросить у админа ответ по жалобе (текст или стандартный ответ)."""
    status = (
        config.COMPLAINT_STATUS_ACCEPTED if accepted else config.COMPLAINT_STATUS_REJECTED
    )
    message = callback.message if isinstance(callback.message, Message) else None
    await state.clear()
    await state.update_data(
        complaint_id=complaint_id,
        status=status,
        card_chat_id=message.chat.id if message is not None else None,
        card_message_id=message.message_id if message is not None else None,
    )
    await state.set_state(ComplaintAdminStates.waiting_decision)
    await _render(
        callback,
        complaints_service.build_decision_question_text(complaint_id, accepted=accepted),
        inline.admin_complaint_decision_keyboard(complaint_id, accepted=accepted),
    )


async def _apply_standard(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    complaint_id: int,
    kind: str,
) -> None:
    """Закрыть жалобу стандартным ответом.

    :param complaint_id: номер жалобы.
    :param kind: ``accept`` или ``reject``.
    """
    accepted = kind != "reject"
    response = (
        config.COMPLAINT_STANDARD_ACCEPT if accepted else config.COMPLAINT_STANDARD_REJECT
    )
    status = (
        config.COMPLAINT_STATUS_ACCEPTED if accepted else config.COMPLAINT_STATUS_REJECTED
    )
    await state.clear()
    await _finish_decision(
        db,
        bot,
        complaint_id,
        status=status,
        response=response,
        actor_id=callback.from_user.id if callback.from_user else None,
    )
    complaint = await queries.get_complaint(db, complaint_id)
    if complaint is None:
        await callback.answer("Жалоба не найдена~ 🤔", show_alert=True)
        return
    await _render_card(callback, db, complaint)
    await callback.answer("Стандартный ответ отправлен~ 💕")


async def _finish_decision(
    db: Database,
    bot: Bot,
    complaint_id: int,
    *,
    status: str,
    response: Optional[str],
    actor_id: Optional[int],
    card_chat_id: Optional[int] = None,
    card_message_id: Optional[int] = None,
) -> Optional[Complaint]:
    """Закрыть жалобу, уведомить автора и обновить карточку в ЛС админа.

    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    :param complaint_id: номер жалобы.
    :param status: ``accepted`` или ``rejected``.
    :param response: ответ администрации автору.
    :param actor_id: кто принял решение.
    :param card_chat_id: чат карточки админа (для обновления).
    :param card_message_id: сообщение карточки админа (для обновления).
    """
    complaint = await complaints_service.close_complaint(
        bot, db, complaint_id, status, response=response, closed_by=actor_id
    )
    if complaint is None:
        return None

    if card_chat_id and card_message_id:
        stats = await queries.count_complaints_by_status(db, complaint.user_id)
        try:
            await bot.edit_message_text(
                chat_id=int(card_chat_id),
                message_id=int(card_message_id),
                text=complaints_service.build_card_text(
                    complaint, decision=DECISION_MARKS.get(status, "")
                ),
                reply_markup=_card_keyboard(
                    complaint, has_history=int(stats.get("total", 0)) > 0
                ),
            )
        except TelegramBadRequest as exc:
            logger.debug("Карточка жалобы #%s не обновилась: %s", complaint_id, exc)
        except Exception:  # noqa: BLE001
            logger.error("Неожиданная ошибка обновления карточки жалобы", exc_info=True)
    return complaint


async def _apply_reopen(
    callback: CallbackQuery,
    db: Database,
    complaint_id: int,
    actor_id: int,
) -> None:
    """Открыть закрытую жалобу заново."""
    updated = await queries.close_complaint(
        db,
        complaint_id,
        config.COMPLAINT_STATUS_OPEN,
        response=None,
        closed_by=actor_id,
    )
    if not updated:
        await callback.answer("Жалоба не найдена~ 🤔", show_alert=True)
        return
    await queries.log_admin_action(db, "reopen_complaint", complaint_id, f"#{complaint_id}")
    await _show_complaint_card(callback, db, complaint_id)


async def _ask_reply(
    callback: CallbackQuery,
    state: FSMContext,
    complaint_id: int,
) -> None:
    """Спросить у админа произвольное сообщение автору жалобы."""
    await state.clear()
    await state.update_data(complaint_id=complaint_id)
    await state.set_state(ComplaintAdminStates.waiting_reply)
    await _render(
        callback,
        complaints_service.build_reply_question_text(complaint_id),
        inline.admin_complaint_response_keyboard(complaint_id),
    )


@router.callback_query(F.data.startswith(f"{inline.CB_ADM}complaint:"))
async def on_admin_complaint_callbacks(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Кнопки разбора жалоб в ЛС владельца бота (``adm:complaint:…``)."""
    user = callback.from_user
    if user is None or not _is_owner(user.id):
        await callback.answer()
        return
    try:
        parts = (callback.data or "").split(":")
        verb = parts[2] if len(parts) > 2 else ""
        if verb.isdigit():
            await _show_complaint_card(callback, db, int(verb))
        elif verb == "accept":
            await _ask_decision(
                callback, state, _int(parts[3] if len(parts) > 3 else ""), accepted=True
            )
        elif verb == "reject":
            await _ask_decision(
                callback, state, _int(parts[3] if len(parts) > 3 else ""), accepted=False
            )
        elif verb == "standard":
            await _apply_standard(
                callback,
                state,
                db,
                bot,
                _int(parts[3] if len(parts) > 3 else ""),
                parts[4] if len(parts) > 4 else "accept",
            )
        elif verb == "reply":
            await _ask_reply(callback, state, _int(parts[3] if len(parts) > 3 else ""))
        elif verb == "list":
            await _show_user_complaints(callback, db, _int(parts[3] if len(parts) > 3 else ""))
        elif verb == "reopen":
            await _apply_reopen(callback, db, _int(parts[3] if len(parts) > 3 else ""), user.id)
        else:
            await callback.answer("Не поняла кнопку~ 🤔", show_alert=True)
            return
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке жалобы", exc)
        await error_handler.notify_user_softly(callback)


@router.message(ComplaintAdminStates.waiting_decision, F.chat.type == ChatType.PRIVATE)
async def admin_decision_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Принять ответ администрации по жалобе и закрыть её."""
    try:
        user = message.from_user
        if user is None or not _is_owner(user.id):
            return
        data = await state.get_data()
        complaint_id = int(data.get("complaint_id") or 0)
        status = str(data.get("status") or config.COMPLAINT_STATUS_REJECTED)
        raw = (message.text or "").strip()

        if raw.lower() in CANCEL_WORDS:
            await state.clear()
            await message.answer("Хорошо, отменила~ Жалоба осталась открытой 🌸")
            return

        response = None if raw.lower() in SKIP_WORDS else raw[:RESPONSE_LIMIT]
        complaint = await _finish_decision(
            db,
            bot,
            complaint_id,
            status=status,
            response=response,
            actor_id=user.id,
            card_chat_id=data.get("card_chat_id"),
            card_message_id=data.get("card_message_id"),
        )
        await state.clear()
        if complaint is None:
            await message.answer("Жалоба не найдена~ 🤔")
            return

        stats = await queries.count_complaints_by_status(db, complaint.user_id)
        await message.answer(
            f"{DECISION_MARKS.get(status, '✅ Готово')} — уведомление автору отправлено~",
        )
        await message.answer(
            complaints_service.build_card_text(complaint),
            reply_markup=_card_keyboard(
                complaint, has_history=int(stats.get("total", 0)) > 0
            ),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("ответе на жалобу", exc)
        await error_handler.notify_user_softly(message)


@router.message(ComplaintAdminStates.waiting_reply, F.chat.type == ChatType.PRIVATE)
async def admin_reply_input(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Отправить автору жалобы сообщение от администрации."""
    try:
        user = message.from_user
        if user is None or not _is_owner(user.id):
            return
        data = await state.get_data()
        complaint_id = int(data.get("complaint_id") or 0)
        raw = (message.text or "").strip()

        if raw.lower() in CANCEL_WORDS:
            await state.clear()
            await message.answer("Хорошо, отменила~ 🌸")
            return
        if not raw:
            await message.answer("Не вижу текста~ Напиши сообщение для юзера 🌸")
            return

        delivered = await complaints_service.send_reply_to_author(
            bot, db, complaint_id, raw[:RESPONSE_LIMIT]
        )
        await state.clear()
        await message.answer(
            "✅ Сообщение отправлено~" if delivered else "Не удалось доставить сообщение 💔"
        )
        complaint = await queries.get_complaint(db, complaint_id)
        if complaint is not None:
            await _render_card_message(message, db, complaint)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("ответе юзеру по жалобе", exc)
        await error_handler.notify_user_softly(message)


async def _render_card_message(message: Message, db: Database, complaint: Complaint) -> None:
    """Отправить карточку жалобы новым сообщением (для потоков из ЛС админа)."""
    try:
        stats = await queries.count_complaints_by_status(db, complaint.user_id)
        await message.answer(
            complaints_service.build_card_text(complaint),
            reply_markup=_card_keyboard(complaint, has_history=int(stats.get("total", 0)) > 0),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("отправке карточки жалобы", exc)