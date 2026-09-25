"""Модуль антирейда и антиспама.

Отслеживает поток входящих участников и спам-активность.

Модуль ничего не знает про хендлеры: он получает ``Bot``, соединение с базой
и параметры чата, а наружу отдаёт готовые результаты. Внутри:

    * :class:`AntiRaidManager` — память о входах и сообщениях (in-memory);
    * механика Telegram (мут/размут, закрытие чата, ревок инвайтов,
      удаление сообщений) — отдельные функции;
    * тексты и уведомления собирают хендлеры (:mod:`yamochan.handlers.antiraid`).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Final, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions

import config
from ..database import queries
from ..database.db import Database
from ..database.models import ChatInfo
from . import punishment, time_parser

logger = logging.getLogger(__name__)

#: Права «чат закрыт»: писать нельзя никому, приглашать тоже.
CLOSED_PERMISSIONS: Final[ChatPermissions] = ChatPermissions(
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


class AntiRaidManager:
    """Менеджер антирейда — отслеживает входящих юзеров и спам."""

    def __init__(self) -> None:
        # {chat_id: [(user_id, timestamp), ...]}
        self.join_log: dict[int, list[tuple[int, float]]] = defaultdict(list)
        # {chat_id: [user_id, ...]} — подозрительные юзеры текущего рейда
        self.raid_suspects: dict[int, list[int]] = defaultdict(list)
        # {(chat_id, user_id): [timestamp, ...]} — лог сообщений для антиспама
        self.message_log: dict[tuple[int, int], list[float]] = defaultdict(list)
        # {(chat_id, user_id): [(message_id, timestamp), ...]} — что удалять
        self.recent_messages: dict[tuple[int, int], list[tuple[int, float]]] = (
            defaultdict(list)
        )
        # {chat_id: timestamp} — до какого момента антирейд «на паузе»
        self.cooldown_until: dict[int, float] = {}
        # Счётчики срабатываний для расширенной статистики админ-панели.
        self.antiraid_hits: int = 0
        self.antispam_hits: int = 0

    # ------------------------------------------------------------------
    # Антирейд
    # ------------------------------------------------------------------
    async def register_join(
        self,
        chat_id: int,
        user_id: int,
        settings: dict,
        bot: Bot,
    ) -> bool:
        """Зарегистрировать вход нового участника.

        :param chat_id: идентификатор чата.
        :param user_id: идентификатор вошедшего.
        :param settings: настройки чата.
        :param bot: экземпляр бота (в сигнатуре для единообразия вызовов).
        :returns: ``True``, если сработал порог входов — значит это рейд.
        """
        if not settings.get("antiraid_enabled", False):
            return False
        if self._on_cooldown(chat_id) or self.is_under_protection(chat_id):
            return False

        now = time.time()
        threshold = max(
            1, int(settings.get("antiraid_threshold", config.ANTIRAID_DEFAULT_THRESHOLD))
        )
        timeframe = max(
            1, int(settings.get("antiraid_timeframe", config.ANTIRAID_DEFAULT_TIMEFRAME))
        )

        self.join_log[chat_id].append((int(user_id), now))
        # Очищаем старые записи за пределами timeframe.
        self.join_log[chat_id] = [
            (uid, ts) for uid, ts in self.join_log[chat_id] if now - ts <= timeframe
        ]

        if len(self.join_log[chat_id]) >= threshold:
            # РЕЙД ОБНАРУЖЕН
            self.raid_suspects[chat_id] = [uid for uid, _ in self.join_log[chat_id]]
            self.join_log[chat_id].clear()
            self.cooldown_until[chat_id] = now + config.ANTIRAID_COOLDOWN_SECONDS
            self.antiraid_hits += 1
            logger.warning(
                "Антирейд в чате %s: %s входов за %s сек — это рейд!",
                chat_id,
                len(self.raid_suspects[chat_id]),
                timeframe,
            )
            return True

        return False

    def get_suspects(self, chat_id: int) -> list[int]:
        """Получить список подозрительных юзеров."""
        return list(self.raid_suspects.get(chat_id, []))

    def clear_suspects(self, chat_id: int) -> None:
        """Очистить список подозрительных."""
        self.raid_suspects.pop(chat_id, None)

    def remember_suspects(self, chat_id: int, user_ids: Sequence[int]) -> None:
        """Запомнить подозрительных (например, восстановив их из базы)."""
        merged = list(
            dict.fromkeys([*self.raid_suspects.get(chat_id, []), *map(int, user_ids)])
        )
        self.raid_suspects[chat_id] = merged

    def is_under_protection(self, chat_id: int) -> bool:
        """Активна ли прямо сейчас защита чата (по памяти менеджера)."""
        return bool(self.raid_suspects.get(chat_id))

    def _on_cooldown(self, chat_id: int) -> bool:
        """Не сработал ли антирейд только что (пауза после снятия защиты)."""
        until = self.cooldown_until.get(chat_id, 0.0)
        if not until:
            return False
        if time.time() < until:
            return True
        self.cooldown_until.pop(chat_id, None)
        return False

    def set_cooldown(
        self,
        chat_id: int,
        seconds: int = config.ANTIRAID_COOLDOWN_SECONDS,
    ) -> None:
        """Поставить антирейд на паузу (после снятия защиты)."""
        self.cooldown_until[chat_id] = time.time() + max(0, int(seconds))

    def forget_chat(self, chat_id: int) -> None:
        """Полностью забыть чат: входы, подозрительных и логи сообщений."""
        self.join_log.pop(chat_id, None)
        self.raid_suspects.pop(chat_id, None)
        for key in [key for key in self.message_log if key[0] == chat_id]:
            self.message_log.pop(key, None)
        for key in [key for key in self.recent_messages if key[0] == chat_id]:
            self.recent_messages.pop(key, None)

    # ------------------------------------------------------------------
    # Антиспам
    # ------------------------------------------------------------------
    async def register_message(
        self,
        chat_id: int,
        user_id: int,
        settings: dict,
    ) -> bool:
        """Зарегистрировать сообщение для антиспама.

        Учитывается любой контент: текст, стикер, гифка, медиа — одинаково.

        :param chat_id: идентификатор чата.
        :param user_id: автор сообщения.
        :param settings: настройки чата.
        :returns: ``True``, если сработал порог и это спам.
        """
        now = time.time()
        key = (int(chat_id), int(user_id))
        msg_threshold = max(
            2, int(settings.get("spam_msg_threshold", config.SPAM_DEFAULT_THRESHOLD))
        )
        msg_timeframe = max(
            1, int(settings.get("spam_msg_timeframe", config.SPAM_DEFAULT_TIMEFRAME))
        )

        self.message_log[key].append(now)
        # Очищаем старые записи.
        self.message_log[key] = [
            ts for ts in self.message_log[key] if now - ts <= msg_timeframe
        ]

        if len(self.message_log[key]) == msg_threshold:
            # Порог перейден впервые в этом окне — считаем срабатывание одно.
            self.antispam_hits += 1
            return True
        return len(self.message_log[key]) >= msg_threshold

    def remember_message(self, chat_id: int, user_id: int, message_id: int) -> None:
        """Запомнить идентификатор сообщения, чтобы потом его удалить."""
        key = (int(chat_id), int(user_id))
        now = time.time()
        self.recent_messages[key].append((int(message_id), now))
        self.recent_messages[key] = [
            (mid, ts)
            for mid, ts in self.recent_messages[key]
            if now - ts <= config.RECENT_MESSAGE_TTL_SECONDS
        ][-config.MAX_MESSAGES_TO_DELETE :]

    def recent_message_ids(self, chat_id: int, user_id: int, seconds: int) -> list[int]:
        """Идентификаторы сообщений пользователя за последние ``seconds`` секунд."""
        key = (int(chat_id), int(user_id))
        threshold = time.time() - max(1, int(seconds))
        return [mid for mid, ts in self.recent_messages.get(key, []) if ts >= threshold]

    def forget_messages(self, chat_id: int, user_id: int) -> None:
        """Забыть сообщения пользователя (например, после их удаления)."""
        self.message_log.pop((int(chat_id), int(user_id)), None)
        self.recent_messages.pop((int(chat_id), int(user_id)), None)


#: Глобальный менеджер антирейда и антиспама.
antiraid_manager: Final[AntiRaidManager] = AntiRaidManager()


# ---------------------------------------------------------------------------
# Механика Telegram
# ---------------------------------------------------------------------------
async def _set_chat_permissions(
    bot: Bot,
    chat_id: int,
    permissions: ChatPermissions,
) -> bool:
    """Поставить права чата (с откатом на вызов без независимых прав)."""
    try:
        await bot.set_chat_permissions(
            chat_id=chat_id,
            permissions=permissions,
            use_independent_chat_permissions=True,
        )
        return True
    except TelegramAPIError as exc:
        logger.info("Повторная попытка setChatPermissions без независимых прав: %s", exc)
    try:
        await bot.set_chat_permissions(chat_id=chat_id, permissions=permissions)
        return True
    except TelegramAPIError as exc:
        logger.error("Не удалось изменить права чата %s: %s", chat_id, exc)
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка прав чата %s", chat_id, exc_info=True)
    return False


async def close_chat(bot: Bot, chat_id: int) -> bool:
    """Закрыть чат: никто не может писать и приглашать."""
    closed = await _set_chat_permissions(bot, chat_id, CLOSED_PERMISSIONS)
    logger.info("Чат %s закрыт антирейдом: %s", chat_id, closed)
    return closed


async def open_chat(bot: Bot, chat_id: int) -> bool:
    """Вернуть чату обычные права участников."""
    opened = await _set_chat_permissions(bot, chat_id, punishment.FULL_PERMISSIONS)
    logger.info("Чат %s снова открыт: %s", chat_id, opened)
    return opened


async def revoke_invite_link(bot: Bot, chat_id: int) -> Optional[str]:
    """Обновить инвайт-ссылку: старая перестаёт работать.

    :returns: новая ссылка или ``None``, если обновить не получилось.
    """
    try:
        invite = await bot.export_chat_invite_link(chat_id=chat_id)
    except TelegramAPIError as exc:
        logger.info("Не удалось обновить инвайт-ссылку чата %s: %s", chat_id, exc)
        return None
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка revoke-ссылки %s", chat_id, exc_info=True)
        return None
    if invite:
        logger.info("Инвайт-ссылка чата %s перевыпущена.", chat_id)
    return invite


async def mute_member(bot: Bot, db: Database, chat_id: int, user_id: int) -> bool:
    """Замутить участника до снятия защиты (без срока)."""
    try:
        await punishment.restrict_member(bot, chat_id, user_id, None)
    except TelegramAPIError as exc:
        logger.error("Не удалось замутить %s в %s: %s", user_id, chat_id, exc)
        return False
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка мута %s в %s", user_id, chat_id, exc_info=True)
        return False
    await queries.set_user_muted(db, chat_id, user_id, True, None)
    return True


async def unmute_member(bot: Bot, db: Database, chat_id: int, user_id: int) -> bool:
    """Вернуть участнику полные права."""
    try:
        await punishment.unrestrict_member(bot, chat_id, user_id)
    except TelegramAPIError as exc:
        logger.info("Не удалось снять мут с %s в %s: %s", user_id, chat_id, exc)
        return False
    except Exception:  # noqa: BLE001
        logger.error("Неожиданная ошибка размута %s в %s", user_id, chat_id, exc_info=True)
        return False
    await queries.set_user_muted(db, chat_id, user_id, False)
    return True


async def delete_recent_messages(
    bot: Bot,
    chat_id: int,
    user_ids: Sequence[int],
    seconds: int,
) -> int:
    """Удалить недавние сообщения указанных участников.

    То, что Telegram удалить не дал (старые сообщения, нет прав), просто
    пропускается — бот из-за этого не падает.

    :returns: сколько сообщений удалено.
    """
    deleted = 0
    for user_id in user_ids:
        for message_id in antiraid_manager.recent_message_ids(chat_id, user_id, seconds):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
                deleted += 1
            except TelegramAPIError:
                continue
            except Exception:  # noqa: BLE001
                logger.debug("Не удалось удалить сообщение %s", message_id, exc_info=True)
        antiraid_manager.forget_messages(chat_id, user_id)
    logger.info("В чате %s удалено сообщений: %s.", chat_id, deleted)
    return deleted


# ---------------------------------------------------------------------------
# Состояние защиты
# ---------------------------------------------------------------------------
async def is_protection_active(db: Database, chat_id: int) -> bool:
    """Активна ли защита чата (по памяти менеджера или по базе)."""
    if antiraid_manager.is_under_protection(chat_id):
        return True
    settings = await queries.get_chat_settings(db, chat_id)
    return bool(settings.get("antiraid_active_protection"))


async def active_protection_chats(db: Database, owner_id: int) -> list[ChatInfo]:
    """Чаты владельца, где прямо сейчас включена активная защита."""
    try:
        chats = await queries.get_owner_chats(db, owner_id)
    except Exception:  # noqa: BLE001
        logger.error("Не удалось получить чаты владельца %s", owner_id, exc_info=True)
        return []

    guarded: list[ChatInfo] = []
    for chat in chats:
        if chat.settings.get("antiraid_active_protection"):
            guarded.append(chat)
        elif antiraid_manager.is_under_protection(chat.chat_id):
            guarded.append(chat)
    return guarded


async def activate_protection(bot: Bot, db: Database, chat_id: int) -> bool:
    """Включить защиту: закрыть чат, обновить ссылку, записать флаг в БД."""
    await queries.update_chat_setting(db, chat_id, "antiraid_active_protection", True)
    closed = await close_chat(bot, chat_id)
    await revoke_invite_link(bot, chat_id)
    return closed


async def lift_protection(
    bot: Bot,
    db: Database,
    chat_id: int,
    *,
    unmute: bool = True,
) -> bool:
    """Снять защиту: открыть чат, размутить подозрительных, убрать метки.

    :param unmute: размучивать ли подозрительных (забаненных не трогаем).
    :returns: удалось ли открыть чат.
    """
    suspects = antiraid_manager.get_suspects(chat_id)
    if not suspects:
        suspects = await queries.get_raid_suspects(db, chat_id)

    if unmute:
        for user_id in suspects:
            chat_user = await queries.get_chat_user(db, chat_id, user_id)
            if chat_user is not None and chat_user.is_banned:
                continue
            await unmute_member(bot, db, chat_id, user_id)

    await queries.clear_raid_suspects(db, chat_id)
    await queries.update_chat_setting(db, chat_id, "antiraid_active_protection", False)
    antiraid_manager.clear_suspects(chat_id)
    antiraid_manager.set_cooldown(chat_id)
    opened = await open_chat(bot, chat_id)
    logger.info("Защита чата %s снята (размучено: %s).", chat_id, len(suspects))
    return opened


# ---------------------------------------------------------------------------
# Разбор пользовательского ввода
# ---------------------------------------------------------------------------
def parse_threshold_input(text: Optional[str]) -> Optional[tuple[int, int]]:
    """Разобрать строку вида «5 5м» в пару ``(количество, секунды)``.

    Разбор строгий: **первое** число — это количество, **вторая** часть —
    время. Значения берутся ровно такими, как их ввёл владелец: никаких
    «докруток» до минимума, из-за которых «1 2м» превращалось в «2 за 2 мин».

    Секунды поддерживаются (``30с``/``30s``), число без единицы — минуты.

    :param text: текст от владельца, например ``"1 2м"`` или ``"7 10с"``.
    :returns: ``(количество, секунды)`` или ``None``, если формат неверный.
    """
    if not text:
        return None

    parts = text.strip().replace(",", " ").split(maxsplit=1)
    if len(parts) != 2:
        return None

    try:
        count = int(parts[0])
    except ValueError:
        return None
    if count < 1:
        return None

    seconds = time_parser.parse_time_token(parts[1])
    if not seconds or seconds < 1:
        return None

    return count, seconds


