"""Логика наказаний: бан, мут, варн, авто-бан после трёх варнов.

Модуль не знает ничего про Telegram-хендлеры: он получает объект
:class:`aiogram.Bot`, базу данных и параметры наказания, а возвращает
описание результата. Это позволяет переиспользовать логику и в командах,
и в настройках антирейда.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions

import config
from database import queries
from database.db import Database
from database.models import Punishment, utcnow
from services import time_parser

logger = logging.getLogger(__name__)

#: Полностью «открытые» права участника — используются при размуте/снятии бана.
FULL_PERMISSIONS: ChatPermissions = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
    can_manage_topics=False,
)

#: Права «в муте»: сообщения и медиа запрещены, чтение остаётся возможным.
MUTED_PERMISSIONS: ChatPermissions = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)


@dataclass(slots=True)
class PunishmentResult:
    """Результат применения наказания.

    :param success: удалось ли выполнить действие в Telegram и БД.
    :param message: готовый текст ответа для чата.
    :param punishment: созданная запись о наказании (если была).
    :param details: дополнительные детали (например, авто-бан после варнов).
    """

    success: bool
    message: str
    punishment: Optional[Punishment] = None
    details: dict[str, object] = field(default_factory=dict)


def format_target_name(full_name: Optional[str], user_id: int) -> str:
    """Собрать отображаемое имя цели для текста ответа."""
    return full_name or f"ID {user_id}"


def _reason_line(reason: Optional[str]) -> str:
    """Строка с причиной наказания."""
    return f"📝 Причина: {reason}" if reason else "📝 Причина: не указана"


async def _apply_ban(
    bot: Bot,
    chat_id: int,
    user_id: int,
    until: Optional[datetime],
) -> None:
    """Вызвать ``banChatMember`` с указанием срока (0 — бессрочно)."""
    until_date = int(until.timestamp()) if until is not None else 0
    await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until_date)


async def _apply_unban(bot: Bot, chat_id: int, user_id: int) -> None:
    """Вызвать ``unbanChatMember``."""
    await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)


async def restrict_member(
    bot: Bot,
    chat_id: int,
    user_id: int,
    until: Optional[datetime],
    permissions: Optional[ChatPermissions] = None,
) -> None:
    """Ограничить участника (мут) заданными правами."""
    until_date = int(until.timestamp()) if until is not None else 0
    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=permissions or MUTED_PERMISSIONS,
        until_date=until_date,
    )


async def unrestrict_member(bot: Bot, chat_id: int, user_id: int) -> None:
    """Вернуть участнику полные права (снятие мута)."""
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=FULL_PERMISSIONS,
            use_independent_chat_permissions=True,
        )
    except TelegramAPIError as exc:
        # Если Telegram не поддерживает независимые права — пробуем без флага.
        logger.info("Повторная попытка снятия ограничений без независимых прав: %s", exc)
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=FULL_PERMISSIONS,
        )


async def ban_user(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    target_name: str,
    seconds: Optional[int],
    reason: Optional[str],
    issued_by: Optional[int],
) -> PunishmentResult:
    """Забанить пользователя и зафиксировать наказание в базе.

    :param seconds: срок в секундах или ``None`` для бессрочного бана.
    :param target_name: имя цели для текста ответа.
    :returns: :class:`PunishmentResult` с текстом сообщения в чат.
    """
    normalized = time_parser.normalize_duration(seconds)
    until = utcnow() + timedelta(seconds=normalized) if normalized else None
    duration_label = time_parser.format_duration(normalized)
    try:
        await _apply_ban(bot, chat_id, user_id, until)
    except TelegramAPIError as exc:
        logger.error("Не удалось забанить %s в %s: %s", user_id, chat_id, exc)
        return PunishmentResult(
            success=False,
            message="Не получилось забанить~ Проверь мои права админа 🙏",
        )
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка бана %s в %s", user_id, chat_id, exc_info=True)
        return PunishmentResult(
            success=False,
            message="Ой, что-то пошло не так~ 💔 Попробуй ещё раз.",
        )

    punishment = await queries.add_punishment(
        db,
        chat_id,
        user_id,
        config.TYPE_BAN,
        reason,
        duration_label,
        normalized,
        issued_by,
    )
    await queries.set_user_banned(db, chat_id, user_id, True, until)
    await queries.deactivate_punishments(db, chat_id, user_id, config.TYPE_MUTE)
    await queries.set_user_muted(db, chat_id, user_id, False)
    await queries.add_reputation(db, user_id, config.REPUTATION_BAN)
    await queries.add_ban_mark(db, user_id, chat_id)

    message = (
        f"✨ Пользователь <b>{target_name}</b> получил бан~ 🔨\n"
        f"⏱ Срок: {duration_label}\n"
        f"{_reason_line(reason)}"
    )
    return PunishmentResult(success=True, message=message, punishment=punishment)


async def unban_user(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    target_name: str,
) -> PunishmentResult:
    """Снять бан с пользователя и обновить базу данных."""
    try:
        await _apply_unban(bot, chat_id, user_id)
    except TelegramAPIError as exc:
        logger.error("Не удалось разбанить %s в %s: %s", user_id, chat_id, exc)
        return PunishmentResult(
            success=False,
            message="У меня не получилось снять бан~ Проверь мои права 🙏",
        )
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка разбана %s в %s", user_id, chat_id, exc_info=True)
        return PunishmentResult(
            success=False,
            message="Ой, что-то пошло не так~ 💔 Попробуй ещё раз.",
        )

    await queries.deactivate_punishments(db, chat_id, user_id, config.TYPE_BAN)
    await queries.set_user_banned(db, chat_id, user_id, False)
    await queries.remove_ban_mark(db, user_id, chat_id)
    await queries.add_reputation(db, user_id, config.REPUTATION_UNBAN)

    message = (
        f"💖 Пользователь <b>{target_name}</b> разбанен~ Добро пожаловать обратно!"
    )
    return PunishmentResult(success=True, message=message)


async def mute_user(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    target_name: str,
    seconds: Optional[int],
    reason: Optional[str],
    issued_by: Optional[int],
) -> PunishmentResult:
    """Замутить пользователя и записать наказание в базу."""
    normalized = time_parser.normalize_duration(seconds)
    until = utcnow() + timedelta(seconds=normalized) if normalized else None
    duration_label = time_parser.format_duration(normalized)
    try:
        await restrict_member(bot, chat_id, user_id, until)
    except TelegramAPIError as exc:
        logger.error("Не удалось замутить %s в %s: %s", user_id, chat_id, exc)
        return PunishmentResult(
            success=False,
            message="Не получилось замутить~ Проверь мои права админа 🙏",
        )
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка мута %s в %s", user_id, chat_id, exc_info=True)
        return PunishmentResult(
            success=False,
            message="Ой, что-то пошло не так~ 💔 Попробуй ещё раз.",
        )

    punishment = await queries.add_punishment(
        db,
        chat_id,
        user_id,
        config.TYPE_MUTE,
        reason,
        duration_label,
        normalized,
        issued_by,
    )
    await queries.set_user_muted(db, chat_id, user_id, True, until)
    await queries.add_reputation(db, user_id, config.REPUTATION_MUTE)

    message = (
        f"🤫 Пользователь <b>{target_name}</b> получил мут~\n"
        f"⏱ Срок: {duration_label}\n"
        f"{_reason_line(reason)}"
    )
    return PunishmentResult(success=True, message=message, punishment=punishment)


async def unmute_user(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    target_name: str,
    *,
    silent: bool = False,
) -> PunishmentResult:
    """Снять мут с пользователя и восстановить его права.

    :param silent: если ``True``, репутация не начисляется (используется
        при автоматическом истечении срока).
    """
    try:
        await unrestrict_member(bot, chat_id, user_id)
    except TelegramAPIError as exc:
        logger.error("Не удалось снять мут с %s в %s: %s", user_id, chat_id, exc)
        return PunishmentResult(
            success=False,
            message="У меня не получилось снять мут~ Проверь мои права 🙏",
        )
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка снятия мута %s в %s", user_id, chat_id, exc_info=True)
        return PunishmentResult(
            success=False,
            message="Ой, что-то пошло не так~ 💔 Попробуй ещё раз.",
        )

    await queries.deactivate_punishments(db, chat_id, user_id, config.TYPE_MUTE)
    await queries.set_user_muted(db, chat_id, user_id, False)
    if not silent:
        await queries.add_reputation(db, user_id, config.REPUTATION_UNMUTE)

    message = f"🎉 Пользователь <b>{target_name}</b> снова может говорить~"
    return PunishmentResult(success=True, message=message)


async def warn_user(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    target_name: str,
    reason: Optional[str],
    issued_by: Optional[int],
) -> PunishmentResult:
    """Выдать предупреждение, а на третьем — автоматически забанить."""
    warns_count = await queries.add_warn(db, chat_id, user_id, reason, issued_by)
    await queries.add_reputation(db, user_id, config.REPUTATION_WARN)

    if warns_count < config.MAX_WARNS:
        message = (
            f"⚠️ Пользователь <b>{target_name}</b> получил предупреждение "
            f"[{warns_count}/{config.MAX_WARNS}]~\n"
            f"{_reason_line(reason)}"
        )
        return PunishmentResult(
            success=True,
            message=message,
            details={"warns": warns_count, "auto_banned": False},
        )

    # Третий варн — автоматический бессрочный бан.
    ban_result = await ban_user(
        bot,
        db,
        chat_id,
        user_id,
        target_name,
        None,
        config.AUTO_BAN_REASON,
        issued_by,
    )
    if not ban_result.success:
        message = (
            f"⚠️ Пользователь <b>{target_name}</b> получил "
            f"[{config.MAX_WARNS}/{config.MAX_WARNS}] предупреждений~\n"
            f"{_reason_line(reason)}\n"
            f"{ban_result.message}"
        )
        return PunishmentResult(success=False, message=message, details={"auto_banned": False})

    await queries.clear_warns(db, chat_id, user_id)
    message = (
        f"⚠️ Пользователь <b>{target_name}</b> получил предупреждение "
        f"[{config.MAX_WARNS}/{config.MAX_WARNS}]~\n"
        f"{_reason_line(reason)}\n\n"
        f"🔨 {config.AUTO_BAN_REASON}"
    )
    return PunishmentResult(
        success=True,
        message=message,
        punishment=ban_result.punishment,
        details={"warns": warns_count, "auto_banned": True},
    )


async def unwarn_user(
    db: Database,
    chat_id: int,
    user_id: int,
    target_name: str,
) -> PunishmentResult:
    """Снять последнее предупреждение пользователя."""
    removed = await queries.pop_last_warn(db, chat_id, user_id)
    if removed is None:
        return PunishmentResult(
            success=False,
            message=f"У <b>{target_name}</b> и так нет предупреждений~ ✨",
        )
    current = await queries.get_warns_count(db, chat_id, user_id)
    await queries.add_reputation(db, user_id, config.REPUTATION_UNWARN)
    message = (
        f"💝 С пользователя <b>{target_name}</b> снят 1 варн~ "
        f"Текущие варны: [{current}/{config.MAX_WARNS}]"
    )
    return PunishmentResult(success=True, message=message, details={"warns": current})


async def kick_user(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    target_name: str,
    reason: Optional[str],
    issued_by: Optional[int],
) -> PunishmentResult:
    """Кикнуть пользователя: бан и немедленный разбан."""
    try:
        await _apply_ban(bot, chat_id, user_id, None)
        await _apply_unban(bot, chat_id, user_id)
    except TelegramAPIError as exc:
        logger.error("Не удалось кикнуть %s из %s: %s", user_id, chat_id, exc)
        return PunishmentResult(
            success=False,
            message="Не получилось кикнуть~ Проверь мои права админа 🙏",
        )
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка кика %s из %s", user_id, chat_id, exc_info=True)
        return PunishmentResult(
            success=False,
            message="Ой, что-то пошло не так~ 💔 Попробуй ещё раз.",
        )

    await queries.add_punishment(
        db,
        chat_id,
        user_id,
        config.TYPE_KICK,
        reason,
        None,
        None,
        issued_by,
    )
    await queries.set_member_presence(db, chat_id, user_id, False)
    await queries.set_user_banned(db, chat_id, user_id, False)

    message = f"👋 Пользователь <b>{target_name}</b> был кикнут из чата~"
    return PunishmentResult(success=True, message=message)


async def release_expired(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    punishments: list[Punishment],
) -> list[str]:
    """Снять истёкшие наказания в Telegram и обновить локальное состояние.

    :returns: список типов снятых наказаний (``ban``/``mute``).
    """
    released: list[str] = []
    kinds = {punishment.type for punishment in punishments}
    if config.TYPE_BAN in kinds:
        try:
            await _apply_unban(bot, chat_id, user_id)
            released.append(config.TYPE_BAN)
        except TelegramAPIError as exc:
            logger.error("Не удалось автоматически разбанить %s: %s", user_id, exc)
        except Exception:  # noqa: BLE001
            logger.error("Ошибка автоматического разбана %s", user_id, exc_info=True)
    if config.TYPE_MUTE in kinds:
        try:
            await unrestrict_member(bot, chat_id, user_id)
            released.append(config.TYPE_MUTE)
        except TelegramAPIError as exc:
            logger.error("Не удалось автоматически снять мут с %s: %s", user_id, exc)
        except Exception:  # noqa: BLE001
            logger.error("Ошибка автоматического снятия мута %s", user_id, exc_info=True)

    await queries.deactivate_punishments(db, chat_id, user_id)
    await queries.set_user_banned(db, chat_id, user_id, False)
    await queries.set_user_muted(db, chat_id, user_id, False)
    await queries.remove_ban_mark(db, user_id, chat_id)
    return released


async def process_expired_punishments(bot: Bot, db: Database, limit: int = 50) -> int:
    """Снять все наказания с истёкшим сроком; вернуть число обработанных."""
    try:
        expired = await queries.get_expired_punishments(db, limit)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось получить истёкшие наказания", exc_info=True)
        return 0

    groups: dict[tuple[int, int], list[Punishment]] = {}
    for punishment in expired:
        groups.setdefault((punishment.chat_id, punishment.user_id), []).append(punishment)

    processed = 0
    for (chat_id, user_id), punishments in groups.items():
        try:
            await release_expired(bot, db, chat_id, user_id, punishments)
            processed += len(punishments)
        except Exception:  # noqa: BLE001
            logger.error(
                "Ошибка при снятии истёкших наказаний %s в %s",
                user_id,
                chat_id,
                exc_info=True,
            )
    if processed:
        logger.info("Автоматически снято истёкших наказаний: %d", processed)
    return processed


async def mute_newcomer_for_antiraid(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    target_name: str,
) -> PunishmentResult:
    """Замутить новичка на время антирейда."""
    return await mute_user(
        bot,
        db,
        chat_id,
        user_id,
        target_name,
        config.ANTIRAID_MUTE_SECONDS,
        "Антирейд: проверка новичка 🛡",
        None,
    )
