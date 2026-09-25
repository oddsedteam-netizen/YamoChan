"""Обработка входа и выхода участников чата, подсчёт активности, антирейд.

Модуль отвечает за:
    * :class:`MessageCounterMiddleware` — +1 к счётчикам за каждое сообщение
      в группе (требование проекта №8);
    * приветствие новичков и возвращение «старожилов» с краткой сводкой;
    * предупреждение о метках банов пользователя в других чатах;
    * анонимное приветствие (``welcome_anonymous``): сводка без имени и
      проверочный мут новичка;
    * превентивные муты при входе (``marked_mute_*`` — помеченным,
      ``join_mute_*`` — всем): применяется ровно один мут с максимальным
      сроком из применимых;
    * пометку выхода из чата без потери данных;
    * антирейд: мут новичков на 10 минут и кик при всплеске входов;
    * удаление служебных сообщений (``delete_service_messages``);
    * определение владельца чата при добавлении бота.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Final, Optional

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import BaseFilter
from aiogram.types import (
    ChatMemberUpdated,
    ChatPermissions,
    Message,
    TelegramObject,
    User,
)

import config
from database import queries
from database.db import Database
from database.models import ChatUser, UserProfile, utcnow
from services import admin as admin_service
from services import antiraid as antiraid_service
from services import permissions, profile as profile_service, punishment
from utils import command_filter, error_handler, telegram
from handlers import antiraid as antiraid_ui
from handlers import rules as rules_ui

logger = logging.getLogger(__name__)

#: Роутер событий чата.
router: Final[Router] = Router(name="events")

#: Групповые типы чатов.
GROUP_CHAT_TYPES: Final[set[str]] = {ChatType.GROUP, ChatType.SUPERGROUP}

#: Статусы, при которых бот считается присутствующим в чате.
BOT_PRESENT_STATUSES: Final[set[str]] = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.RESTRICTED,
}

#: Приветствие чата после добавления бота.
BOT_ADDED_GREETING: Final[str] = (
    "✨ Спасибо, что добавили меня! Я YamoChan — ваш модератор~ 💕\n"
    "Выдайте мне права администратора, и я наведу порядок 🌸\n\n"
    "Команды: .бан .разбан .мут .размут .варн .снятьварн .кик .инфо"
)

#: Поля сообщения, по которым оно считается служебным (не активностью юзера).
SERVICE_MESSAGE_FIELDS: Final[tuple[str, ...]] = (
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "pinned_message",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_scheduled",
    "proximity_alert_triggered",
)


def is_service_message(message: Message) -> bool:
    """Проверить, что сообщение служебное (вход/выход, закреп, смена названия).

    Такие сообщения не считаются активностью пользователя: иначе новичок
    получал запись в ``chat_users`` до приветствия и бот писал ему
    «С возвращением».

    :param message: сообщение из апдейта.
    """
    return any(getattr(message, field, None) for field in SERVICE_MESSAGE_FIELDS)


#: Служебные сообщения, которые бот умеет скрывать.
#:
#: Миграция группы в супергруппу сюда не входит: её системное сообщение
#: Telegram использует для перехода в новый чат, удалять его нельзя.
DELETABLE_SERVICE_MESSAGE_FIELDS: Final[tuple[str, ...]] = (
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "pinned_message",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_scheduled",
    "proximity_alert_triggered",
)


def is_deletable_service_message(message: Message) -> bool:
    """Служебное ли сообщение, которое бот вправе скрыть.

    :param message: сообщение из апдейта.
    """
    return any(
        getattr(message, field, None) for field in DELETABLE_SERVICE_MESSAGE_FIELDS
    )


class ServiceMessageFilter(BaseFilter):
    """Фильтр: служебное сообщение (название, фото, закреп, видеозвонок)."""

    async def __call__(self, message: Message) -> bool:
        """Проверить, что сообщение служебное и его можно удалять."""
        return is_deletable_service_message(message)


async def delete_service_message(
    message: Message,
    settings: dict[str, Any],
) -> bool:
    """Удалить служебное сообщение, если это разрешено настройками чата.

    Права ``can_delete_messages`` может не быть: тогда Telegram вернёт
    ошибку, и сообщение просто останется в чате — бот падать не должен.

    :param message: служебное сообщение.
    :param settings: настройки чата.
    :returns: ``True``, если сообщение действительно удалено.
    """
    if not bool(settings.get(config.DELETE_SERVICE_MESSAGES_KEY, True)):
        return False
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001 - прав на удаление может не быть
        logger.debug(
            "Не удалось удалить служебное сообщение в чате %s: %s",
            message.chat.id,
            exc,
        )
        return False
    return True


class MessageCounterMiddleware(BaseMiddleware):
    """Middleware подсчёта сообщений в группах."""

    def __init__(self, db: Database) -> None:
        """Сохранить соединение с базой данных.

        :param db: объект базы данных.
        """
        self._db: Database = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Посчитать сообщение, проверить спам и передать управление дальше."""
        try:
            if isinstance(event, Message) and not is_service_message(event):
                await self._count_message(event)
                await self._check_spam(event, data.get("bot"))
        except Exception as exc:  # noqa: BLE001 - счётчики не важнее апдейта
            error_handler.log_exception("подсчёте сообщения", exc)
        return await handler(event, data)

    async def _count_message(self, message: Message) -> None:
        """Обновить счётчики сообщений пользователя в чате и глобально.

        Служебные сообщения, сообщения ботов и команды модерации не считаем.
        """
        chat = message.chat
        if chat.type not in GROUP_CHAT_TYPES:
            return
        author = message.from_user
        if author is None or author.is_bot:
            return
        text = message.text or message.caption or ""
        # Команды (в том числе без префикса) — не активность пользователя.
        if command_filter.is_command_message(text):
            return

        # Число участников в апдейте сообщения не приходит (в aiogram 3.x поля
        # members_count у Chat нет), а лишний запрос на каждое сообщение не нужен:
        # счётчик подтягивается при входе участника и добавлении бота.
        await queries.ensure_chat(
            self._db,
            chat.id,
            chat.title,
            telegram.resolve_members_count(chat),
        )
        await queries.ensure_user(
            self._db, author.id, author.username, author.first_name
        )
        await queries.increment_messages(self._db, chat.id, author.id)
        await queries.log_message(self._db, chat.id, author.id)

    # ------------------------------------------------------------------
    # Антиспам
    # ------------------------------------------------------------------
    async def _check_spam(self, message: Message, bot: Optional[Bot]) -> None:
        """Проверить сообщение на спам и наказать нарушителя.

        Учитывается любой контент: текст, стикер, гифка, медиа — одинаково.
        Админов, владельца чата и самого бота проверка не касается.
        """
        if bot is None:
            return
        chat = message.chat
        if chat.type not in GROUP_CHAT_TYPES:
            return
        author = message.from_user
        if author is None or author.is_bot:
            return
        text = message.text or message.caption or ""
        # Команды (в том числе без префикса) — не спам.
        if command_filter.is_command_message(text):
            return

        # Идентификаторы сообщений нужны, чтобы потом их удалить.
        antiraid_service.antiraid_manager.remember_message(
            chat.id, author.id, message.message_id
        )

        settings = await queries.get_chat_settings(self._db, chat.id)
        if not await antiraid_service.antiraid_manager.register_message(
            chat.id, author.id, settings
        ):
            return

        # Порог сработал — только теперь спрашиваем Telegram о правах автора.
        if await self._is_privileged(bot, chat.id, author.id):
            antiraid_service.antiraid_manager.forget_messages(chat.id, author.id)
            return
        await self._punish_spammer(bot, chat.id, author, settings)

    async def _is_privileged(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        """Владелец, админ или супер-админ — спамом не считаем."""
        if user_id in config.SUPERADMIN_IDS:
            return True
        try:
            chat = await queries.get_chat(self._db, chat_id)
        except Exception:  # noqa: BLE001
            chat = None
        if chat is not None and chat.owner_id == user_id:
            return True
        return await permissions.is_chat_administrator(bot, chat_id, user_id)

    async def _punish_spammer(
        self,
        bot: Bot,
        chat_id: int,
        author: User,
        settings: dict[str, Any],
    ) -> None:
        """Забанить спамера, почистить его сообщения и сообщить в чат."""
        name = _user_name(author)
        logger.warning(
            "Антиспам в чате %s: %s (%s) превысил порог сообщений.",
            chat_id,
            author.id,
            name,
        )
        await punishment.ban_user(
            bot,
            self._db,
            chat_id,
            author.id,
            name,
            None,
            config.SPAM_BAN_REASON,
            None,
        )
        await queries.set_user_spammer(self._db, author.id, True)

        timeframe = int(
            settings.get("spam_msg_timeframe") or config.SPAM_DEFAULT_TIMEFRAME
        )
        await antiraid_service.delete_recent_messages(bot, chat_id, [author.id], timeframe)

        chat_closed = False
        if settings.get("antispam_mode") == config.ANTISPAM_MODE_CLOSE:
            chat_closed = await antiraid_service.close_chat(bot, chat_id)
            await queries.update_chat_setting(
                self._db, chat_id, "antiraid_active_protection", bool(chat_closed)
            )

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=profile_service.build_spam_text(name, author.id, chat_closed),
            )
        except Exception as exc:  # noqa: BLE001 - чат мог быть закрыт для бота
            logger.info("Не удалось сообщить о спамере в чате %s: %s", chat_id, exc)


def _user_name(user: User) -> str:
    """Получить человекочитаемое имя пользователя из Telegram."""
    return user.full_name or user.username or str(user.id)


async def _knows_user(db: Database, user_id: int) -> bool:
    """Проверить, есть ли у пользователя профиль в базе.

    :param db: соединение с базой данных.
    :param user_id: идентификатор пользователя.
    """
    try:
        return await queries.get_user(db, user_id) is not None
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("проверке профиля пользователя", exc)
        return False


def _never_active(chat_user: ChatUser) -> bool:
    """Проверить, что участник в этом чате ещё ничего не делал.

    Признаки активности — сообщения и предупреждения: ``last_message_at``
    проставляется самой записью ``chat_users``, поэтому на него не смотрим.
    Запись без активности могла появиться раньше от служебных событий,
    поэтому для такого участника «С возвращением» не пишем.

    :param chat_user: связка «чат — пользователь» из базы.
    """
    return chat_user.messages_count == 0 and chat_user.warns_count == 0


async def handle_new_member(
    bot: Bot,
    db: Database,
    message: Message,
    member: User,
) -> Optional[str]:
    """Обработать вход одного участника и вернуть текст приветствия.

    Порядок важен: состояние пользователя фиксируется ДО создания записей,
    иначе «С возвращением» получит даже тот, кто в этом чате впервые.

    Логика:
        1. совсем новый пользователь → «Добро пожаловать»;
        2. пользователь есть в базе, но в этом чате впервые → приветствие
           (или предупреждение о метках банов);
        3. пользователь уже был в этом чате → краткая сводка с возвращением.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param message: служебное сообщение о входе.
    :param member: вошедший пользователь.
    :returns: текст ответа бота или ``None``.
    """
    chat_id = message.chat.id
    name = _user_name(member)

    is_new_globally = not await _knows_user(db, member.id)
    chat_user = await queries.get_chat_user(db, chat_id, member.id)
    is_new_in_chat = chat_user is None or _never_active(chat_user)

    profile = await queries.ensure_user(db, member.id, member.username, member.first_name)
    await queries.ensure_chat(
        db,
        chat_id,
        message.chat.title,
        await telegram.fetch_members_count(bot, chat_id),
    )
    await queries.mark_joined(db, chat_id, member.id)

    # 1. Совсем новый пользователь.
    if is_new_globally:
        logger.info("Новый участник %s в чате %s.", member.id, chat_id)
        return profile_service.build_welcome_new_text(name)

    # 2. Бот знает пользователя, но в этом чате он впервые.
    if is_new_in_chat:
        if profile.ban_marks:
            logger.info("У %s есть метки банов в %s чатах.", member.id, len(profile.ban_marks))
            return profile_service.build_welcome_banned_note_text(profile, name)
        return profile_service.build_welcome_new_in_chat_text(name)

    # 3. Возвращение: пользователь уже был в этом чате.
    if chat_user is None:  # страховка от гонки: запись обязана существовать
        chat_user = await queries.ensure_chat_user(db, chat_id, member.id)
    active = await queries.get_active_punishments(db, chat_id, member.id)
    return profile_service.build_welcome_returning_text(profile, chat_user, active, name)


def welcome_check_duration(settings: dict[str, Any]) -> int:
    """Длительность проверочного мута новичка в допустимых границах.

    :param settings: настройки чата (``welcome_check_duration``).
    """
    try:
        raw = int(
            settings.get(config.WELCOME_CHECK_DURATION_KEY)
            or config.WELCOME_CHECK_DEFAULT_SECONDS
        )
    except (TypeError, ValueError):
        raw = config.WELCOME_CHECK_DEFAULT_SECONDS
    return max(
        config.WELCOME_CHECK_MIN_SECONDS,
        min(config.WELCOME_CHECK_MAX_SECONDS, raw),
    )


def mute_until(seconds: int) -> int:
    """Отметка времени (UTC), до которой действует мут.

    :param seconds: длительность мута в секундах.
    """
    return int(
        (datetime.now(config.UTC) + timedelta(seconds=int(seconds))).timestamp()
    )


async def restrict_newcomer(
    bot: Bot,
    chat_id: int,
    user_id: int,
    seconds: int,
) -> bool:
    """Замутить новичка (анонимное приветствие или мут при входе).

    Мут снимает сам Telegram по ``until_date``, поэтому фоновый воркер
    и журнал наказаний здесь не нужны.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    :param user_id: идентификатор новичка.
    :param seconds: длительность ограничения.
    :returns: ``True``, если ограничение установлено.
    """
    until = mute_until(seconds)
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - прав может не хватить
        logger.error(
            "Не смогла замутить нового участника %s в чате %s: %s",
            user_id,
            chat_id,
            exc,
        )
        return False


async def handle_anonymous_welcome(
    bot: Bot,
    db: Database,
    chat_id: int,
    member: User,
    settings: dict[str, Any],
    *,
    chat_title: Optional[str] = None,
    members_count: Optional[int] = None,
) -> bool:
    """Анонимно сообщить о новом участнике и замутить его на проверку.

    Режим ``welcome_anonymous``: имя, ID и юзернейм вошедшего не
    показываются — только сводка по репутации и меткам. Новичок получает
    мут на время проверки, чтобы не успел навредить до решения админов.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param member: вошедший пользователь.
    :param settings: настройки чата.
    :param chat_title: название чата (для записи в базу).
    :param members_count: число участников чата (если известно).
    :returns: ``True``, если анонимное уведомление отправлено.
    """
    check_seconds = welcome_check_duration(settings)

    # Профиль читаем ДО создания записи: иначе «новый аккаунт» уже не
    # отличить от давно известного.
    profile: Optional[UserProfile] = None
    try:
        profile = await queries.get_user(db, member.id)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("чтении профиля нового участника", exc)
    is_new_globally = profile is None

    try:
        await queries.ensure_chat(db, chat_id, chat_title, members_count)
        await queries.mark_joined(db, chat_id, member.id)
        profile = await queries.ensure_user(
            db, member.id, member.username, member.first_name
        )
    except Exception as exc:  # noqa: BLE001 - сводка важнее записей в базе
        error_handler.log_exception("записи анонимного входа", exc)

    # Единый мут при входе: проверочный плюс (если включены) мут всем
    # новичкам и превентивный мут помеченным. Применяется один restrict —
    # с максимальным сроком из применимых.
    applied = await apply_entry_mute(
        bot, db, chat_id, member, settings, check_seconds=check_seconds
    )
    muted = applied is not None
    duration = applied[1] if applied is not None else check_seconds
    logger.info(
        "Анонимный вход: участник %s в чате %s, мут %s секунд (%s).",
        member.id,
        chat_id,
        duration,
        "ок" if muted else "не удалось",
    )

    text = profile_service.build_anonymous_welcome_text(
        None if is_new_globally else profile,
        duration,
        muted=muted,
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Не смогла отправить анонимное приветствие в чат %s: %s", chat_id, exc
        )
        return False
    return True


def join_mute_duration(settings: dict[str, Any]) -> int:
    """Длительность мута при входе для всех новичков в допустимых границах.

    :param settings: настройки чата (``join_mute_duration``).
    """
    try:
        raw = int(
            settings.get(config.JOIN_MUTE_DURATION_KEY)
            or config.JOIN_MUTE_DEFAULT_SECONDS
        )
    except (TypeError, ValueError):
        raw = config.JOIN_MUTE_DEFAULT_SECONDS
    return max(
        config.JOIN_MUTE_MIN_SECONDS,
        min(config.JOIN_MUTE_MAX_SECONDS, raw),
    )


def marked_mute_duration(settings: dict[str, Any]) -> int:
    """Длительность превентивного мута помеченным в допустимых границах.

    :param settings: настройки чата (``marked_mute_duration``).
    """
    try:
        raw = int(
            settings.get("marked_mute_duration") or config.MARKED_MUTE_DEFAULT_SECONDS
        )
    except (TypeError, ValueError):
        raw = config.MARKED_MUTE_DEFAULT_SECONDS
    return max(
        config.MARKED_MUTE_MIN_SECONDS,
        min(config.MARKED_MUTE_MAX_SECONDS, raw),
    )


def combine_mute_candidates(
    candidates: list[tuple[str, int]],
) -> Optional[tuple[str, int]]:
    """Выбрать из применимых мутов один — с максимальным сроком.

    При равенстве сроков приоритет у добавленного раньше, поэтому порядок
    кандидатов задаёт старшинство: метка → мут всем → проверка.

    :param candidates: пары ``(причина, секунды)``.
    :returns: выбранная пара или ``None``, если кандидатов нет.
    """
    valid: list[tuple[str, int]] = []
    for reason, seconds in candidates:
        try:
            value = int(seconds)
        except (TypeError, ValueError):
            continue
        if value > 0:
            valid.append((reason, value))
    if not valid:
        return None
    return max(valid, key=lambda item: item[1])


async def apply_join_mute(
    bot: Bot,
    chat_id: int,
    user_id: int,
    settings: dict[str, Any],
    already_muted: object = False,
) -> Optional[int]:
    """Применить мут при входе, если он включён в настройках чата.

    ``already_muted`` — участник уже замучен (анонимный режим или
    превентивный мут): берётся максимальное время из действующего и нового,
    а повторный ``restrict`` с меньшим сроком не выдаётся.

    :param bot: экземпляр бота.
    :param chat_id: идентификатор чата.
    :param user_id: идентификатор новичка.
    :param settings: настройки чата (``join_mute_*``).
    :param already_muted: ``True`` или длительность действующего мута.
    :returns: длительность применённого мута в секундах или ``None``.
    """
    if not bool(settings.get(config.JOIN_MUTE_ENABLED_KEY, False)):
        return None

    duration = join_mute_duration(settings)
    if duration < 1:
        return None

    current = 0
    if isinstance(already_muted, bool):
        current = duration if already_muted else 0
    else:
        try:
            current = int(already_muted)
        except (TypeError, ValueError):
            current = 0
    final = max(duration, current)
    if final <= current:
        # Действующий мут не короче нового — второй restrict не нужен.
        return None

    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=mute_until(final),
        )
        return final
    except Exception as exc:  # noqa: BLE001 - прав может не хватить
        logger.error("Не смогла применить join_mute: %s", exc)
        return None


async def _is_marked_newcomer(db: Database, user_id: int) -> bool:
    """Помечен ли аккаунт: спамер, много банов или плохая репутация."""
    try:
        profile = await queries.get_user(db, user_id)
    except Exception as exc:  # noqa: BLE001 - метка не критична для входа
        error_handler.log_exception("проверке меток новичка", exc)
        return False
    return profile is not None and profile_service.is_marked_profile(profile)


async def apply_entry_mute(
    bot: Bot,
    db: Database,
    chat_id: int,
    member: User,
    settings: dict[str, Any],
    *,
    check_seconds: int = 0,
) -> Optional[tuple[str, int]]:
    """Применить ОДИН мут при входе — с максимальным сроком из применимых.

    Кандидаты (порядок задаёт старшинство при равных сроках):
        1. превентивный мут помеченным (``marked_mute_enabled``);
        2. мут всем новым участникам (``join_mute_enabled``);
        3. проверочный мут анонимного режима (``check_seconds``).

    ``restrict_chat_member`` вызывается ровно один раз. Мут помеченным
    дополнительно пишется в журнал наказаний, остальные муты снимает сам
    Telegram по ``until_date``.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param member: вошедший пользователь.
    :param settings: настройки чата.
    :param check_seconds: длительность проверочного мута (анонимный режим).
    :returns: пара ``(причина, секунды)`` или ``None``, если мут не применён.
    """
    try:
        candidates: list[tuple[str, int]] = []

        if bool(settings.get("marked_mute_enabled")) and await _is_marked_newcomer(
            db, member.id
        ):
            candidates.append(
                (config.MARKED_MUTE_REASON, marked_mute_duration(settings))
            )

        if bool(settings.get(config.JOIN_MUTE_ENABLED_KEY)):
            candidates.append((config.JOIN_MUTE_REASON, join_mute_duration(settings)))

        if int(check_seconds or 0) > 0:
            candidates.append((config.WELCOME_CHECK_REASON, int(check_seconds)))

        winner = combine_mute_candidates(candidates)
        if winner is None:
            return None

        reason, duration = winner
        name = _user_name(member)

        if reason == config.MARKED_MUTE_REASON:
            # Метка: помимо ограничения ведём запись в журнале наказаний.
            result = await punishment.mute_user(
                bot, db, chat_id, member.id, name, duration, reason, None
            )
            if not result.success:
                logger.info(
                    "Превентивный мут %s в чате %s не удался (нет прав?).",
                    member.id,
                    chat_id,
                )
                return None
        elif reason == config.JOIN_MUTE_REASON:
            applied = await apply_join_mute(bot, chat_id, member.id, settings)
            if applied is None:
                return None
            duration = applied
        elif not await restrict_newcomer(bot, chat_id, member.id, duration):
            return None

        logger.info(
            "Мут при входе: участник %s в чате %s на %s секунд (%s).",
            member.id,
            chat_id,
            duration,
            reason,
        )
        return reason, duration
    except Exception as exc:  # noqa: BLE001 - мут не должен ронять вход
        error_handler.log_exception("муте при входе", exc)
        return None


async def apply_antiraid(
    bot: Bot,
    db: Database,
    chat_id: int,
    member: User,
) -> Optional[str]:
    """Применить антирейд к новому участнику.

    Если за последнюю минуту вошло больше
    :data:`config.ANTIRAID_JOIN_LIMIT` человек — новичок кикается,
    иначе он получает мут на :data:`config.ANTIRAID_MUTE_SECONDS` секунд.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param member: вошедший пользователь.
    :returns: текст ответа бота или ``None``.
    """
    name = _user_name(member)
    since = utcnow() - timedelta(seconds=config.ANTIRAID_WINDOW_SECONDS)
    recent_joins = await queries.count_recent_joins(db, chat_id, since)

    if recent_joins > config.ANTIRAID_JOIN_LIMIT:
        logger.warning(
            "Антирейд в чате %s: %s входов за %s сек, кикаю %s.",
            chat_id,
            recent_joins,
            config.ANTIRAID_WINDOW_SECONDS,
            member.id,
        )
        await punishment.kick_user(
            bot,
            db,
            chat_id,
            member.id,
            name,
            "Антирейд: всплеск входов ",
            None,
        )
        return profile_service.build_antiraid_kick_text(name)

    result = await punishment.mute_newcomer_for_antiraid(bot, db, chat_id, member.id, name)
    return result.message


async def apply_raid_protection(
    bot: Bot,
    db: Database,
    chat_id: int,
    settings: dict[str, Any],
) -> Optional[str]:
    """Реакция на рейд: мут, чистка сообщений, закрытие чата и уведомление.

    Порядок действий:
        1. замутить всех подозрительных;
        2. пометить их в ``chat_users`` (``is_raid_suspect``);
        3. удалить их свежие сообщения (что получится — остальное пропускаем);
        4. закрыть чат и обновить инвайт-ссылку;
        5. уведомить владельца в личке с кнопками решения.

    :param bot: экземпляр бота.
    :param db: соединение с базой данных.
    :param chat_id: идентификатор чата.
    :param settings: настройки чата.
    :returns: текст сообщения в чат или ``None``.
    """
    manager = antiraid_service.antiraid_manager
    suspects = manager.get_suspects(chat_id)
    timeframe = int(settings.get("antiraid_timeframe") or config.ANTIRAID_DEFAULT_TIMEFRAME)

    muted = 0
    for user_id in suspects:
        if await antiraid_service.mute_member(bot, db, chat_id, user_id):
            muted += 1
    await queries.mark_raid_suspects(db, chat_id, suspects)
    await antiraid_service.delete_recent_messages(bot, chat_id, suspects, timeframe)
    await antiraid_service.activate_protection(bot, db, chat_id)

    chat_info = await queries.get_chat(db, chat_id)
    if chat_info is not None:
        await antiraid_ui.notify_raid(bot, db, chat_info, suspects, timeframe)
    else:
        logger.warning("Рейд в чате %s, но чата нет в базе — уведомление не отправлено.", chat_id)

    logger.warning(
        "Антирейд в чате %s: подозрительных %s, замучено %s, чат закрыт.",
        chat_id,
        len(suspects),
        muted,
    )
    return profile_service.build_raid_chat_notice_text(len(suspects))


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), F.new_chat_members)
async def on_new_members(message: Message, db: Database, bot: Bot) -> None:
    """Поприветствовать новых участников и применить антирейд.

    Служебное сообщение о входе удаляется (настройка
    ``delete_service_messages``), а при включённом ``welcome_anonymous``
    вместо обычного приветствия уходит анонимная сводка, и новичок
    получает мут на время проверки.

    :param message: служебное сообщение о входе.
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    """
    try:
        if not message.new_chat_members:
            return
        chat_id = message.chat.id
        settings = await queries.get_chat_settings(db, chat_id)

        # Системное сообщение о входе скрываем: бот всё равно ответит своим
        # приветствием, а лишний шум в чате не нужен.
        await delete_service_message(message, settings)

        # Анонимный режим: личность новичка не раскрываем, вместо обычного
        # приветствия уходит сводка, а сам новичок уходит в проверочный мут.
        if bool(settings.get(config.WELCOME_ANONYMOUS_KEY)):
            for member in message.new_chat_members:
                if member.is_bot:
                    continue
                try:
                    await handle_anonymous_welcome(
                        bot,
                        db,
                        chat_id,
                        member,
                        settings,
                        chat_title=message.chat.title,
                        members_count=telegram.resolve_members_count(message.chat),
                    )
                except Exception as exc:  # noqa: BLE001 - один вход не ломает других
                    error_handler.log_exception(
                        f"анонимном приветствии участника {member.id}", exc
                    )
            return

        antiraid_enabled = bool(settings.get("antiraid_enabled", settings.get("antiraid")))

        replies: list[str] = []
        manager = antiraid_service.antiraid_manager
        for member in message.new_chat_members:
            if member.is_bot:
                continue

            # Своё приветствие владельца (текст + премиум-эмодзи + фото + кнопки)
            # заменяет стандартный текст бота.
            custom_greeting_sent = await rules_ui.send_custom_greeting(
                bot, db, chat_id, settings, member
            )

            greeting = await handle_new_member(bot, db, message, member)
            show_profile = rules_ui.setting_flag(
                settings, rules_ui.GREETING_PROFILE_KEY, "welcome_show_profile"
            )
            if greeting and (not custom_greeting_sent or show_profile):
                replies.append(greeting)

            # Правила при входе: свёрнутая цитата с данными новичка.
            try:
                await rules_ui.send_rules_on_join(bot, db, chat_id, settings, member)
            except Exception as exc:  # noqa: BLE001 - правила не важнее приветствия
                error_handler.log_exception("отправке правил при входе", exc)

            # Превентивные муты: помеченным и всем новым участникам (если
            # включены). apply_entry_mute применяет один мут — с наибольшим
            # сроком из применимых.
            try:
                applied = await apply_entry_mute(bot, db, chat_id, member, settings)
                if applied is not None:
                    reason, duration = applied
                    if reason == config.MARKED_MUTE_REASON:
                        replies.append(
                            profile_service.build_marked_mute_notice_text(
                                _user_name(member), duration
                            )
                        )
                    else:
                        replies.append(
                            profile_service.build_join_mute_notice_text(
                                _user_name(member), duration
                            )
                        )
            except Exception as exc:  # noqa: BLE001 - мут не важнее приветствия
                error_handler.log_exception("превентивном муте новичка", exc)

            if not antiraid_enabled:
                continue

            # 1. Считаем входы: превышение порога означает рейд.
            if await manager.register_join(chat_id, member.id, settings, bot):
                raid_note = await apply_raid_protection(bot, db, chat_id, settings)
                if raid_note:
                    replies.append(raid_note)
                continue

            # 2. Чат уже под защитой: новичок сразу становится подозрительным.
            if manager.is_under_protection(chat_id):
                await antiraid_service.mute_member(bot, db, chat_id, member.id)
                await queries.mark_raid_suspects(db, chat_id, [member.id])
                manager.remember_suspects(chat_id, [member.id])
                continue

            # 3. Обычный вход: прежние правила (кик при всплеске или мут новичка).
            antiraid_note = await apply_antiraid(bot, db, chat_id, member)
            if antiraid_note:
                replies.append(antiraid_note)

        if replies:
            await message.answer("\n\n".join(replies))
    except Exception as exc:  # noqa: BLE001 - входы не должны ронять бота
        error_handler.log_exception("приветствии нового участника", exc)


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), F.left_chat_member)
async def on_left_member(message: Message, db: Database) -> None:
    """Отметить выход участника, сохранив всю его статистику.

    Служебное сообщение о выходе удаляется, если в чате включено
    ``delete_service_messages``.

    :param message: служебное сообщение о выходе.
    :param db: соединение с базой данных.
    """
    try:
        member = message.left_chat_member
        if member is None or member.is_bot:
            return
        settings = await queries.get_chat_settings(db, message.chat.id)
        await delete_service_message(message, settings)
        await queries.set_member_presence(db, message.chat.id, member.id, False)
        # Точного числа участников в апдейте нет — уменьшаем то, что знаем сами.
        chat_info = await queries.get_chat(db, message.chat.id)
        known_members = chat_info.members_count if chat_info is not None else 0
        await queries.update_members_count(db, message.chat.id, max(0, known_members - 1))
        logger.info("Участник %s покинул чат %s.", member.id, message.chat.id)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("обработке выхода участника", exc)


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), ServiceMessageFilter())
async def on_service_message(message: Message, db: Database) -> None:
    """Скрыть прочие служебные сообщения (название, фото, закреп, видеозвонок).

    Вход и выход обрабатываются отдельно (:func:`on_new_members` и
    :func:`on_left_member`), здесь остаются события, на которые бот больше
    никак не реагирует. Ошибки не считаются критичными: без права
    ``can_delete_messages`` сообщение просто останется в чате.

    :param message: служебное сообщение.
    :param db: соединение с базой данных.
    """
    try:
        settings = await queries.get_chat_settings(db, message.chat.id)
        if await delete_service_message(message, settings):
            logger.info(
                "Служебное сообщение в чате %s скрыто.", message.chat.id
            )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("удалении служебного сообщения", exc)


async def _banned_owner_of(
    db: Database,
    owner_id: Optional[int],
    event: ChatMemberUpdated,
) -> Optional[int]:
    """Кто из причастных к добавлению бота забанен в боте.

    Проверяются и владелец чата, и тот, кто добавил бота: забаненный
    пользователь не может держать бота в своих чатах.

    :param db: соединение с базой данных.
    :param owner_id: определённый владелец чата (может быть ``None``).
    :param event: обновление статуса бота в чате.
    :returns: идентификатор забаненного пользователя или ``None``.
    """
    candidates = [owner_id, event.from_user.id if event.from_user else None]
    owner = int(config.BOT_OWNER_ID or 0)
    for candidate in candidates:
        if not candidate or int(candidate) == owner:
            continue
        try:
            if await queries.is_admin_banned(db, int(candidate)):
                return int(candidate)
        except Exception:  # noqa: BLE001 - проверка не важнее работы бота
            logger.error(
                "Не удалось проверить бан пользователя %s", candidate, exc_info=True
            )
    return None


@router.my_chat_member()
async def on_bot_membership(event: ChatMemberUpdated, db: Database, bot: Bot) -> None:
    """Обработать добавление и удаление бота из чата.

    При добавлении: сразу создаём запись в ``chats``, определяем владельца
    через ``getChatAdministrators`` (в том числе анонимного), связываем его
    с ``chat_users`` и здороваемся с чатом.

    :param event: обновление статуса бота в чате.
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    """
    try:
        chat = event.chat
        if chat.type not in GROUP_CHAT_TYPES:
            return

        new_status = event.new_chat_member.status
        if new_status not in BOT_PRESENT_STATUSES:
            logger.info("Бота удалили из чата %s.", chat.id)
            return

        # 1. Чат попадает в базу сразу — не дожидаясь первого сообщения в группе.
        await queries.ensure_chat(
            db,
            chat.id,
            chat.title,
            await telegram.fetch_members_count(bot, chat.id),
        )
        logger.info("Чат %s (%s) сохранён в базе.", chat.id, chat.title)

        # 2. Владелец: фильтр по status == "creator" среди администраторов.
        owner_id, owner_channel_id = await permissions.sync_chat_owner(bot, db, chat.id)

        # 2.5. Забаненный в боте пользователь не может держать бота у себя:
        #      чат сразу отвязывается (и добавление, и владелец чата).
        banned_user = await _banned_owner_of(db, owner_id, event)
        if banned_user is not None:
            logger.warning(
                "Чат %s добавлен пользователем %s, забаненным в боте — отвязываю.",
                chat.id,
                banned_user,
            )
            await admin_service.detach_chat(
                bot,
                db,
                chat.id,
                title=chat.title,
                owner_id=banned_user,
                actor_id=banned_user,
            )
            return

        # 3. Владелец обязан быть и в chat_users, иначе он не увидит чат
        #    в «Моих чатах», пока сам не напишет в группе.
        if owner_id is not None:
            try:
                await queries.link_chat_owner(db, chat.id, owner_id)
            except Exception as exc:  # noqa: BLE001 - чат и так уже сохранён
                error_handler.log_exception("связывании владельца с участниками чата", exc)

        logger.info(
            "Бот добавлен в чат %s (%s). Владелец: %s / канал %s",
            chat.id,
            chat.title,
            owner_id,
            owner_channel_id,
        )
        await bot.send_message(chat.id, BOT_ADDED_GREETING)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("добавлении бота в чат", exc)