"""Обработчики личных сообщений бота: ``/start``, профиль и «Возможности».

В личке YamoChan здоровается, создаёт профиль пользователя, принудительно
связывает его с чатами (как владельца или как участника) и показывает главное
меню. Никаких команд модерации здесь нет — они доступны только в группах.
"""

from __future__ import annotations

import logging
from typing import Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

import config
from ..database import queries
from ..database.db import Database
from ..database.models import ChatInfo
from ..keyboards import inline
from ..services import antiraid as antiraid_service
from ..services import permissions, profile as profile_service
from ..utils import error_handler

logger = logging.getLogger(__name__)

#: Роутер личных сообщений.
router: Final[Router] = Router(name="start")

#: Текст-подсказка для прочих сообщений в личке.
PRIVATE_HINT: Final[str] = (
    "Я понимаю только команды в чате~ 🌸\n"
    "Открой меню командой /start или добавь меня в свой чат!"
)


async def _show_main_menu(message: Message, db: Database) -> None:
    """Показать главное меню и заодно освежить профиль пользователя.

    Если в чатах владельца активна защита антирейда, в меню появляются
    кнопки её снятия.

    :param message: сообщение пользователя в личке.
    :param db: соединение с базой данных.
    """
    guarded: list[ChatInfo] = []
    if message.from_user is not None:
        try:
            await queries.ensure_user(
                db,
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
            )
        except Exception as exc:  # noqa: BLE001 - профиль не критичен для ответа
            error_handler.log_exception("создании профиля из /start", exc)
        try:
            guarded = await antiraid_service.active_protection_chats(db, message.from_user.id)
        except Exception as exc:  # noqa: BLE001 - меню важнее списка защиты
            error_handler.log_exception("поиске чатов под защитой", exc)

    await message.answer(
        profile_service.build_main_menu_text(len(guarded)),
        reply_markup=inline.main_menu_keyboard(guarded),
    )


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, db: Database, bot: Bot) -> None:
    """Обработать ``/start`` в личных сообщениях.

    Перед показом меню пользователь принудительно связывается со всеми
    известными чатами: владелец, который добавил бота и молчит в группе,
    всё равно увидит свой чат в «Моих чатах».

    :param message: сообщение с командой.
    :param db: соединение с базой данных (прокидывается middleware).
    :param bot: экземпляр бота.
    """
    user = message.from_user
    logger.info("Пользователь %s запустил бота в личке.", user.id if user else "?")
    if user is not None:
        try:
            linked = await permissions.sync_user_chats(
                bot,
                db,
                user.id,
                user.username,
                user.first_name,
            )
            if linked:
                logger.info("Пользователь %s связан с %s чатами: %s", user.id, len(linked), linked)
        except Exception as exc:  # noqa: BLE001 - меню важнее синхронизации
            error_handler.log_exception("синхронизации чатов пользователя", exc)
    try:
        await _show_main_menu(message, db)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("отправке главного меню", exc)
        await error_handler.notify_user_softly(message)


@router.message(Command("help"), F.chat.type == ChatType.PRIVATE)
async def cmd_help(message: Message, db: Database) -> None:
    """Показать справку о возможностях бота (``/help`` в личке)."""
    try:
        await message.answer(
            profile_service.build_capabilities_text(),
            reply_markup=inline.back_to_menu_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("отправке справки", exc)
        await error_handler.notify_user_softly(message)


@router.message(F.chat.type == ChatType.PRIVATE)
async def private_fallback(message: Message) -> None:
    """Мягко подсказать, что делать, если текст в личке непонятен."""
    try:
        await message.answer(
            PRIVATE_HINT,
            reply_markup=inline.back_to_menu_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("ответе в личке", exc)