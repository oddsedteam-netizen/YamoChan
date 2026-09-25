"""Система жалоб: пользователь жалуется, владелец бота разбирает жалобу.

Модуль собирает тексты обоих сценариев:

    * пользовательский — кнопка «📩 Оставить жалобу» в главном меню:
      категория → описание → фото → предпросмотр → подтверждение;
    * админский — раздел «Жалобы» со статистикой, списки по статусам,
      карточка жалобы, принятие/отклонение (с текстом или стандартным
      ответом), ответ автору и список всех жалоб пользователя.

Здесь же живут действия: :func:`submit_complaint` сохраняет жалобу,
:func:`notify_owner` отправляет владельцу подробное уведомление (с фото,
репутацией, чатами и счётчиком прошлых жалоб), :func:`close_complaint`
закрывает жалобу и пишет автору решение, :func:`send_reply_to_author` —
произвольный ответ администрации.

Клавиатуры живут в :mod:`yamochan.keyboards.inline`, маршрутизация кнопок —
в :mod:`yamochan.handlers.complaints` (жалобы и их разбор) и
:mod:`yamochan.handlers.admin_panel` (раздел «Жалобы» в панели).
"""

from __future__ import annotations

import logging
from typing import Final, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup

import config
from ..database import queries
from ..database.db import Database
from ..database.models import Complaint, escape_text
from . import admin as admin_service, profile as profile_service

logger = logging.getLogger(__name__)

#: Заголовок карточки жалобы для владельца бота.
COMPLAINT_CARD_TITLE: Final[str] = "📩 Новая жалоба"


def reason_title(key: str) -> str:
    """Человекочитаемое название категории жалобы."""
    return config.COMPLAINT_REASONS.get(key, "❓ Другое")


def status_title(status: str) -> str:
    """Человекочитаемое название статуса жалобы."""
    return config.COMPLAINT_STATUS_TITLES.get(status, status)


def status_icon(status: str) -> str:
    """Короткий значок статуса для списков: ``⏳`` / ``✅`` / ``❌``."""
    return config.COMPLAINT_STATUS_ICONS.get(status, "❓")


def preview_of(description: str, length: int = config.COMPLAINT_PREVIEW_LENGTH) -> str:
    """Обрезать описание жалобы до нужной длины (пробелы нормализуются).

    :param description: полный текст жалобы.
    :param length: сколько символов оставить.
    """
    single_line = " ".join((description or "").split())
    if len(single_line) <= length:
        return single_line
    return single_line[:length] + "…"


# ---------------------------------------------------------------------------
# Пользовательский сценарий
# ---------------------------------------------------------------------------
def build_intro_text() -> str:
    """Шаг 1 — текст экрана «Подача жалобы» (выбор категории)."""
    return "📩 Подача жалобы\n\nВыбери категорию жалобы:"


def build_description_prompt_text(reason: str) -> str:
    """Шаг 2 — запрос описания проблемы.

    :param reason: ключ выбранной категории.
    """
    return (
        f"📩 Жалоба — {reason_title(reason)}\n\n"
        "Опиши подробно суть проблемы~\n"
        "Напиши текстом в одном сообщении.\n\n"
        "Для отмены отправь /cancel"
    )


def build_photo_prompt_text() -> str:
    """Шаг 3 — предложение прикрепить фото или скриншот."""
    return (
        "📸 Хочешь прикрепить фото/скриншот?\n\n"
        "Отправь фото или нажми «Пропустить»"
    )


def build_preview_text(reason: str, description: str, has_photo: bool) -> str:
    """Шаг 4 — предпросмотр жалобы перед отправкой.

    :param reason: ключ категории.
    :param description: текст жалобы.
    :param has_photo: приложено ли фото.
    """
    return (
        "📩 Предпросмотр жалобы\n\n"
        f"📋 Категория: {reason_title(reason)}\n"
        f"📝 Описание: {preview_of(description)}\n"
        f"📸 Фото: {'✅ прикреплено' if has_photo else '❌ нет'}\n\n"
        "Отправить жалобу?"
    )


def build_saved_text(complaint_id: int) -> str:
    """Подтверждение отправки жалобы автору.

    :param complaint_id: номер созданной жалобы.
    """
    return (
        f"✅ Жалоба #{complaint_id} отправлена!\n"
        "Мы рассмотрим её в ближайшее время~ 💕\n\n"
        "Ты получишь уведомление когда жалоба будет обработана."
    )


def build_cancelled_text() -> str:
    """Текст отмены заполнения жалобы."""
    return "Хорошо, жалоба отменена~ 🌸"


# ---------------------------------------------------------------------------
# Админский сценарий: раздел, списки и карточки
# ---------------------------------------------------------------------------
def build_section_text(stats: dict[str, int]) -> str:
    """Текст раздела «Жалобы» со статистикой по статусам.

    :param stats: результат :func:`queries.count_complaints_by_status`.
    """
    return "\n".join(
        [
            "📩 Жалобы",
            "",
            "📊 Статистика:",
            f"  Всего: {stats.get('total', 0)}",
            f"  ⏳ Открытых: {stats.get(config.COMPLAINT_STATUS_OPEN, 0)}",
            f"  ✅ Принятых: {stats.get(config.COMPLAINT_STATUS_ACCEPTED, 0)}",
            f"  ❌ Отклонённых: {stats.get(config.COMPLAINT_STATUS_REJECTED, 0)}",
        ]
    )


def build_list_text(
    title: str,
    complaints: Sequence[Complaint],
    page: int,
    pages: int,
) -> str:
    """Список жалоб короткими карточками (открытые/принятые/отклонённые).

    :param title: заголовок списка (например «⏳ Открытые жалобы»).
    :param complaints: жалобы текущей страницы (свежие сверху).
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    lines = [title, ""]
    if not complaints:
        lines.append("Здесь пусто~ ✨")
        return "\n".join(lines)

    for complaint in complaints:
        lines.extend(
            [
                f"#{complaint.id} | {admin_service.short_moment(complaint.created_at)}",
                f"👤 {escape_text(complaint.display_name)} "
                f"(ID: <code>{complaint.user_id}</code>)",
                f"📋 {reason_title(complaint.reason)}: "
                f"{escape_text(preview_of(complaint.description, config.COMPLAINT_LIST_PREVIEW_LENGTH))}",
                "",
            ]
        )
    if pages > 1:
        lines.append(f"Страница {page}/{pages}")
    return "\n".join(lines)


def build_user_list_text(
    name: str,
    user_id: int,
    stats: dict[str, int],
    complaints: Sequence[Complaint],
) -> str:
    """Список всех жалоб одного пользователя.

    :param name: имя автора жалоб.
    :param user_id: идентификатор автора.
    :param stats: результат :func:`queries.count_complaints_by_status`.
    :param complaints: жалобы автора (свежие сверху).
    """
    lines = [
        f"📩 Жалобы пользователя {escape_text(name)} (ID: <code>{user_id}</code>)",
        "",
        f"Всего: {stats.get('total', 0)} | "
        f"Открытых: {stats.get(config.COMPLAINT_STATUS_OPEN, 0)} | "
        f"Принятых: {stats.get(config.COMPLAINT_STATUS_ACCEPTED, 0)} | "
        f"Отклонённых: {stats.get(config.COMPLAINT_STATUS_REJECTED, 0)}",
        "",
    ]
    if not complaints:
        lines.append("Жалоб пока нет~ ✨")
        return "\n".join(lines)
    for complaint in complaints:
        lines.append(
            f"#{complaint.id} | {admin_service.short_moment(complaint.created_at)} | "
            f"{reason_title(complaint.reason)} | {status_icon(complaint.status)}"
        )
    return "\n".join(lines)


def build_card_text(complaint: Complaint, *, decision: str = "") -> str:
    """Текст карточки жалобы для владельца бота.

    :param complaint: жалоба из базы.
    :param decision: пометка решения (``✅ ПРИНЯТА`` / ``❌ ОТКЛОНЕНА``),
        добавляется в конец текста при обновлении сообщения админа.
    """
    lines = [
        f"{COMPLAINT_CARD_TITLE} #{complaint.id}",
        "",
        f"👤 Автор: {escape_text(complaint.display_name)} "
        f"(ID: <code>{complaint.user_id}</code>)",
        f"🔗 Username: "
        f"{'@' + escape_text(complaint.user_username) if complaint.user_username else 'скрыт'}",
        f"📌 Категория: {reason_title(complaint.reason)}",
        f"🕐 Отправлена: {admin_service.format_moment(complaint.created_at)}",
        f"🔖 Статус: {status_title(complaint.status)}",
        "",
        "📝 Суть:",
        escape_text(complaint.description),
    ]
    if complaint.has_photo:
        lines.extend(["", "📸 Фото приложено выше."])
    if complaint.admin_response:
        lines.extend(["", f"💬 Ответ админа: {escape_text(complaint.admin_response)}"])
    if complaint.closed_at is not None:
        lines.append(f"✅ Закрыта: {admin_service.format_moment(complaint.closed_at)}")
    if decision:
        lines.extend(["", decision])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Уведомление владельцу и ответы автору
# ---------------------------------------------------------------------------
def build_owner_notification_text(
    complaint: Complaint,
    reputation: int,
    chat_titles: Sequence[str],
    stats: dict[str, int],
) -> str:
    """Текст уведомления владельцу бота о новой жалобе.

    :param complaint: жалоба из базы.
    :param reputation: репутация автора.
    :param chat_titles: названия чатов, где состоит автор.
    :param stats: результат :func:`queries.count_complaints_by_status`.
    """
    handle = (
        f"@{escape_text(complaint.user_username)}" if complaint.user_username else "скрыт"
    )
    lines = [
        f"📩 НОВАЯ ЖАЛОБА #{complaint.id}",
        "",
        f"👤 От: {escape_text(complaint.display_name)} ({handle})",
        f"🆔 ID: <code>{complaint.user_id}</code>",
        f"📊 Репутация: {reputation} {profile_service.reputation_scale(reputation)}",
        "",
        "💬 Состоит в чатах:",
    ]
    if chat_titles:
        lines.extend([f"  • {escape_text(title)}" for title in chat_titles[:15]])
        if len(chat_titles) > 15:
            lines.append(f"  • …и ещё {len(chat_titles) - 15}")
    else:
        lines.append("  • данных нет~")

    lines.extend(
        [
            "",
            f"📋 Категория: {reason_title(complaint.reason)}",
            "📝 Описание:",
            escape_text(complaint.description),
            "",
            f"📸 Фото: {'прикреплено к сообщению' if complaint.has_photo else 'нет'}",
            "",
            f"📩 Предыдущие жалобы: {stats.get('total', 0)} "
            f"(принято: {stats.get(config.COMPLAINT_STATUS_ACCEPTED, 0)}, "
            f"отклонено: {stats.get(config.COMPLAINT_STATUS_REJECTED, 0)})",
        ]
    )
    return "\n".join(lines)


def build_decision_question_text(complaint_id: int, *, accepted: bool) -> str:
    """Текст вопроса админу после нажатия «Принять»/«Отклонить».

    :param complaint_id: номер жалобы.
    :param accepted: ``True`` — принятие, ``False`` — отклонение.
    """
    if accepted:
        return (
            f"✅ Принятие жалобы #{complaint_id}\n\n"
            "Напиши ответ пользователю (что было сделано):\n"
            "Или нажми «Стандартный ответ»"
        )
    return (
        f"❌ Отклонение жалобы #{complaint_id}\n\n"
        "Напиши причину отклонения:\n"
        "Или нажми «Стандартный ответ»"
    )


def build_reply_question_text(complaint_id: int) -> str:
    """Текст вопроса админу для произвольного ответа по жалобе."""
    return (
        f"💬 Ответ по жалобе #{complaint_id}\n\n"
        "Напиши сообщение — я отправлю его юзеру\n"
        "от своего имени.\n\n"
        "Для отмены отправь /cancel"
    )


def build_accepted_notice_text(complaint: Complaint) -> str:
    """Уведомление автору о принятии жалобы."""
    response = complaint.admin_response or config.COMPLAINT_STANDARD_ACCEPT
    return (
        f"✅ Ваша жалоба #{complaint.id} принята!\n\n"
        f"📋 Категория: {reason_title(complaint.reason)}\n"
        f"💬 Ответ администрации:\n{escape_text(response)}\n\n"
        "Спасибо за обращение~ 💕"
    )


def build_rejected_notice_text(complaint: Complaint) -> str:
    """Уведомление автору об отклонении жалобы."""
    response = complaint.admin_response or config.COMPLAINT_STANDARD_REJECT
    return (
        f"❌ Ваша жалоба #{complaint.id} отклонена.\n\n"
        f"📋 Категория: {reason_title(complaint.reason)}\n"
        f"💬 Ответ администрации:\n{escape_text(response)}\n\n"
        "Если проблема повторяется — подайте новую жалобу\n"
        "с дополнительными доказательствами."
    )


def build_reply_notice_text(complaint_id: int, text: str) -> str:
    """Сообщение автору от администрации по жалобе."""
    return (
        "💬 Сообщение от администрации YamoChan\n\n"
        f"По вашей жалобе #{complaint_id}:\n{escape_text(text)}"
    )


# ---------------------------------------------------------------------------
# Действия
# ---------------------------------------------------------------------------
async def submit_complaint(
    db: Database,
    *,
    user_id: int,
    reason: str,
    description: str,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    photo_file_id: Optional[str] = None,
) -> int:
    """Сохранить жалобу пользователя и вернуть её номер."""
    return await queries.create_complaint(
        db,
        user_id,
        reason,
        description,
        username=username,
        first_name=first_name,
        photo_file_id=photo_file_id,
    )


async def send_card(
    bot: Bot,
    complaint: Complaint,
    chat_id: int,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    title: Optional[str] = None,
) -> bool:
    """Отправить карточку жалобы в чат (с фото, если оно приложено).

    :param bot: экземпляр бота.
    :param complaint: жалоба.
    :param chat_id: кому отправляем (владелец бота или автор).
    :param reply_markup: клавиатура карточки.
    :param title: свой заголовок вместо стандартного.
    :returns: ``True``, если сообщение доставлено.
    """
    text = build_card_text(complaint)
    if title:
        text = f"{title}\n\n{text}"
    return await send_rich_text(
        bot, chat_id, text, photo=complaint.photo_file_id, reply_markup=reply_markup
    )


async def send_rich_text(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    photo: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """Отправить текст (и фото) в чат, разбивая слишком длинную подпись.

    :param bot: экземпляр бота.
    :param chat_id: получатель.
    :param text: текст сообщения.
    :param photo: ``file_id`` фото (если есть).
    :param reply_markup: клавиатура (ставится на главное сообщение).
    :returns: ``True``, если основное сообщение доставлено.
    """
    try:
        if photo:
            short = len(text) <= config.PHOTO_CAPTION_LIMIT
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=text if short else None,
                reply_markup=reply_markup if short else None,
            )
            if not short:
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
            return True
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return True
    except TelegramAPIError as exc:
        logger.info("Не удалось отправить сообщение в %s: %s", chat_id, exc)
    except Exception:  # noqa: BLE001 - отправка не должна ронять бота
        logger.error("Неожиданная ошибка отправки жалобы", exc_info=True)
    return False


async def notify_owner(
    bot: Bot,
    db: Database,
    complaint: Complaint,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """Уведомить владельца бота о новой жалобе.

    В уведомлении: автор (имя, юзернейм), репутация со шкалой, список чатов,
    категория, полное описание, фото и счётчик прошлых жалоб.

    Если владелец сам является автором жалобы, уведомление не отправляется.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param complaint: новая жалоба.
    :param reply_markup: кнопки решения по жалобе.
    """
    owner_id = int(config.BOT_OWNER_ID or 0)
    if owner_id <= 0 or owner_id == int(complaint.user_id):
        return False

    profile = await queries.get_user(db, complaint.user_id)
    reputation = int(profile.reputation) if profile is not None else 0
    stats = await queries.count_complaints_by_status(db, complaint.user_id)
    chat_titles: list[str] = []
    try:
        pairs = await queries.get_user_chats(db, complaint.user_id)
        chat_titles = [chat.display_title for chat, _ in pairs]
    except Exception:  # noqa: BLE001 - список чатов не критичен
        logger.error("Не удалось получить чаты автора жалобы", exc_info=True)

    text = build_owner_notification_text(complaint, reputation, chat_titles, stats)
    return await send_rich_text(
        bot,
        owner_id,
        text,
        photo=complaint.photo_file_id,
        reply_markup=reply_markup,
    )


async def notify_author_decision(bot: Bot, complaint: Complaint) -> bool:
    """Сообщить автору решение по жалобе (принятие или отклонение).

    :param bot: экземпляр бота.
    :param complaint: жалоба со статусом ``accepted`` или ``rejected``.
    """
    if complaint.status == config.COMPLAINT_STATUS_ACCEPTED:
        text = build_accepted_notice_text(complaint)
    elif complaint.status == config.COMPLAINT_STATUS_REJECTED:
        text = build_rejected_notice_text(complaint)
    else:
        return False
    try:
        await bot.send_message(chat_id=complaint.user_id, text=text)
        return True
    except TelegramAPIError as exc:
        logger.info("Автор жалобы #%s недоступен: %s", complaint.id, exc)
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка уведомления автора жалобы", exc_info=True)
    return False


async def send_reply_to_author(
    bot: Bot,
    db: Database,
    complaint_id: int,
    text: str,
) -> bool:
    """Отправить автору произвольный ответ администрации по жалобе.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param complaint_id: номер жалобы.
    :param text: сообщение админа.
    """
    complaint = await queries.get_complaint(db, complaint_id)
    if complaint is None:
        return False
    delivered = await send_rich_text(
        bot, complaint.user_id, build_reply_notice_text(complaint_id, text)
    )
    await queries.log_admin_action(
        db,
        "reply_complaint",
        complaint_id,
        f"#{complaint_id} ({'доставлено' if delivered else 'не доставлено'})",
    )
    return delivered


async def close_complaint(
    bot: Bot,
    db: Database,
    complaint_id: int,
    status: str,
    *,
    response: Optional[str] = None,
    closed_by: Optional[int] = None,
) -> Optional[Complaint]:
    """Закрыть жалобу, записать журнал и уведомить её автора.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param complaint_id: номер жалобы.
    :param status: новый статус (``accepted`` / ``rejected`` / ``open``).
    :param response: ответ администрации автору.
    :param closed_by: кто закрыл жалобу.
    :returns: обновлённая жалоба или ``None``, если её не было.
    """
    updated = await queries.close_complaint(
        db,
        complaint_id,
        status,
        response=response,
        closed_by=closed_by,
    )
    if not updated:
        return None

    complaint = await queries.get_complaint(db, complaint_id)
    if complaint is None:
        return None

    action = {
        config.COMPLAINT_STATUS_ACCEPTED: "accept_complaint",
        config.COMPLAINT_STATUS_REJECTED: "reject_complaint",
    }.get(complaint.status)
    if action:
        await queries.log_admin_action(
            db,
            action,
            complaint_id,
            f"#{complaint_id} от {complaint.display_name}",
        )

    if complaint.status in {
        config.COMPLAINT_STATUS_ACCEPTED,
        config.COMPLAINT_STATUS_REJECTED,
    }:
        await notify_author_decision(bot, complaint)
    return complaint