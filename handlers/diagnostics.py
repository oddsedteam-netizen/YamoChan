"""Команда диагностики: почему бот не может работать в чате.

Главная боль — «бот молчит, в логах пусто». Эта команда отвечает на вопрос
прямо в чате: показывает права бота, состояние настроек приветствия и, что
важнее всего, результат **реальной** тестовой отправки сообщения. Если Telegram
отказал — в отчёте будет точный текст ошибки, а не предположение.

Запуск: ``.диагностика`` / ``/диагностика`` / ``!диагностика`` (админы чата).
"""

from __future__ import annotations

import logging
from typing import Final, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import BaseFilter
from aiogram.types import Message

import config
from database import queries
from database.db import Database
from services import permissions, profile as profile_service, richtext
from utils import command_filter, error_handler

logger = logging.getLogger(__name__)

#: Роутер диагностики.
router: Final[Router] = Router(name="diagnostics")

#: Групповые типы чатов.
GROUP_CHAT_TYPES: Final[set[str]] = {ChatType.GROUP, ChatType.SUPERGROUP}


class DiagnosticsCommandFilter(BaseFilter):
    """Фильтр: сообщение — команда диагностики (``.диагностика``, ``/диагностика``)."""

    async def __call__(self, message: Message) -> bool:
        """Распознать команду диагностики с любым префиксом и без него.

        :param message: входящее сообщение.
        :returns: ``True``, если это команда диагностики.
        """
        command = command_filter.split_command(message.text)
        if command is None:
            return False
        name, _ = command
        return name == config.COMMAND_DIAGNOSTICS


async def collect_bot_rights(bot: Bot, chat_id: int) -> dict[str, Optional[bool]]:
    """Собрать права бота в чате для отчёта диагностики.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    :returns: словарь ``название права → есть ли``; ``None``, если неизвестно.
    """
    rights: dict[str, Optional[bool]] = {
        "admin": None,
        "delete": None,
        "restrict": None,
        "read": None,
    }
    try:
        me = await bot.get_me()
    except Exception as exc:  # noqa: BLE001 - диагностика должна отчёт отдать
        logger.warning("Диагностика: не удалось получить данные бота: %s", exc)
        return rights

    member = await permissions.get_chat_member(bot, chat_id, me.id)
    if member is None:
        logger.warning(
            "Диагностика чата %s: getChatMember вернул пусто — бот не участник чата?",
            chat_id,
        )
        return rights

    status = member.status
    if status == ChatMemberStatus.CREATOR:
        # У создателя все права есть всегда, отдельные флаги Telegram не шлёт.
        return {key: True for key in rights}
    if status != ChatMemberStatus.ADMINISTRATOR:
        return {key: False for key in rights}

    rights["admin"] = True
    rights["delete"] = bool(getattr(member, "can_delete_messages", False))
    rights["restrict"] = bool(getattr(member, "can_restrict_members", False))
    rights["read"] = bool(getattr(member, "can_read_messages", True))
    return rights


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), DiagnosticsCommandFilter())
async def cmd_diagnostics(message: Message, db: Database, bot: Bot) -> None:
    """Показать в чате полный отчёт: права бота, настройки и проверка отправки.

    Команда для владельца и админов чата: она отвечает на главный вопрос «почему
    не приходит приветствие» — показывает права, состояние настройки и, главное,
    результат реальной тестовой отправки сообщения в чат.

    :param message: сообщение с командой.
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    """
    user = message.from_user
    chat_id = message.chat.id
    if user is None:
        return

    try:
        if not await permissions.is_chat_administrator(bot, chat_id, user.id):
            await message.reply(profile_service.build_diagnostics_not_admin_text())
            return

        settings = await queries.get_chat_settings(db, chat_id)
        rights = await collect_bot_rights(bot, chat_id)
        greeting_content = richtext.content_from_settings(settings, "greeting")

        # Главная часть проверки — реальная отправка в чат: она покажет
        # точную причину отказа Telegram, а не догадку.
        test_sent = False
        test_error = ""
        try:
            await bot.send_message(chat_id, "🩺 Проверка связи YamoChan…")
            test_sent = True
        except Exception as exc:  # noqa: BLE001 - причину покажем в отчёте
            test_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Диагностика чата %s: тестовая отправка не удалась — %s",
                chat_id,
                test_error,
            )

        report = profile_service.build_diagnostics_report(
            chat_title=message.chat.title or "",
            rights=rights,
            greeting_enabled=bool(
                settings.get("greeting_enabled", settings.get("welcome_enabled"))
            ),
            greeting_has_text=greeting_content.has_text,
            greeting_has_photo=greeting_content.has_photo,
            greeting_buttons=len(
                richtext.parse_saved_buttons(settings.get("greeting_buttons"))
            ),
            test_sent=test_sent,
            test_error=test_error,
        )
        await message.reply(report)
        logger.info(
            "Диагностика чата %s выполнена: отправка=%s, приветствие=%s.",
            chat_id,
            "ок" if test_sent else "отказ",
            "включено" if settings.get("greeting_enabled") else "выключено",
        )
    except Exception as exc:  # noqa: BLE001 - команда не должна ронять бота
        error_handler.log_exception("команде диагностики", exc)
        await error_handler.notify_user_softly(message)