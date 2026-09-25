"""Админ-панель владельца бота: команда ``/adm``, экраны и действия.

Команда входа реагирует на ``/adm``, ``.adm``, ``!adm`` и просто «adm»
(а также ``admin``, ``адм``, ``админ``) — за распознавание отвечает
:class:`yamochan.utils.command_filter.FlexCommand`. Работает панель **только**
для ``config.BOT_OWNER_ID``: остальным бот молча не отвечает.

Все инлайн-кнопки панели имеют префикс ``adm:`` и маршрутизируются одним
обработчиком :func:`on_admin_callbacks` по частям ``callback_data``.
Расширенная статистика, тексты и сами действия (отвязка чата, бан, рассылка)
живут в :mod:`yamochan.services.admin`; жалобы — в
:mod:`yamochan.services.complaints`.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Final, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

import config
from ..database import queries
from ..database.db import Database
from ..database.models import UserProfile, escape_text, utcnow
from ..keyboards import inline
from ..services import admin as admin_service
from ..services import antiraid as antiraid_service
from ..services import complaints as complaints_service
from ..services import permissions, richtext
from ..utils import command_filter, error_handler, html_utils, telegram

logger = logging.getLogger(__name__)

#: Роутер админ-панели владельца бота.
router: Final[Router] = Router(name="admin_panel")

#: Слова, которыми админ отменяет ввод.
CANCEL_WORDS: Final[frozenset[str]] = frozenset({"/cancel", "отмена", "cancel"})

#: Фильтры раздела «Пользователи» → ``(заголовок, параметры выборки)``.
USER_FILTERS: Final[dict[str, tuple[str, dict[str, Any]]]] = {
    "badrep": (
        "🔴 Плохая репутация",
        {"max_reputation": config.BAD_REPUTATION_THRESHOLD},
    ),
    "spammers": ("🚫 Спамеры", {"is_spammer": True}),
    "gbanned": ("🔨 Глобально забанены", {"is_globally_banned": True}),
}


class AdminStates(StatesGroup):
    """Состояния ввода админ-панели."""

    waiting_search = State()
    waiting_complaint_search = State()
    waiting_broadcast = State()
    waiting_user_message = State()


def is_owner(user_id: Optional[int]) -> bool:
    """Является ли пользователь владельцем бота (он же супер-админ панели)."""
    owner_id = int(config.BOT_OWNER_ID or 0)
    return owner_id > 0 and user_id is not None and int(user_id) == owner_id


async def _render(
    callback: CallbackQuery,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Показать новый экран вместо текущего сообщения.

    Текст чинится :func:`yamochan.utils.html_utils.safe_html_text`, а показ
    экрана делает :func:`yamochan.utils.telegram.render_screen`: панель не
    ломается из-за одного неэкранированного ``<`` в данных.
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
                error_handler.log_exception("отправке экрана админ-панели", exc)
        return
    try:
        await telegram.render_screen(message, text, markup)
    except Exception as exc:  # noqa: BLE001 - панель не важнее работы бота
        error_handler.log_exception("обновлении экрана админ-панели", exc)


def _parse_int(raw: str, default: int = 0) -> int:
    """Безопасно разобрать целое число из ``callback_data``."""
    cleaned = (raw or "").strip()
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return default


# ---------------------------------------------------------------------------
# Вспомогательные данные
# ---------------------------------------------------------------------------
async def _open_complaints(db: Database) -> int:
    """Сколько жалоб сейчас открыто (для подписи кнопки панели)."""
    try:
        return await queries.count_complaints(db, status=config.COMPLAINT_STATUS_OPEN)
    except Exception:  # noqa: BLE001 - подпись кнопки не важнее панели
        logger.error("Не удалось посчитать открытые жалобы", exc_info=True)
        return 0


async def _user_card_payload(
    db: Database,
    user_id: int,
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """Собрать текст и клавиатуру админского профиля пользователя.

    :returns: пару ``(текст, клавиатура)`` или ``None``, если пользователя нет.
    """
    profile = await queries.get_user(db, user_id)
    if profile is None:
        return None
    stats = await queries.get_user_global_stats(db, user_id)
    chats = await queries.get_user_chats(db, user_id)
    counts = await queries.get_user_complaint_counts(db, user_id)
    ban = await queries.get_admin_ban(db, user_id)
    text = admin_service.build_admin_user_profile_text(profile, stats, chats, counts, ban)
    markup = inline.admin_user_card_keyboard(
        user_id,
        has_complaints=int(counts.get("total", 0)) > 0,
        bot_banned=ban is not None,
        globally_banned=bool(profile.is_globally_banned),
    )
    return text, markup


# ---------------------------------------------------------------------------
# Экраны панели
# ---------------------------------------------------------------------------
async def _show_panel(callback: CallbackQuery, db: Database) -> None:
    """Главный экран админ-панели со сводной статистикой."""
    stats = await admin_service.collect_global_stats(db)
    await _render(
        callback,
        admin_service.build_panel_text(stats),
        inline.admin_panel_keyboard(stats.get("open_complaints", 0)),
    )


async def _show_chats(callback: CallbackQuery, db: Database, page: int = 1) -> None:
    """Список подключённых чатов с пагинацией."""
    overviews, total, pages = await admin_service.collect_chat_overviews(db, page=page)
    page = admin_service.clamp_page(page, pages)
    items = [(item.chat.chat_id, item.chat.display_title) for item in overviews]
    await _render(
        callback,
        admin_service.build_chats_list_text(overviews, total, page, pages),
        inline.admin_chats_keyboard(items, page, pages),
    )


async def _show_chat_card(
    callback: CallbackQuery,
    db: Database,
    chat_id: int,
    page: int = 1,
) -> None:
    """Карточка конкретного чата."""
    chat = await queries.get_chat(db, chat_id)
    if chat is None:
        await callback.answer("Этот чат мне ещё не знаком~ 🌸", show_alert=True)
        await _show_chats(callback, db, page)
        return
    stats = await queries.get_chat_stats(db, chat_id)
    settings = await queries.get_chat_settings(db, chat_id)
    owner = await queries.get_user(db, chat.owner_id) if chat.owner_id else None
    await _render(
        callback,
        admin_service.build_chat_card_text(chat, stats, settings, owner),
        inline.admin_chat_card_keyboard(chat_id, page),
    )


async def _confirm_detach(callback: CallbackQuery, db: Database, chat_id: int) -> None:
    """Показать подтверждение отвязки чата."""
    chat = await queries.get_chat(db, chat_id)
    if chat is None:
        await callback.answer("Этот чат мне ещё не знаком~ 🌸", show_alert=True)
        await _show_chats(callback, db)
        return
    await _render(
        callback,
        admin_service.build_detach_confirm_text(chat.display_title),
        inline.admin_detach_confirm_keyboard(chat_id),
    )


async def _apply_detach(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    chat_id: int,
) -> None:
    """Отвязать чат и показать результат."""
    chat = await queries.get_chat(db, chat_id)
    actor = callback.from_user.id if callback.from_user else None
    await admin_service.detach_chat(
        bot,
        db,
        chat_id,
        title=chat.display_title if chat is not None else None,
        owner_id=chat.owner_id if chat is not None else None,
        actor_id=actor,
    )
    open_complaints = await _open_complaints(db)
    await _render(
        callback,
        admin_service.build_detached_done_text(),
        inline.admin_panel_keyboard(open_complaints),
    )


async def _show_users(callback: CallbackQuery, db: Database) -> None:
    """Раздел «Пользователи»: счётчики и кнопки-фильтры."""
    total = await queries.count_users(db)
    bad_reputation = await queries.count_users_by_filter(
        db, max_reputation=config.BAD_REPUTATION_THRESHOLD
    )
    spammers = await queries.count_users_by_filter(db, is_spammer=True)
    globally_banned = await queries.count_users_by_filter(db, is_globally_banned=True)
    await _render(
        callback,
        admin_service.build_users_text(total, bad_reputation, spammers, globally_banned),
        inline.admin_users_keyboard(),
    )


async def _show_user_list(
    callback: CallbackQuery,
    db: Database,
    section: str,
    page: int = 1,
) -> None:
    """Список пользователей по фильтру (``badrep``, ``spammers``, ``gbanned``)."""
    title, filters = USER_FILTERS.get(section, USER_FILTERS["badrep"])
    total = await queries.count_users_by_filter(db, **filters)
    pages = admin_service.page_count(total, config.ADMIN_PAGE_SIZE)
    page = admin_service.clamp_page(page, pages)
    users = await queries.get_users_by_filter(
        db,
        limit=config.ADMIN_PAGE_SIZE,
        offset=(page - 1) * config.ADMIN_PAGE_SIZE,
        **filters,
    )
    items = [(user.user_id, user.display_name) for user in users]
    await _render(
        callback,
        admin_service.build_user_list_text(title, users, page, pages),
        inline.admin_user_list_keyboard(section, items, page, pages),
    )


async def _show_user_card(callback: CallbackQuery, db: Database, user_id: int) -> None:
    """Админский профиль пользователя."""
    payload = await _user_card_payload(db, user_id)
    if payload is None:
        await callback.answer(admin_service.build_search_not_found_text(), show_alert=True)
        await _show_users(callback, db)
        return
    await _render(callback, payload[0], payload[1])


async def _show_stats(callback: CallbackQuery, db: Database) -> None:
    """Расширенная статистика с «графиком» активности."""
    now = utcnow()
    week_ago = now - timedelta(days=config.ADMIN_ACTIVITY_DAYS)
    day_ago = now - timedelta(hours=24)

    daily = await queries.get_daily_message_counts(db, week_ago)
    top_chat_rows = await queries.get_top_chats_since(db, day_ago, config.ADMIN_TOP_LIMIT)
    top_chats: list[tuple[str, int]] = []
    for chat_id, count in top_chat_rows:
        chat = await queries.get_chat(db, chat_id)
        top_chats.append(
            (chat.display_title if chat is not None else f"Чат {chat_id}", count)
        )

    top_user_rows = await queries.get_top_users_by_messages(db, config.ADMIN_TOP_LIMIT)
    profiles = await queries.get_users_by_ids(db, [user_id for user_id, _ in top_user_rows])
    top_users = [
        (
            profiles[user_id].display_name if user_id in profiles else f"ID {user_id}",
            count,
        )
        for user_id, count in top_user_rows
    ]

    punishment_counts = await queries.get_punishment_counts_since(db, week_ago)
    warns_count = await queries.count_warns_since(db, week_ago)
    manager = antiraid_service.antiraid_manager
    await _render(
        callback,
        admin_service.build_stats_text(
            daily,
            top_chats,
            top_users,
            punishment_counts,
            warns_count,
            int(getattr(manager, "antiraid_hits", 0)),
            int(getattr(manager, "antispam_hits", 0)),
        ),
        inline.admin_stats_keyboard(),
    )


async def _show_banned(callback: CallbackQuery, db: Database, page: int = 1) -> None:
    """Список забаненных ботом пользователей."""
    items, total, pages = await admin_service.collect_banned(db, page=page)
    page = admin_service.clamp_page(page, pages)
    buttons = [
        (ban.user_id, profile.display_name if profile else f"ID {ban.user_id}")
        for ban, profile in items
    ]
    await _render(
        callback,
        admin_service.build_banned_list_text(items, total, page, pages),
        inline.admin_banned_keyboard(buttons, page, pages),
    )


async def _show_log(callback: CallbackQuery, db: Database, page: int = 1) -> None:
    """Журнал действий владельца бота."""
    entries, total, pages = await admin_service.collect_admin_log(db, page=page)
    page = admin_service.clamp_page(page, pages)
    await _render(
        callback,
        admin_service.build_log_text(entries, total, page, pages),
        inline.admin_log_keyboard(page, pages),
    )


async def _show_broadcast(callback: CallbackQuery, db: Database) -> None:
    """Экран «Рассылка»: выбор получателей."""
    chats = await queries.count_chats(db)
    owners = len(await queries.get_owner_ids(db))
    await _render(
        callback,
        admin_service.build_broadcast_menu_text(),
        inline.admin_broadcast_keyboard(chats, owners),
    )


async def _broadcast_targets(db: Database, kind: str) -> list[int]:
    """Получатели рассылки: чаты или личные сообщения владельцев."""
    if kind == "owners":
        return await queries.get_owner_ids(db)
    return await queries.get_known_chat_ids(db)


async def _show_complaints_section(callback: CallbackQuery, db: Database) -> None:
    """Раздел «Жалобы»: статистика и кнопки списков по статусам."""
    stats = await queries.count_complaints_by_status(db)
    await _render(
        callback,
        complaints_service.build_section_text(stats),
        inline.admin_complaints_section_keyboard(
            stats.get(config.COMPLAINT_STATUS_OPEN, 0),
            stats.get(config.COMPLAINT_STATUS_ACCEPTED, 0),
            stats.get(config.COMPLAINT_STATUS_REJECTED, 0),
        ),
    )


#: Заголовки списков жалоб по статусу.
COMPLAINT_LIST_TITLES: Final[dict[str, str]] = {
    config.COMPLAINT_STATUS_OPEN: "⏳ Открытые жалобы",
    config.COMPLAINT_STATUS_ACCEPTED: "✅ Принятые жалобы",
    config.COMPLAINT_STATUS_REJECTED: "❌ Отклонённые жалобы",
}


async def _show_complaint_list(
    callback: CallbackQuery,
    db: Database,
    kind: str,
    page: int = 1,
) -> None:
    """Список жалоб по статусу с пагинацией.

    :param kind: ``open`` / ``accepted`` / ``rejected``.
    :param page: номер страницы (1-based).
    """
    status = kind if kind in COMPLAINT_LIST_TITLES else config.COMPLAINT_STATUS_OPEN
    total = await queries.count_complaints(db, status=status)
    pages = admin_service.page_count(total, config.ADMIN_PAGE_SIZE)
    page = admin_service.clamp_page(page, pages)
    complaints = await queries.get_complaints(
        db,
        status=status,
        limit=config.ADMIN_PAGE_SIZE,
        offset=(page - 1) * config.ADMIN_PAGE_SIZE,
    )
    await _render(
        callback,
        complaints_service.build_list_text(
            COMPLAINT_LIST_TITLES[status], complaints, page, pages
        ),
        inline.admin_complaint_list_keyboard(
            status, [int(item.id or 0) for item in complaints], page, pages
        ),
    )


async def _show_quick_actions(callback: CallbackQuery) -> None:
    """Показать подменю «Быстрые действия»."""
    await _render(
        callback,
        admin_service.build_quick_actions_text(),
        inline.admin_quick_actions_keyboard(),
    )


async def _reset_admin_cache(callback: CallbackQuery, db: Database) -> None:
    """Сбросить кэш администраторов чатов."""
    cached = len(permissions.cached_admin_chats())
    permissions.clear_admin_cache()
    await queries.log_admin_action(db, "reset_admin_cache", None, f"чатов в кэше: {cached}")
    await _render(
        callback,
        admin_service.build_cache_reset_text(cached),
        inline.admin_quick_actions_keyboard(),
    )


async def _refresh_counters(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Пересчитать счётчики варнов и число участников чатов."""
    result = await admin_service.refresh_counters(bot, db)
    await _render(
        callback,
        admin_service.build_counters_refreshed_text(result),
        inline.admin_quick_actions_keyboard(),
    )


async def _cleanup_logs(callback: CallbackQuery, db: Database) -> None:
    """Удалить старые записи журнала действий."""
    removed = await queries.cleanup_admin_log(db, config.ADMIN_LOG_TTL_DAYS)
    await queries.log_admin_action(db, "cleanup_logs", None, f"удалено: {removed}")
    await _render(
        callback,
        admin_service.build_logs_cleared_text(removed),
        inline.admin_quick_actions_keyboard(),
    )


async def _send_backup(callback: CallbackQuery, db: Database) -> None:
    """Отправить владельцу файл резервной копии базы данных."""
    message: Optional[Message] = (
        callback.message if isinstance(callback.message, Message) else None
    )
    if message is None:
        await callback.answer("Не удалось получить сообщение~ 🤔", show_alert=True)
        return
    path = config.DB_PATH
    try:
        if not path.exists():
            raise FileNotFoundError(f"файл базы не найден: {path}")
        document = FSInputFile(str(path), filename=admin_service.backup_filename())
        await message.answer_document(document, caption=admin_service.build_backup_caption())
        await queries.log_admin_action(db, "backup_db", None, path.name)
        await callback.answer("✅ Бэкап отправлен!")
    except Exception as exc:  # noqa: BLE001 - бэкап не должен ронять панель
        error_handler.log_exception("бэкапе базы данных", exc)
        await callback.answer(admin_service.build_backup_error_text(), show_alert=True)


async def _panel_payload(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура главного экрана панели (для ответов на сообщения)."""
    stats = await admin_service.collect_global_stats(db)
    return (
        admin_service.build_panel_text(stats),
        inline.admin_panel_keyboard(stats.get("open_complaints", 0)),
    )


# ---------------------------------------------------------------------------
# Вход в панель и маршрутизация кнопок
# ---------------------------------------------------------------------------
@router.message(
    command_filter.FlexCommand(*config.ADMIN_COMMANDS),
    F.chat.type == ChatType.PRIVATE,
)
async def cmd_admin_panel(message: Message, state: FSMContext, db: Database) -> None:
    """Открыть админ-панель: ``/adm``, ``.adm``, ``!admin`` или «админ» в ЛС.

    Команда реагирует только на ``config.BOT_OWNER_ID``; остальным бот молча
    не отвечает, чтобы не раскрывать существование панели.
    """
    user = message.from_user
    if user is None:
        return
    if not is_owner(user.id):
        logger.info(
            "[ADMIN] Отказ: пользователь %s не владелец (BOT_OWNER_ID=%s).",
            user.id,
            config.BOT_OWNER_ID,
        )
        return
    try:
        await state.clear()
        text, markup = await _panel_payload(db)
        await message.answer(text, reply_markup=markup)
        logger.info("Владелец %s открыл админ-панель.", user.id)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("открытии админ-панели", exc)
        await error_handler.notify_user_softly(message)


@router.callback_query(F.data.startswith(inline.CB_ADM))
async def on_admin_callbacks(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Обработать все кнопки админ-панели (``callback_data`` = ``adm:…``)."""
    user = callback.from_user
    if user is None or not is_owner(user.id):
        # Молча игнорируем: панель существует только для владельца бота.
        await callback.answer()
        return
    try:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else "panel"
        args = parts[2:]
        if not await _dispatch(callback, state, db, bot, action, args):
            logger.info("Неизвестная кнопка админ-панели: %r", callback.data)
            await callback.answer("Не поняла кнопку~ 🤔", show_alert=True)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("кнопке админ-панели", exc)
        await error_handler.notify_user_softly(callback)


async def _dispatch(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    action: str,
    args: list[str],
) -> bool:
    """Развести нажатие кнопки панели по экранам и действиям.

    :returns: ``True``, если действие обработано.
    """
    first = _parse_int(args[0]) if args else 0
    actor_id = callback.from_user.id if callback.from_user else 0

    if action == "panel":
        await _show_panel(callback, db)
    elif action == "chats":
        await _show_chats(callback, db, first or 1)
    elif action == "chat" and args:
        page = _parse_int(args[1], 1) if len(args) > 1 else 1
        await _show_chat_card(callback, db, first, page)
    elif action == "user_from_chat" and args:
        await _open_chat_owner(callback, db, first)
    elif action == "detach" and args:
        await _confirm_detach(callback, db, first)
    elif action == "detach_ok" and args:
        await _apply_detach(callback, db, bot, first)
    elif action == "users":
        await _show_users(callback, db)
    elif action in USER_FILTERS:
        await _show_user_list(callback, db, action, first or 1)
    elif action == "search":
        await state.clear()
        await state.set_state(AdminStates.waiting_search)
        await _render(
            callback,
            admin_service.build_search_prompt_text(),
            inline.admin_prompt_keyboard(f"{inline.CB_ADM}panel"),
        )
    elif action == "user" and args:
        await _show_user_card(callback, db, first)
    elif action == "ban_user" and args:
        await _confirm_ban(callback, db, first)
    elif action == "ban_owner" and args:
        await _confirm_owner_ban(callback, db, first)
    elif action == "ban_reason" and len(args) > 1:
        await _apply_ban(callback, db, bot, first, args[1])
        return True
    elif action == "unban_user" and args:
        await _apply_unban(callback, db, bot, first)
    elif action == "gban" and args:
        await _toggle_global_ban(callback, db, first)
    elif action == "msg_user" and args:
        await state.clear()
        await state.update_data(target_id=first)
        await state.set_state(AdminStates.waiting_user_message)
        profile = await queries.get_user(db, first)
        name = profile.display_name if profile is not None else f"ID {first}"
        await _render(
            callback,
            f"📨 Сообщение пользователю {escape_text(name)}\n\n"
            "Напиши текст — я отправлю ему в ЛС от своего имени.\n"
            "Можно с фото, форматированием и премиум эмодзи.\n\n"
            "Для отмены отправь /cancel",
            inline.admin_prompt_keyboard(f"{inline.CB_ADM}user:{first}"),
        )
    elif action == "stats":
        await _show_stats(callback, db)
    elif action == "banned":
        await _show_banned(callback, db, first or 1)
    elif action == "log":
        await _show_log(callback, db, first or 1)
    elif action == "broadcast":
        await state.clear()
        await _show_broadcast(callback, db)
    elif action in {"broadcast_chats", "broadcast_owners"}:
        kind = "owners" if action.endswith("owners") else "chats"
        await state.clear()
        await state.update_data(kind=kind)
        await state.set_state(AdminStates.waiting_broadcast)
        await _render(
            callback,
            admin_service.build_broadcast_prompt_text(),
            inline.admin_prompt_keyboard(f"{inline.CB_ADM}broadcast_cancel"),
        )
    elif action == "broadcast_cancel":
        await state.clear()
        await _render(
            callback,
            admin_service.build_broadcast_cancelled_text(),
            inline.admin_panel_keyboard(await _open_complaints(db)),
        )
    elif action == "broadcast_send":
        await _run_broadcast(callback, state, db, bot, actor_id)
    elif action == "complaints":
        if args and args[0] in COMPLAINT_LIST_TITLES:
            page = _parse_int(args[1], 1) if len(args) > 1 else 1
            await _show_complaint_list(callback, db, args[0], page)
        elif args and args[0].isdigit():
            await _show_complaint_list(callback, db, config.COMPLAINT_STATUS_OPEN, first)
        else:
            await _show_complaints_section(callback, db)
    elif action == "complaint_search":
        await state.clear()
        await state.set_state(AdminStates.waiting_complaint_search)
        await _render(
            callback,
            "🔍 Поиск жалобы по ID\n\n"
            "Отправь номер жалобы числом (например <code>45</code>).\n\n"
            "Для отмены отправь /cancel",
            inline.admin_prompt_keyboard(f"{inline.CB_ADM}complaints"),
        )
    elif action in {"quick", "backup"}:
        await _dispatch_quick(callback, db, bot, args, action)
    else:
        return False

    await callback.answer()
    return True


async def _dispatch_quick(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    args: list[str],
    action: str,
) -> None:
    """Обработать подменю «Быстрые действия».

    :param callback: нажатие кнопки.
    :param db: соединение с базой данных.
    :param bot: экземпляр бота.
    :param args: аргументы ``callback_data`` (после ``adm:quick``).
    :param action: ``quick`` или короткое имя операции (``backup``).
    """
    operation = args[0] if args else action
    if operation == "cache":
        await _reset_admin_cache(callback, db)
    elif operation == "counters":
        await _refresh_counters(callback, db, bot)
    elif operation == "logs":
        await _cleanup_logs(callback, db)
    elif operation == "backup":
        await _send_backup(callback, db)
    else:
        await _show_quick_actions(callback)


# ---------------------------------------------------------------------------
# Действия: владелец чата и бан в боте
# ---------------------------------------------------------------------------
async def _open_chat_owner(callback: CallbackQuery, db: Database, chat_id: int) -> None:
    """Открыть профиль владельца из карточки чата."""
    chat = await queries.get_chat(db, chat_id)
    if chat is None or not chat.owner_id:
        await callback.answer("Владелец этого чата не определён~ 🤔", show_alert=True)
        return
    await _show_user_card(callback, db, int(chat.owner_id))


async def _confirm_ban(callback: CallbackQuery, db: Database, user_id: int) -> None:
    """Показать подтверждение бана пользователя в боте."""
    profile = await queries.get_user(db, user_id)
    if profile is None:
        await callback.answer(admin_service.build_search_not_found_text(), show_alert=True)
        return
    await _render(
        callback,
        admin_service.build_ban_confirm_text(profile.display_name, user_id),
        inline.admin_ban_reasons_keyboard(user_id),
    )


async def _confirm_owner_ban(callback: CallbackQuery, db: Database, chat_id: int) -> None:
    """Показать подтверждение бана владельца чата."""
    chat = await queries.get_chat(db, chat_id)
    if chat is None or not chat.owner_id:
        await callback.answer("Владелец этого чата не определён~ 🤔", show_alert=True)
        return
    await _confirm_ban(callback, db, int(chat.owner_id))


async def _apply_ban(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    user_id: int,
    reason_key: str,
) -> None:
    """Забанить пользователя в боте, отвязав все его чаты."""
    reason = None if reason_key == "none" else config.ADMIN_BAN_REASONS.get(reason_key)
    profile = await queries.get_user(db, user_id)
    name = profile.display_name if profile is not None else f"ID {user_id}"
    actor_id = callback.from_user.id if callback.from_user else None

    detached = await admin_service.ban_user_in_bot(
        bot, db, user_id, reason, banned_by=actor_id, name=name
    )
    await callback.answer(f"Забанен: {name} 🚫", show_alert=True)

    payload = await _user_card_payload(db, user_id)
    if payload is None:
        await _render(
            callback,
            f"🚫 Пользователь {escape_text(name)} забанен в боте.\n"
            f"Причина: {escape_text(reason or 'не указана')}\n"
            f"Отвязано чатов: {len(detached)}",
            inline.admin_panel_keyboard(await _open_complaints(db)),
        )
        return
    await _render(
        callback,
        f"🚫 Забанен в боте. Отвязано чатов: {len(detached)}\n\n{payload[0]}",
        payload[1],
    )


async def _apply_unban(callback: CallbackQuery, db: Database, bot: Bot, user_id: int) -> None:
    """Снять бан в боте и обновить профиль пользователя."""
    profile = await queries.get_user(db, user_id)
    name = profile.display_name if profile is not None else f"ID {user_id}"
    actor_id = callback.from_user.id if callback.from_user else None
    removed = await admin_service.unban_user_in_bot(
        bot, db, user_id, actor_id=actor_id, name=name
    )
    if not removed:
        await callback.answer("Пользователь и так не забанен~ ✨", show_alert=True)
        return
    await callback.answer(f"Разбанен: {name} ✅", show_alert=True)
    payload = await _user_card_payload(db, user_id)
    if payload is None:
        await _render(
            callback,
            f"✅ Пользователь {escape_text(name)} разбанен в боте.",
            inline.admin_panel_keyboard(await _open_complaints(db)),
        )
        return
    await _render(callback, f"✅ Разбанен в боте.\n\n{payload[0]}", payload[1])


async def _toggle_global_ban(callback: CallbackQuery, db: Database, user_id: int) -> None:
    """Переключить глобальную метку бана пользователя."""
    profile = await queries.get_user(db, user_id)
    if profile is None:
        await callback.answer(admin_service.build_search_not_found_text(), show_alert=True)
        return
    new_value = not bool(profile.is_globally_banned)
    await admin_service.set_global_ban_flag(db, user_id, new_value)
    await callback.answer(
        "🔨 Глобальный бан включён" if new_value else "🕊 Глобальный бан снят"
    )
    payload = await _user_card_payload(db, user_id)
    if payload is not None:
        await _render(callback, payload[0], payload[1])


async def _run_broadcast(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    actor_id: int,
) -> None:
    """Отправить рассылку с обновлением прогресса в сообщении превью."""
    data = await state.get_data()
    kind = str(data.get("kind") or "chats")
    raw = data.get("content") or {}
    content = richtext.RichContent(
        text=str(raw.get("text") or ""),
        entities=list(raw.get("entities") or []),
        photo=raw.get("photo"),
    )
    if not content.has_content:
        await callback.answer("Содержимое рассылки потерялось~", show_alert=True)
        await state.clear()
        return

    targets = await _broadcast_targets(db, kind)
    if not targets:
        await state.clear()
        await _render(
            callback,
            "Нет получателей для рассылки~ 🤔",
            inline.admin_panel_keyboard(await _open_complaints(db)),
        )
        return

    preview: Optional[Message] = (
        callback.message if isinstance(callback.message, Message) else None
    )
    total = len(targets)
    await callback.answer("Рассылка запущена~ 📢")

    async def progress(sent: int, errors: int) -> None:
        """Обновить текст превью прогрессом рассылки."""
        if preview is None:
            return
        try:
            await preview.edit_text(
                admin_service.build_broadcast_progress_text(sent, errors, total)
            )
        except TelegramBadRequest:
            pass  # текст не изменился — это нормально
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось обновить прогресс рассылки", exc_info=True)

    sent, errors = await admin_service.send_broadcast(
        bot,
        db,
        content,
        targets,
        kind,
        actor_id=actor_id,
        on_progress=progress,
    )
    await state.clear()
    await _render(
        callback,
        admin_service.build_broadcast_done_text(sent, errors),
        inline.admin_panel_keyboard(await _open_complaints(db)),
    )


# ---------------------------------------------------------------------------
# Ввод от владельца бота (FSM)
# ---------------------------------------------------------------------------
async def _back_to_panel(message: Message, db: Database) -> None:
    """Сбросить состояние и вернуть владельца в главное меню панели."""
    try:
        text, markup = await _panel_payload(db)
        await message.answer(text, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("возврате в админ-панель", exc)


@router.message(AdminStates.waiting_search, F.chat.type == ChatType.PRIVATE)
async def admin_search_input(message: Message, state: FSMContext, db: Database) -> None:
    """Найти пользователя по ID или ``@username``."""
    try:
        if message.from_user is None or not is_owner(message.from_user.id):
            return
        raw = (message.text or "").strip()
        if raw.lower() in CANCEL_WORDS:
            await state.clear()
            await _back_to_panel(message, db)
            return
        if not raw:
            await message.answer(admin_service.build_search_prompt_text())
            return

        profile: Optional[UserProfile] = None
        if raw.lstrip("-").isdigit():
            profile = await queries.get_user(db, int(raw))
        else:
            username = raw.lstrip("@")
            if username:
                profile = await queries.get_user_by_username(db, username)

        if profile is None:
            await message.answer(admin_service.build_search_not_found_text())
            return

        payload = await _user_card_payload(db, profile.user_id)
        await state.clear()
        if payload is None:
            await message.answer(admin_service.build_search_not_found_text())
            return
        await message.answer(payload[0], reply_markup=payload[1])
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("поиске пользователя в панели", exc)
        await error_handler.notify_user_softly(message)


@router.message(AdminStates.waiting_broadcast, F.chat.type == ChatType.PRIVATE)
async def admin_broadcast_content(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    """Принять содержимое рассылки и показать превью."""
    try:
        if message.from_user is None or not is_owner(message.from_user.id):
            return
        raw = (message.text or "").strip()
        if raw.lower() in CANCEL_WORDS:
            await state.clear()
            await message.answer(admin_service.build_broadcast_cancelled_text())
            return

        content = richtext.content_from_message(message)
        if not content.has_content:
            await message.answer(
                "Не вижу текста или фото~ Пришли сообщение для рассылки 🌸",
                reply_markup=inline.admin_prompt_keyboard(f"{inline.CB_ADM}broadcast_cancel"),
            )
            return

        content = richtext.trim_content(content)
        data = await state.get_data()
        kind = str(data.get("kind") or "chats")
        targets = await _broadcast_targets(db, kind)
        await state.update_data(
            kind=kind,
            content={
                "text": content.text,
                "entities": content.entities,
                "photo": content.photo,
            },
        )
        await message.answer(
            admin_service.build_broadcast_preview_text(content, len(targets), kind),
            reply_markup=inline.admin_broadcast_confirm_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("приёме содержимого рассылки", exc)
        await error_handler.notify_user_softly(message)


@router.message(AdminStates.waiting_complaint_search, F.chat.type == ChatType.PRIVATE)
async def admin_complaint_search_input(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    """Найти жалобу по её номеру и открыть карточку.

    Карточка рисуется хендлером жалоб (:mod:`yamochan.handlers.complaints`),
    поэтому здесь достаточно отправить владельца по кнопке-ссылке: показываем
    карточку сами, переиспользуя клавиатуру из ``inline``.
    """
    try:
        user = message.from_user
        if user is None or not is_owner(user.id):
            return
        raw = (message.text or "").strip()
        if raw.lower() in CANCEL_WORDS:
            await state.clear()
            await _back_to_panel(message, db)
            return
        if not raw.lstrip("#").isdigit():
            await message.answer("Жду номер жалобы числом~ 🌸")
            return

        complaint_id = int(raw.lstrip("#"))
        complaint = await queries.get_complaint(db, complaint_id)
        await state.clear()
        if complaint is None:
            await message.answer("Жалоба не найдена~ 🤔")
            return
        stats = await queries.count_complaints_by_status(db, complaint.user_id)
        await message.answer(
            complaints_service.build_card_text(complaint),
            reply_markup=inline.admin_complaint_card_keyboard(
                complaint_id,
                is_open=complaint.is_open,
                user_id=complaint.user_id,
                has_history=int(stats.get("total", 0)) > 0,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("поиске жалобы по ID", exc)
        await error_handler.notify_user_softly(message)


@router.message(AdminStates.waiting_user_message, F.chat.type == ChatType.PRIVATE)
async def admin_message_user(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    """Отправить пользователю сообщение админа (текст, сущности, фото)."""
    try:
        user = message.from_user
        if user is None or not is_owner(user.id):
            return
        data = await state.get_data()
        target_id = int(data.get("target_id") or 0)
        raw = (message.text or "").strip()
        if raw.lower() in CANCEL_WORDS:
            await state.clear()
            await _back_to_panel(message, db)
            return

        content = richtext.content_from_message(message)
        if not content.has_content:
            await message.answer("Не вижу текста или фото~ Пришли сообщение 🌸")
            return

        delivered = await admin_service.send_admin_message(
            bot, db, target_id, content, actor_id=user.id
        )
        await state.clear()
        await message.answer(
            "✅ Сообщение отправлено~" if delivered else "Не удалось доставить сообщение 💔"
        )
        await _back_to_panel(message, db)
    except Exception as exc:  # noqa: BLE001
        error_handler.log_exception("отправке сообщения пользователю", exc)
        await error_handler.notify_user_softly(message)
