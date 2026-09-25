"""FAQ: гайд по возможностям бота для владельца чата.

Раздел живёт в личке: вход — кнопка «❓ FAQ» в главном меню, дальше список
разделов и тексты-подсказки. Тексты хранятся в
:mod:`yamochan.utils.faq_texts`, клавиатуры — в
:mod:`yamochan.keyboards.inline`.

Маршрутизация ``callback_data``:
    * ``faq:main`` — список разделов (туда же ведёт «🔙 К разделам FAQ»);
    * ``faq:section:{id}`` — конкретный раздел гайда.

Возврат в главное меню бота — кнопка ``back:main`` (её обрабатывает
:mod:`yamochan.handlers.callbacks`). Любая ошибка логируется, а
пользователь получает мягкое сообщение и бот продолжает работу.
"""

from __future__ import annotations

import logging
from typing import Final, Optional

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from keyboards import inline
from utils import error_handler, faq_texts, html_utils, telegram

logger = logging.getLogger(__name__)

#: Роутер FAQ.
router: Final[Router] = Router(name="faq")


async def _render(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    """Показать экран FAQ вместо текущего сообщения.

    Если сообщение отредактировать нельзя (устарело, недоступно или текст
    не изменился), бот отправляет новое сообщение или обновляет клавиатуру.
    Текст заранее чинится :func:`yamochan.utils.html_utils.safe_html_text`.

    :param callback: нажатие на инлайн-кнопку.
    :param text: текст нового экрана.
    :param markup: клавиатура нового экрана.
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
                error_handler.log_exception("отправке экрана FAQ", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:  # noqa: BLE001 - экран не важнее работы бота
        error_handler.log_exception("обновлении экрана FAQ", exc)


@router.callback_query(F.data == f"{inline.CB_FAQ}main")
async def faq_main(callback: CallbackQuery) -> None:
    """Показать главный экран FAQ со списком разделов."""
    try:
        await _render(
            callback,
            faq_texts.build_main_text(),
            inline.faq_main_keyboard(),
        )
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("показе главного экрана FAQ", exc)
        await error_handler.notify_user_softly(callback)


@router.callback_query(F.data.startswith(f"{inline.CB_FAQ}section:"))
async def faq_section(callback: CallbackQuery) -> None:
    """Показать выбранный раздел FAQ.

    Формат ``callback_data``: ``faq:section:{идентификатор}``.
    """
    try:
        parts = (callback.data or "").split(":", 2)
        section_id = parts[2] if len(parts) > 2 else ""
        text = faq_texts.FAQ_TEXTS.get(section_id)
        if text is None:
            logger.info("Неизвестный раздел FAQ: %r", callback.data)
            await callback.answer("Раздел не найден~ 🤔", show_alert=True)
            return
        await _render(callback, text, inline.faq_section_keyboard())
        await callback.answer()
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("показе раздела FAQ", exc)
        await error_handler.notify_user_softly(callback)
