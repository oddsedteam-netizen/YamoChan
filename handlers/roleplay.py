"""Ролевые команды: обычные и 18+ (NSFW), доступные всем участникам чата.

Модуль не знает про модерацию: у RP-команд свои имена, поэтому фильтр команд
модерации их не перехватывает. Логика такая:

    * :data:`NORMAL_RP_COMMANDS` и :data:`NSFW_RP_COMMANDS` — словари
      ``"команда": ("эмодзи", "действие")``;
    * :class:`RoleplayFilter` — разбирает текст и пускает дальше только
      известные RP-команды (они работают лишь с префиксом ``.``, ``/`` или
      ``!``: без префикса обычная речь вроде «я обнял её» командой не считается);
    * один хендлер отвечает за все команды и работает по реплаю,
      ``@юзернейму`` или ID цели;
    * NSFW-команды включаются настройкой чата ``nsfw_commands``.

Настройка 18+ команд (кнопка в меню настроек чата) живёт здесь же:
префикс ``callback_data`` — ``nsfw:``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from database import queries
from database.db import Database
from database.models import ChatInfo, UserProfile
from keyboards import inline
from services import permissions, profile as profile_service
from utils import command_filter, error_handler, html_utils, telegram

logger = logging.getLogger(__name__)

#: Роутер ролевых команд.
router: Final[Router] = Router(name="roleplay")

#: Групповые типы чатов, где работают RP-команды.
GROUP_CHAT_TYPES: Final[set[str]] = {ChatType.GROUP, ChatType.SUPERGROUP}

#: Обычные ролевые команды: ``команда → (эмодзи, действие)``.
NORMAL_RP_COMMANDS: dict[str, tuple[str, str]] = {
    "обнять": ("🤗", "обнял(а)"),
    "погладить": ("🥰", "погладил(а)"),
    "поцеловать": ("😘", "поцеловал(а)"),
    "пнуть": ("🦵", "пнул(а)"),
    "ударить": ("👊", "ударил(а)"),
    "укусить": ("😈", "укусил(а)"),
    "лизнуть": ("👅", "лизнул(а)"),
    "потыкать": ("👉", "потыкал(а)"),
    "шлёпнуть": ("🫲", "шлёпнул(а)"),
    "обидеть": ("😢", "обидел(а)"),
    "утешить": ("🫂", "утешил(а)"),
    "подмигнуть": ("😉", "подмигнул(а)"),
    "приласкать": ("💕", "приласкал(а)"),
    "толкнуть": ("😤", "толкнул(а)"),
    "щекотать": ("🤣", "пощекотал(а)"),
    "напугать": ("👻", "напугал(а)"),
    "накормить": ("🍰", "накормил(а)"),
    "напоить": ("🍵", "напоил(а)"),
    "спасти": ("🦸", "спас(ла)"),
    "защитить": ("🛡", "защитил(а)"),
    "похвалить": ("⭐", "похвалил(а)"),
    "поблагодарить": ("🙏", "поблагодарил(а)"),
    "извиниться": ("🥺", "извинился(ась) перед"),
    "подарить": ("🎁", "подарил(а) подарок"),
    "танцевать": ("💃", "танцует с"),
    "спеть": ("🎤", "спел(а) песню для"),
    "нарисовать": ("🎨", "нарисовал(а) портрет"),
    "сфоткать": ("📸", "сфоткал(а)"),
    "выпить": ("🍻", "выпил(а) с"),
    "покурить": ("🚬", "покурил(а) с"),
    "поспать": ("😴", "уснул(а) рядом с"),
    "разбудить": ("⏰", "разбудил(а)"),
    "связать": ("🪢", "связал(а)"),
    "развязать": ("✂️", "развязал(а)"),
    "арестовать": ("🚔", "арестовал(а)"),
    "освободить": ("🔓", "освободил(а)"),
    "кинуть": ("🤾", "кинул(а)"),
    "поймать": ("🫴", "поймал(а)"),
    "прижать": ("😏", "прижал(а) к стенке"),
    "оттолкнуть": ("🙅", "оттолкнул(а)"),
    "понюхать": ("👃", "понюхал(а)"),
    "плюнуть": ("💦", "плюнул(а) в"),
    "дать5": ("🖐", "дал(а) пять"),
    "бросить": ("🗑", "бросил(а)"),
    "женить": ("💒", "женился/вышла замуж за"),
    "развестись": ("💔", "развёлся/развелась с"),
    "усыновить": ("👶", "усыновил(а)"),
    "наказать": ("⚡", "наказал(а)"),
    "простить": ("💝", "простил(а)"),
    "ущипнуть": ("🤏", "ущипнул(а)"),
}

#: 18+ ролевые команды: включаются настройкой ``nsfw_commands``.
NSFW_RP_COMMANDS: dict[str, tuple[str, str]] = {
    "трахнуть": ("🔞", "трахнул(а)"),
    "отсосать": ("🔞", "отсосал(а) у"),
    "вылизать": ("🔞", "вылизал(а)"),
    "изнасиловать": ("🔞", "изнасиловал(а)"),
    "кончить": ("🔞", "кончил(а) на"),
    "раздеть": ("🔞", "раздел(а)"),
    "связатьбдсм": ("🔞", "связал(а) в БДСМ"),
    "отшлёпать": ("🔞", "отшлёпал(а) по попке"),
    "засунуть": ("🔞", "засунул(а) пальцы в"),
    "оседлать": ("🔞", "оседлал(а)"),
    "придушить": ("🔞", "придушил(а) во время секса"),
    "отыметь": ("🔞", "отымел(а)"),
    "сосать": ("🔞", "сосёт у"),
    "дрочить": ("🔞", "дрочит на"),
    "фистинг": ("🔞", "сделал(а) фистинг"),
    "анал": ("🔞", "сделал(а) анал с"),
    "69": ("🔞", "сделал(а) 69 с"),
    "минет": ("🔞", "сделал(а) минет"),
    "куни": ("🔞", "сделал(а) куни"),
    "сквирт": ("🔞", "довёл(а) до сквирта"),
}

#: Все RP-команды вместе (для фильтра и проверок коллизий).
RP_COMMANDS: Final[dict[str, tuple[str, str]]] = {
    **NORMAL_RP_COMMANDS,
    **NSFW_RP_COMMANDS,
}

#: Цель не указана вовсе.
NO_TARGET_TEXT: Final[str] = "А кого?~ 🤔"
#: Цель указана, но пользователя нет в базе бота.
UNKNOWN_TARGET_TEXT: Final[str] = "Не могу найти этого пользователя~ 🤔"
#: 18+ команды выключены для этого чата.
NSFW_DISABLED_TEXT: Final[str] = (
    "🔞 18+ команды отключены в этом чате~ Попроси владельца включить в настройках!"
)


# ---------------------------------------------------------------------------
# Разбор команды
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ParsedRoleplay:
    """Разобранная RP-команда."""

    name: str
    args: list[str]


def parse_roleplay(text: Optional[str]) -> Optional[ParsedRoleplay]:
    """Разобрать текст сообщения в RP-команду.

    RP-команды работают только с префиксом (``.обнять``, ``/обнять``,
    ``!обнять``): без префикса обычная речь («я обнял её») срабатывала бы
    как команда.

    :param text: например ``".обнять @user"`` или ``"!69 5"``.
    :returns: :class:`ParsedRoleplay` или ``None``, если это не RP-команда.
    """
    command = command_filter.split_command(text, require_prefix=True)
    if command is None:
        return None
    name, tail = command
    if name not in RP_COMMANDS:
        return None
    return ParsedRoleplay(name=name, args=[part for part in tail.split() if part])


class RoleplayFilter(BaseFilter):
    """Фильтр: сообщение является известной RP-командой (только с префиксом).

    За распознавание префиксов отвечает
    :class:`yamochan.utils.command_filter.RPCommand`.
    """

    def __init__(self) -> None:
        """Собрать фильтр по словарю RP-команд."""
        self._rp: command_filter.RPCommand = command_filter.RPCommand(RP_COMMANDS)

    async def __call__(self, message: Message) -> Any:
        """Разобрать текст и передать команду в хендлер."""
        if not await self._rp(message):
            return False
        parsed = parse_roleplay(message.text)
        if parsed is None:
            return False
        return {"command": parsed}


# ---------------------------------------------------------------------------
# Цель и текст действия
# ---------------------------------------------------------------------------
def _user_name(user: Any) -> str:
    """Имя пользователя из Telegram (для отправителя)."""
    return getattr(user, "full_name", None) or getattr(user, "username", None) or str(user.id)


def _profile_name(profile: Optional[UserProfile], fallback: str) -> str:
    """Имя из сохранённого профиля или запасное."""
    if profile is not None and profile.display_name:
        return profile.display_name
    return fallback


async def resolve_target(
    message: Message,
    args: list[str],
    db: Database,
) -> tuple[Optional[tuple[int, str]], Optional[str]]:
    """Определить цель команды: реплай → ``@юзернейм`` → ID.

    :param message: сообщение с командой.
    :param args: аргументы после команды.
    :param db: соединение с базой данных.
    :returns: ``((user_id, имя), None)`` или ``(None, текст_ошибки)``.
    """
    reply = message.reply_to_message
    if reply is not None:
        reply_user = reply.from_user
        if reply_user is None or reply_user.is_bot:
            return None, UNKNOWN_TARGET_TEXT
        profile = await queries.get_user(db, reply_user.id)
        return (reply_user.id, _profile_name(profile, _user_name(reply_user))), None

    if not args:
        return None, NO_TARGET_TEXT

    candidate = args[0].strip()
    if candidate.startswith("@"):
        profile = await queries.get_user_by_username(db, candidate)
        if profile is None:
            return None, UNKNOWN_TARGET_TEXT
        return (profile.user_id, profile.display_name), None

    if candidate.lstrip("-").isdigit():
        profile = await queries.get_user(db, int(candidate))
        if profile is None:
            return None, UNKNOWN_TARGET_TEXT
        return (profile.user_id, profile.display_name), None

    return None, NO_TARGET_TEXT


def build_action_text(
    emoji: str,
    action: str,
    sender_id: int,
    sender_name: str,
    target_id: int,
    target_name: str,
) -> str:
    """Собрать HTML-строку действия со ссылками на обоих участников."""
    sender_link = profile_service.user_mention(sender_name, sender_id)
    target_link = profile_service.user_mention(target_name, target_id)
    return f"{emoji} {sender_link} {action} {target_link}~"


# ---------------------------------------------------------------------------
# Обработчик команд
# ---------------------------------------------------------------------------
@router.message(F.chat.type.in_(GROUP_CHAT_TYPES), RoleplayFilter())
async def handle_roleplay(
    message: Message,
    command: ParsedRoleplay,
    db: Database,
) -> None:
    """Выполнить RP-команду (доступна всем участникам чата).

    :param message: сообщение с командой.
    :param command: разобранная команда (прокидывается фильтром).
    :param db: соединение с базой данных.
    """
    try:
        sender = message.from_user
        if sender is None or sender.is_bot:
            return

        await queries.ensure_user(db, sender.id, sender.username, sender.first_name)
        sender_profile = await queries.get_user(db, sender.id)
        sender_name = _profile_name(sender_profile, _user_name(sender))

        # 18+ команды доступны только при включённой настройке чата.
        if command.name in NSFW_RP_COMMANDS:
            settings = await queries.get_chat_settings(db, message.chat.id)
            if not settings.get("nsfw_commands"):
                await message.reply(NSFW_DISABLED_TEXT)
                return

        target, error = await resolve_target(message, command.args, db)
        if target is None:
            await message.reply(error or UNKNOWN_TARGET_TEXT)
            return

        emoji, action = RP_COMMANDS[command.name]
        await message.answer(
            build_action_text(emoji, action, sender.id, sender_name, target[0], target[1]),
            parse_mode=ParseMode.HTML,
        )
        logger.info(
            "RP-команда «.%s»: %s → %s в чате %s",
            command.name,
            sender.id,
            target[0],
            message.chat.id,
        )
    except Exception as exc:  # noqa: BLE001 - бот не должен падать
        error_handler.log_exception(f"RP-команде «.{command.name}»", exc)
        await error_handler.notify_user_softly(message)


# ---------------------------------------------------------------------------
# Настройка 18+ команд (кнопка «🔞 18+ команды» в настройках чата)
# ---------------------------------------------------------------------------
def _chat_id_from(raw: Optional[str]) -> int:
    """Разобрать идентификатор чата из ``callback_data``."""
    cleaned = (raw or "").strip()
    return int(cleaned) if cleaned.lstrip("-").isdigit() else 0


def nsfw_entries() -> list[tuple[str, str, str]]:
    """Список ``(команда, эмодзи, действие)`` для экрана «Список 18+ команд»."""
    return [
        (name, emoji, action)
        for name, (emoji, action) in sorted(NSFW_RP_COMMANDS.items())
    ]


async def _render(callback: CallbackQuery, text: str, markup=None) -> None:
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
                error_handler.log_exception("отправке экрана 18+ команд", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:  # noqa: BLE001 - экран не важнее работы бота
        error_handler.log_exception("обновлении экрана 18+ команд", exc)


async def _owner_chat(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> Optional[ChatInfo]:
    """Проверить, что кнопку нажал владелец чата, и вернуть чат."""
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


async def _show_nsfw_menu(
    callback: CallbackQuery,
    db: Database,
    enabled: bool,
    note: Optional[str] = None,
) -> None:
    """Показать экран настройки 18+ команд."""
    await _render(
        callback,
        profile_service.build_nsfw_menu_text(enabled),
        inline.nsfw_keyboard(_chat_id_from((callback.data or "").split(":")[-1]), enabled),
    )
    await callback.answer(note or "")


@router.callback_query(F.data.startswith(inline.CB_NSFW))
async def on_nsfw_callbacks(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Включить/выключить 18+ команды и показать их список.

    Поддерживаемые форматы ``callback_data``:

        * ``nsfw:menu:{chat_id}`` — открыть экран настройки;
        * ``nsfw:enable:{chat_id}`` / ``nsfw:disable:{chat_id}`` — переключить;
        * ``nsfw:list:{chat_id}`` — список всех 18+ команд.
    """
    try:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        chat_id = _chat_id_from(parts[-1])
        chat = await _owner_chat(callback, db, bot, chat_id)
        if chat is None:
            return

        settings = await queries.get_chat_settings(db, chat_id)
        enabled = bool(settings.get("nsfw_commands"))

        if action == "list":
            await _render(
                callback,
                profile_service.build_nsfw_list_text(nsfw_entries()),
                inline.nsfw_list_keyboard(chat_id),
            )
            await callback.answer()
            return

        if action in {"enable", "disable"}:
            enabled = action == "enable"
            await queries.update_chat_setting(db, chat_id, "nsfw_commands", enabled)
            logger.info("18+ команды в чате %s: %s", chat_id, enabled)
            await _show_nsfw_menu(
                callback,
                db,
                enabled,
                "18+ команды включены ✅" if enabled else "18+ команды выключены ❌",
            )
            return

        await _show_nsfw_menu(callback, db, enabled)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("настройке 18+ команд", exc)
        await error_handler.notify_user_softly(callback)
