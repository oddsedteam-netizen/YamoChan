"""Все инлайн-клавиатуры YamoChan.

Правила проекта:
    * ``callback_data`` всегда имеет чёткий префикс для маршрутизации:
      ``profile:``, ``chats:``, ``chat_detail:``, ``settings:``, ``back:``,
      ``antiraid:`` (меню антирейда), ``shield:`` (снятие защиты из меню),
      ``nsfw:`` (экран настройки 18+ команд), ``call:``,
      ``rules:`` (правила чата), ``greeting:`` (приветствие и муты
      помеченным) и ``faq:`` (гайд по возможностям);
    * раскладка собирается через :class:`InlineKeyboardBuilder` и
      :meth:`InlineKeyboardBuilder.adjust`: важные кнопки занимают всю ширину
      ряда, второстепенные могут стоять парами;
    * состояние переключателей видно в подписи: ``✅ ВКЛ`` / ``❌ ВЫКЛ``;
    * кнопки раскрашиваются по смыслу (Bot API ``style``): красный — возврат
      «назад» и удаление, зелёный — добавление и включённые настройки,
      синий — FAQ; раскраску проставляет :class:`StyledKeyboardBuilder`
      (цвета подбирает :func:`button_style`);
    * название чата может содержать переводы строк, поэтому подписи
      нормализуются и обрезаются под лимит Telegram (64 символа).

Каждая функция — чистая и возвращает новый объект
:class:`aiogram.types.InlineKeyboardMarkup`.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Iterable, Optional, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from ..database.models import ChatInfo
from ..services import richtext, time_parser
from ..utils import faq_texts

logger = logging.getLogger(__name__)

#: Префиксы callback_data (используются и в обработчиках, и в клавиатурах).
CB_PROFILE: str = "profile:"
CB_CHATS: str = "chats:"
CB_CHAT_DETAIL: str = "chat_detail:"
CB_SETTINGS: str = "settings:"
CB_BACK: str = "back:"
CB_ANTIRAID: str = "antiraid:"
CB_SHIELD: str = "shield:"
CB_NSFW: str = "nsfw:"
CB_CALL: str = "call:"
CB_RULES: str = "rules:"
CB_GREETING: str = "greeting:"
CB_FAQ: str = "faq:"
#: Админ-панель владельца бота (``/adm``).
CB_ADM: str = "adm:"
#: Система жалоб пользователей (кнопка в главном меню).
CB_COMPLAINT: str = "complaint:"

#: Ключи «назад» — куда возвращаться из подменю.
BACK_MENU: str = "menu"
#: Ключ «назад в главное меню» для FAQ (``back:main``).
BACK_MAIN: str = "main"
BACK_CHATS: str = "chats"
BACK_PROFILE: str = "profile"

#: Максимальная длина подписи инлайн-кнопки в Telegram.
MAX_BUTTON_LENGTH: int = 64


def state_mark(value: object) -> str:
    """Подпись состояния переключателя: ``✅ ВКЛ`` или ``❌ ВЫКЛ``.

    Тексты хранятся в :mod:`yamochan.config`, чтобы подписи кнопок и сообщений
    не расходились.
    """
    return config.STATE_ON if bool(value) else config.STATE_OFF


def state_icon(value: object) -> str:
    """Короткая иконка состояния для компактных кнопок: ``✅`` или ``❌``."""
    return "✅" if bool(value) else "❌"


def utf16_length(text: str) -> int:
    """Длина строки в UTF-16-единицах — именно так её считает Telegram."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _label(text: str, limit: int = MAX_BUTTON_LENGTH) -> str:
    """Нормализовать подпись кнопки: одна строка и лимит длины Telegram.

    Переводы строк (они бывают в названиях чатов) заменяются пробелами,
    а слишком длинная подпись обрезается с многоточием. Считаем в
    UTF-16-единицах, чтобы эмодзи не выводили текст за лимит Telegram.

    :param text: исходная подпись.
    :param limit: максимальная длина подписи.
    """
    single_line = " ".join(text.split())
    if utf16_length(single_line) <= limit:
        return single_line

    shortened = ""
    used = 0
    for char in single_line:
        size = 2 if ord(char) > 0xFFFF else 1
        if used + size > limit - 1:  # одну позицию оставляем под «…»
            break
        shortened += char
        used += size
    return shortened.rstrip() + "…"


def _inline_button(text: str, callback_data: str) -> InlineKeyboardButton:
    """Создать кнопку с нормализованной подписью."""
    return InlineKeyboardButton(text=_label(text), callback_data=callback_data)


# ---------------------------------------------------------------------------
# Цвета кнопок: Bot API принимает danger (красный), success (зелёный) и
# primary (синий). Старые клиенты цвет игнорируют, поэтому ошибка раскраски
# ничего не ломает — клавиатура всё равно остаётся рабочей.
# ---------------------------------------------------------------------------
#: Маркеры удаляющих действий в подписи кнопки — такие кнопки краснеют.
DANGER_BUTTON_MARKERS: Final[tuple[str, ...]] = (
    "очистить",
    "убрать",
    "удал",  # «удалить» и «удаление»
    "снять защиту",
    "снять активную защиту",
    "отменить",
    "выключить",
)

#: Максимальная длина подписи состояния после последнего двоеточия.
TOGGLE_LABEL_LIMIT: int = 12


def _toggle_state(text: str) -> Optional[bool]:
    """Прочитать состояние тумблера из подписи кнопки.

    У тумблеров состояние стоит после последнего двоеточия
    (``🔞 18+: ✅``, ``🔔 Call: ✅ ВКЛ``), а у обычных кнопок там числа или
    название чата — поэтому подпись состояния короткая.

    :param text: подпись кнопки.
    :returns: ``True`` — включено, ``False`` — выключено, ``None`` — не тумблер.
    """
    _, separator, tail = text.rpartition(":")
    if not separator:
        return None
    tail = tail.strip()
    if not tail or len(tail) > TOGGLE_LABEL_LIMIT:
        return None
    if tail.startswith("✅"):
        return True
    if tail.startswith("❌"):
        return False
    return None


def button_style(button: InlineKeyboardButton) -> Optional[str]:
    """Подобрать цвет кнопки по её смыслу.

    Порядок правил (первое совпадение побеждает):

    1. красный (:data:`config.BUTTON_STYLE_DANGER`) — возврат «🔙»,
       отказ «❌» и удаляющие действия (очистить, убрать, удалить,
       снять защиту, отменить, выключить);
    2. зелёный (:data:`config.BUTTON_STYLE_SUCCESS`) — тумблер в положении
       «включено», добавление «➕» и подтверждение «✅»;
    3. синий (:data:`config.BUTTON_STYLE_PRIMARY`) — вход в FAQ.

    :param button: кнопка, которой подбирается цвет.
    :returns: имя стиля или ``None`` — оставить оформление клиента.
    """
    try:
        text = (button.text or "").strip()
        callback_data = button.callback_data or ""
        lowered = text.lower()

        # 1. Откат назад и удаление.
        if text.startswith("🔙") or callback_data.startswith(CB_BACK):
            return config.BUTTON_STYLE_DANGER
        if text.startswith("❌"):
            return config.BUTTON_STYLE_DANGER
        if any(marker in lowered for marker in DANGER_BUTTON_MARKERS):
            return config.BUTTON_STYLE_DANGER

        # 2. Включение и подтверждение.
        if _toggle_state(text) is True:
            return config.BUTTON_STYLE_SUCCESS
        if text.startswith(("➕", "✅")):
            return config.BUTTON_STYLE_SUCCESS
        if "включить" in lowered:
            return config.BUTTON_STYLE_SUCCESS

        # 3. Справочные разделы.
        if callback_data == f"{CB_FAQ}main":
            return config.BUTTON_STYLE_PRIMARY
    except Exception:  # noqa: BLE001 - без цвета кнопка всё равно работает
        logger.debug(
            "Не подобрала цвет кнопки %r", getattr(button, "text", ""), exc_info=True
        )
    return None


def apply_button_styles(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Раскрасить кнопки клавиатуры по их смыслу.

    :param markup: готовая клавиатура.
    :returns: та же клавиатура с проставленными стилями.
    """
    if not config.BUTTON_STYLES_ENABLED:
        return markup
    try:
        for row in markup.inline_keyboard:
            for button in row:
                style = button_style(button)
                if style:
                    button.style = style
    except Exception:  # noqa: BLE001 - клавиатура важнее её раскраски
        logger.debug("Не удалось раскрасить кнопки", exc_info=True)
    return markup


class StyledKeyboardBuilder(InlineKeyboardBuilder):
    """Билдер клавиатур YamoChan: сам красит кнопки при сборке."""

    def as_markup(self, **kwargs: Any) -> InlineKeyboardMarkup:
        """Собрать клавиатуру и раскрасить её кнопки."""
        return apply_button_styles(super().as_markup(**kwargs))


#: В этом модуле все клавиатуры собираются «раскрашивающим» билдером, поэтому
#: стиль не нужно указывать в каждой из полусотни кнопок вручную.
InlineKeyboardBuilder = StyledKeyboardBuilder


def main_menu_keyboard(guarded_chats: Sequence[ChatInfo] = ()) -> InlineKeyboardMarkup:
    """Главное меню в личных сообщениях.

    «Профиль» — длинная кнопка на всю ширину, ниже — кнопки снятия защиты
    для чатов с активным антирейдом, затем «Мои чаты» и «Возможности»
    парой, а замыкает меню широкая кнопка «FAQ».

    :param guarded_chats: чаты владельца с активной защитой.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="👤  П Р О Ф И Л Ь  👤", callback_data=f"{CB_PROFILE}open")
    for chat in guarded_chats:
        builder.button(
            text=_label(f"🛡 Снять защиту: {chat.display_title}"),
            callback_data=f"{CB_SHIELD}lift:{chat.chat_id}",
        )
    builder.button(text="💬 Мои чаты", callback_data=f"{CB_CHATS}list")
    builder.button(text="⚡ Возможности", callback_data=f"{CB_PROFILE}capabilities")
    builder.button(
        text="❓  F A Q  ❓",
        callback_data=f"{CB_FAQ}main",
    )
    builder.button(
        text="📩  О С Т А В И Т Ь   Ж А Л О Б У  📩",
        callback_data=f"{CB_COMPLAINT}open",
    )
    builder.adjust(1, *([1] * len(guarded_chats)), 2, 1, 1)
    return builder.as_markup()


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Одна широкая кнопка «Назад в меню» (возможности, справка, подсказки)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад в меню", callback_data=f"{CB_BACK}{BACK_MENU}")
    builder.adjust(1)
    return builder.as_markup()


def faq_main_keyboard() -> InlineKeyboardMarkup:
    """Главный экран FAQ: разделы парами и возврат в главное меню.

    Набор и порядок кнопок берётся из :data:`utils.faq_texts.FAQ_SECTIONS`,
    поэтому новый раздел достаточно описать в текстах.
    """
    builder = InlineKeyboardBuilder()
    for section_id, title in faq_texts.FAQ_SECTIONS:
        builder.button(text=title, callback_data=f"{CB_FAQ}section:{section_id}")
    builder.button(text="🔙 Назад в меню", callback_data=f"{CB_BACK}{BACK_MAIN}")

    # Разделы идут парами, непарный остаток и «Назад» — отдельными рядами.
    rows = [2] * (len(faq_texts.FAQ_SECTIONS) // 2)
    if len(faq_texts.FAQ_SECTIONS) % 2:
        rows.append(1)
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def faq_section_keyboard() -> InlineKeyboardMarkup:
    """Экран раздела FAQ: одна широкая кнопка «К разделам FAQ»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 К разделам FAQ", callback_data=f"{CB_FAQ}main")
    builder.adjust(1)
    return builder.as_markup()


def profile_keyboard() -> InlineKeyboardMarkup:
    """Карточка профиля: одна широкая кнопка возврата в главное меню."""
    return back_to_menu_keyboard()


def chats_keyboard(chats: Sequence[ChatInfo]) -> InlineKeyboardMarkup:
    """Список «Мои чаты»: каждый чат — длинная кнопка в своём ряду.

    :param chats: чаты пользователя (как владельца или как участника).
    """
    builder = InlineKeyboardBuilder()
    for index, chat in enumerate(chats[: config.MAX_CHATS_IN_LIST], start=1):
        builder.button(
            # Название чата приходит из Telegram: убираем переносы строк и лимит длины.
            text=_label(f"💬 Чат №{index} - {chat.display_title}"),
            callback_data=f"{CB_CHAT_DETAIL}{chat.chat_id}",
        )
    builder.button(text="🔙 Назад", callback_data=f"{CB_BACK}{BACK_MENU}")
    builder.adjust(1)
    return builder.as_markup()


def chat_detail_keyboard(chat_id: int, is_owner: bool) -> InlineKeyboardMarkup:
    """Карточка чата: «Настройки» (владельцу) и возврат к списку чатов.

    :param chat_id: идентификатор чата.
    :param is_owner: показывать ли кнопку настроек (только владельцу).
    """
    builder = InlineKeyboardBuilder()
    if is_owner:
        builder.button(
            text="⚙️  Н А С Т Р О Й К И  ⚙️",
            callback_data=f"{CB_SETTINGS}open:{chat_id}",
        )
    builder.button(text="🔙 К списку чатов", callback_data=f"{CB_BACK}{BACK_CHATS}")
    builder.adjust(1)
    return builder.as_markup()


def settings_keyboard(chat_id: int, settings: dict[str, object]) -> InlineKeyboardMarkup:
    """Настройки чата: тумблеры парами, подменю — на всю ширину.

    Раскладка: ``🔞 18+`` и ``🛡 Антирейд`` в первом ряду, ``📞 Call`` и
    ``📜 Правила`` во втором, затем «👋 Приветствие», «🧹 Служебные
    сообщения», «🛠 Настройка команд модератора» и возврат к карточке чата.

    :param chat_id: идентификатор чата.
    :param settings: текущие настройки чата.
    """
    nsfw = bool(settings.get("nsfw_commands"))
    antiraid = bool(settings.get("antiraid_enabled", settings.get("antiraid")))
    call_enabled = bool(settings.get("call_enabled"))
    rules_enabled = bool(settings.get("rules_enabled"))
    greeting_enabled = bool(settings.get("greeting_enabled"))
    service_messages = bool(settings.get(config.DELETE_SERVICE_MESSAGES_KEY, True))
    protection = bool(settings.get("antiraid_active_protection"))

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🔞 18+: {state_icon(nsfw)}",
        callback_data=f"{CB_NSFW}menu:{chat_id}",
    )
    builder.button(
        text=f"🛡 Антирейд: {state_icon(antiraid)}",
        callback_data=f"{CB_ANTIRAID}menu:{chat_id}",
    )
    builder.button(
        text=f"📞 Call: {state_icon(call_enabled)}",
        callback_data=f"{CB_CALL}menu:{chat_id}",
    )
    builder.button(
        text=f"📜 Правила: {state_icon(rules_enabled)}",
        callback_data=f"{CB_RULES}menu:{chat_id}",
    )
    builder.button(
        text=f"👋 Приветствие: {state_icon(greeting_enabled)}",
        callback_data=f"{CB_GREETING}menu:{chat_id}",
    )
    builder.button(
        text=(
            "🧹 Служебные сообщения: "
            + ("✅ скрыты" if service_messages else "❌ видны")
        ),
        callback_data=f"{CB_SETTINGS}toggle:service:{chat_id}",
    )
    builder.button(
        text="🛠  Настройка команд модератора  🛠",
        callback_data=f"{CB_SETTINGS}commands:{chat_id}",
    )
    if protection:
        builder.button(
            text="🛡 Снять активную защиту",
            callback_data=f"{CB_ANTIRAID}lift:{chat_id}",
        )
    builder.button(text="🔙 Назад к чату", callback_data=f"{CB_CHAT_DETAIL}{chat_id}")
    rows = [2, 2, 1, 1, 1, 1]
    if protection:
        rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def antiraid_keyboard(chat_id: int, settings: dict[str, object]) -> InlineKeyboardMarkup:
    """Меню настроек антирейда и антиспама.

    :param chat_id: идентификатор чата.
    :param settings: текущие настройки чата.
    """
    enabled = bool(settings.get("antiraid_enabled", settings.get("antiraid")))
    threshold = int(settings.get("antiraid_threshold") or config.ANTIRAID_DEFAULT_THRESHOLD)
    timeframe = int(settings.get("antiraid_timeframe") or config.ANTIRAID_DEFAULT_TIMEFRAME)
    spam_threshold = int(settings.get("spam_msg_threshold") or config.SPAM_DEFAULT_THRESHOLD)
    spam_timeframe = int(settings.get("spam_msg_timeframe") or config.SPAM_DEFAULT_TIMEFRAME)
    mode = str(settings.get("antispam_mode") or config.ANTISPAM_MODE_NO_CLOSE)
    protection = bool(settings.get("antiraid_active_protection"))

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🔄 Антирейд: {state_mark(enabled)}",
        callback_data=f"{CB_ANTIRAID}toggle:{chat_id}",
    )
    builder.button(
        text=_label(
            f"👥 Порог: {threshold} чел / {time_parser.human_duration(timeframe)} — изменить"
        ),
        callback_data=f"{CB_ANTIRAID}threshold:{chat_id}",
    )
    builder.button(
        text=_label(
            f"📨 Антиспам с закрытием чата: "
            f"{'✅' if mode == config.ANTISPAM_MODE_CLOSE else '❌'}"
        ),
        callback_data=f"{CB_ANTIRAID}spam:close:{chat_id}",
    )
    builder.button(
        text=_label(
            f"📨 Антиспам без закрытия чата: "
            f"{'✅' if mode == config.ANTISPAM_MODE_NO_CLOSE else '❌'}"
        ),
        callback_data=f"{CB_ANTIRAID}spam:no_close:{chat_id}",
    )
    builder.button(
        text=_label(
            f"📊 Порог спама: {spam_threshold} сооб / "
            f"{time_parser.human_duration(spam_timeframe)} — изменить"
        ),
        callback_data=f"{CB_ANTIRAID}spam_threshold:{chat_id}",
    )
    if protection:
        builder.button(
            text="🛡 Снять активную защиту",
            callback_data=f"{CB_ANTIRAID}lift:{chat_id}",
        )
    builder.button(text="🔙 Назад к настройкам", callback_data=f"{CB_SETTINGS}open:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def antiraid_prompt_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Кнопка возврата из ожидания ввода порога."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад к настройкам антирейда", callback_data=f"{CB_ANTIRAID}menu:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def raid_alert_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Кнопки уведомления владельцу о рейде."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Это не рейд — снять защиту",
        callback_data=f"{CB_ANTIRAID}false_alarm:{chat_id}",
    )
    builder.button(text="🚫 Это рейд!", callback_data=f"{CB_ANTIRAID}confirm_raid:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def raid_confirmed_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Кнопки подтверждённого рейда: снимать защиту или нет."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, снять защиту", callback_data=f"{CB_ANTIRAID}lift:{chat_id}")
    builder.button(text="❌ Нет, оставить", callback_data=f"{CB_ANTIRAID}keep:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def lift_protection_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Одна широкая кнопка снятия защиты (меню и подтверждения)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🛡 Снять защиту", callback_data=f"{CB_SHIELD}lift:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def nsfw_keyboard(chat_id: int, enabled: bool) -> InlineKeyboardMarkup:
    """Экран настройки 18+ команд.

    :param chat_id: идентификатор чата.
    :param enabled: включены ли 18+ команды сейчас.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="❌ Выключить 18+ команды" if enabled else "✅ Включить 18+ команды",
        callback_data=f"{CB_NSFW}{'disable' if enabled else 'enable'}:{chat_id}",
    )
    builder.button(
        text="📋 Список всех 18+ команд",
        callback_data=f"{CB_NSFW}list:{chat_id}",
    )
    builder.button(text="🔙 Назад к настройкам", callback_data=f"{CB_SETTINGS}open:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def nsfw_list_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Экран списка 18+ команд: только возврат к экрану настройки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад к 18+", callback_data=f"{CB_NSFW}menu:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Call режим
# ---------------------------------------------------------------------------
def call_keyboard(chat_id: int, settings: dict[str, object]) -> InlineKeyboardMarkup:
    """Меню Call режима: тумблер широкий, настройки парами, вызов — широкий.

    :param chat_id: идентификатор чата.
    :param settings: текущие настройки чата.
    """
    enabled = bool(settings.get("call_enabled"))
    admins = bool(settings.get("call_mention_admins", True))
    emoji_mode = bool(settings.get("call_use_emoji"))
    delete_messages = bool(settings.get("call_delete_messages"))
    admins_only = bool(settings.get(config.CALL_ADMINS_ONLY_KEY))
    scheduled = len(settings.get("call_scheduled") or [])

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🔔 Call: {state_mark(enabled)}",
        callback_data=f"{CB_CALL}toggle:{chat_id}",
    )
    builder.button(
        text=f"👑 Админы: {state_icon(admins)}",
        callback_data=f"{CB_CALL}admins:{chat_id}",
    )
    builder.button(
        text=f"🎭 Эмодзи: {state_icon(emoji_mode)}",
        callback_data=f"{CB_CALL}emoji_toggle:{chat_id}",
    )
    builder.button(
        text=f"🗑 Удаление: {state_icon(delete_messages)}",
        callback_data=f"{CB_CALL}delete:{chat_id}",
    )
    builder.button(text="🎨 Сменить эмодзи", callback_data=f"{CB_CALL}emoji:{chat_id}")
    builder.button(
        text=f"🔒 Только админам: {state_mark(admins_only)}",
        callback_data=f"{CB_CALL}admins_only:{chat_id}",
    )
    builder.button(
        text="⏰  О Т Л О Ж Е Н Н Ы Й   C A L L  ⏰",
        callback_data=f"{CB_CALL}schedule:{chat_id}",
    )
    builder.button(
        text=f"📋 Список отложенных ({scheduled})",
        callback_data=f"{CB_CALL}list:{chat_id}",
    )
    builder.button(text="🔙 Назад к настройкам", callback_data=f"{CB_SETTINGS}open:{chat_id}")
    builder.adjust(1, 2, 2, 1, 1, 1, 1)
    return builder.as_markup()


def call_scheduled_keyboard(
    chat_id: int,
    entries: Sequence[dict[str, object]],
) -> InlineKeyboardMarkup:
    """Список отложенных вызовов: у каждой записи своя кнопка отмены."""
    builder = InlineKeyboardBuilder()
    for index in range(len(entries)):
        builder.button(
            text=f"❌ Отменить №{index + 1}",
            callback_data=f"{CB_CALL}cancel_scheduled:{chat_id}:{index}",
        )
    builder.button(text="🔙 Назад к Call", callback_data=f"{CB_CALL}menu:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def call_prompt_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Возврат из ожидания ввода (эмодзи или время отложенного вызова)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад к Call", callback_data=f"{CB_CALL}menu:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def commands_settings_keyboard(chat_id: int, settings: dict[str, object]) -> InlineKeyboardMarkup:
    """Подменю команд модератора: каждая команда — длинная кнопка.

    :param chat_id: идентификатор чата.
    :param settings: текущие настройки чата.
    """
    commands = settings.get("commands") or {}
    builder = InlineKeyboardBuilder()
    for command in config.MODERATION_COMMANDS:
        title = config.COMMAND_TITLES.get(command, command)
        enabled = bool(commands.get(command, True))
        builder.button(
            text=f"{title}: {state_mark(enabled)}",
            callback_data=f"{CB_SETTINGS}command:{command}:{chat_id}",
        )
    builder.button(text="🔙 Назад к настройкам", callback_data=f"{CB_SETTINGS}open:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def back_to_chat_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Одна широкая кнопка возврата к карточке конкретного чата."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад к чату", callback_data=f"{CB_CHAT_DETAIL}{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def build_keyboard(rows: Iterable[Iterable[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Собрать клавиатуру из строк ``(текст, callback_data)``.

    Каждая переданная последовательность становится отдельным рядом кнопок.

    :param rows: строки вида ``(подпись кнопки, callback_data)``.
    """
    builder = InlineKeyboardBuilder()
    for row in rows:
        buttons = [_inline_button(text, data) for text, data in row]
        if buttons:
            builder.row(*buttons)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Правила чата и приветствие
# ---------------------------------------------------------------------------
def rules_keyboard(chat_id: int, settings: dict[str, object]) -> InlineKeyboardMarkup:
    """Меню «Правила чата»: каждая кнопка занимает всю ширину ряда.

    Кнопки: тумблер отправки правил при входе, редактор, предпросмотр
    (если правила заданы), удаление фото и полная очистка.

    :param chat_id: идентификатор чата.
    :param settings: текущие настройки чата.
    """
    has_content = bool(str(settings.get("rules_text") or "").strip()) or bool(
        settings.get("rules_photo")
    )
    has_photo = bool(settings.get("rules_photo"))
    show_on_join = bool(settings.get("rules_show_on_join"))

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🚪 При заходе: {state_mark(show_on_join)}",
        callback_data=f"{CB_RULES}join:{chat_id}",
    )
    builder.button(text="✏️ Редактор правил", callback_data=f"{CB_RULES}edit:{chat_id}")
    if has_content:
        builder.button(
            text="👁 Посмотреть правила",
            callback_data=f"{CB_RULES}preview:{chat_id}",
        )
    if has_photo:
        builder.button(text="🖼 Убрать фото", callback_data=f"{CB_RULES}drop_photo:{chat_id}")
    builder.button(text="🧹 Очистить правила", callback_data=f"{CB_RULES}clear:{chat_id}")
    builder.button(text="🔙 Назад к настройкам", callback_data=f"{CB_SETTINGS}open:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def greeting_keyboard(chat_id: int, settings: dict[str, object]) -> InlineKeyboardMarkup:
    """Меню «Приветствие новичков».

    Раскладка: редактор, подменю мутов и сервисные кнопки занимают всю
    ширину ряда, а переключатели профиля и анонимности стоят парой.

    :param chat_id: идентификатор чата.
    :param settings: текущие настройки чата.
    """
    has_content = bool(str(settings.get("greeting_text") or "").strip()) or bool(
        settings.get("greeting_photo")
    )
    has_photo = bool(settings.get("greeting_photo"))
    show_profile = bool(settings.get("greeting_show_profile"))
    anonymous = bool(settings.get(config.WELCOME_ANONYMOUS_KEY))
    check_duration = int(
        settings.get(config.WELCOME_CHECK_DURATION_KEY)
        or config.WELCOME_CHECK_DEFAULT_SECONDS
    )
    join_mute = bool(settings.get(config.JOIN_MUTE_ENABLED_KEY))
    buttons = len(richtext.parse_saved_buttons(settings.get("greeting_buttons")))

    builder = InlineKeyboardBuilder()
    builder.row(_inline_button("✏️ Редактор приветствия", f"{CB_GREETING}edit:{chat_id}"))
    builder.row(
        _inline_button(
            f"📋 Профиль: {state_mark(show_profile)}",
            f"{CB_GREETING}profile:{chat_id}",
        ),
        _inline_button(
            f"🔒 Анонимно: {state_mark(anonymous)}",
            f"{CB_GREETING}anonymous:{chat_id}",
        ),
    )
    if anonymous:
        # Время проверочного мута имеет смысл только в анонимном режиме.
        builder.row(
            _inline_button(
                f"⏱ Время мута проверки: {time_parser.format_duration(check_duration)}",
                f"{CB_GREETING}check_duration:{chat_id}",
            )
        )
    builder.row(
        _inline_button(
            f"🔇 Мут при входе: {state_mark(join_mute)}",
            f"{CB_GREETING}join_mute_menu:{chat_id}",
        )
    )
    builder.row(_inline_button("🔇 Муты помеченным", f"{CB_GREETING}mute_menu:{chat_id}"))
    builder.row(
        _inline_button(
            f"🔘 Кнопки: {buttons}/{config.MAX_GREETING_BUTTONS}",
            f"{CB_GREETING}buttons:{chat_id}",
        )
    )
    if has_content:
        builder.row(
            _inline_button("👁 Превью приветствия", f"{CB_GREETING}preview:{chat_id}")
        )
    if has_photo:
        builder.row(
            _inline_button("🖼 Убрать фото", f"{CB_GREETING}drop_photo:{chat_id}")
        )
    if buttons:
        builder.row(
            _inline_button("🗑 Убрать кнопки", f"{CB_GREETING}drop_buttons:{chat_id}")
        )
    builder.row(_inline_button("🧹 Очистить приветствие", f"{CB_GREETING}clear:{chat_id}"))
    builder.row(_inline_button("🔙 Назад к настройкам", f"{CB_SETTINGS}open:{chat_id}"))
    return builder.as_markup()


def marked_mute_keyboard(chat_id: int, settings: dict[str, object]) -> InlineKeyboardMarkup:
    """Подменю «Муты помеченным»: автомут и его длительность.

    :param chat_id: идентификатор чата.
    :param settings: текущие настройки чата.
    """
    enabled = bool(settings.get("marked_mute_enabled"))
    duration = int(
        settings.get("marked_mute_duration") or config.MARKED_MUTE_DEFAULT_SECONDS
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🔇 Автомут: {state_mark(enabled)}",
        callback_data=f"{CB_GREETING}mute_toggle:{chat_id}",
    )
    builder.button(
        text=f"⏱ Изменить время: {time_parser.format_duration(duration)}",
        callback_data=f"{CB_GREETING}mute_duration:{chat_id}",
    )
    builder.button(
        text="🔙 Назад к приветствию",
        callback_data=f"{CB_GREETING}mute_back:{chat_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def join_mute_keyboard(chat_id: int, settings: dict[str, object]) -> InlineKeyboardMarkup:
    """Подменю «Мут при входе»: тумблер и длительность мута для всех.

    :param chat_id: идентификатор чата.
    :param settings: текущие настройки чата.
    """
    enabled = bool(settings.get(config.JOIN_MUTE_ENABLED_KEY))
    duration = int(
        settings.get(config.JOIN_MUTE_DURATION_KEY) or config.JOIN_MUTE_DEFAULT_SECONDS
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        _inline_button(
            f"🔔 Мут при входе: {state_mark(enabled)}",
            f"{CB_GREETING}join_mute_toggle:{chat_id}",
        )
    )
    builder.row(
        _inline_button(
            f"⏱ Изменить время: {time_parser.human_duration(duration)}",
            f"{CB_GREETING}join_mute_duration:{chat_id}",
        )
    )
    builder.row(
        _inline_button(
            "🔙 Назад к приветствию", f"{CB_GREETING}join_mute_back:{chat_id}"
        )
    )
    return builder.as_markup()


def greeting_buttons_choice_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Шаг 2 редактора приветствия: добавить кнопки или закончить.

    :param chat_id: идентификатор чата.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data=f"{CB_GREETING}buttons_yes:{chat_id}")
    builder.button(text="❌ Нет, готово", callback_data=f"{CB_GREETING}buttons_done:{chat_id}")
    builder.adjust(2)
    return builder.as_markup()


def greeting_button_more_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """После добавления кнопки: добавить ещё или закончить.

    :param chat_id: идентификатор чата.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Да, ещё", callback_data=f"{CB_GREETING}button_more:{chat_id}")
    builder.button(text="✅ Готово", callback_data=f"{CB_GREETING}buttons_done:{chat_id}")
    builder.adjust(2)
    return builder.as_markup()


def greeting_button_prompt_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Клавиатура ожидания текста/ссылки кнопки приветствия."""

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data=f"{CB_GREETING}buttons_done:{chat_id}")
    builder.button(text="🔙 К приветствию", callback_data=f"{CB_GREETING}menu:{chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def content_saved_keyboard(chat_id: int, kind: str) -> InlineKeyboardMarkup:
    """Экран «сохранено»: превью и возврат к меню блока.

    :param chat_id: идентификатор чата.
    :param kind: ``rules`` или ``greeting``.
    """
    prefix = CB_RULES if kind == "rules" else CB_GREETING
    builder = InlineKeyboardBuilder()
    builder.button(text="👁 Превью", callback_data=f"{prefix}preview:{chat_id}")
    builder.button(
        text="🔙 К правилам" if kind == "rules" else "🔙 К приветствию",
        callback_data=f"{prefix}menu:{chat_id}",
    )
    builder.adjust(2)
    return builder.as_markup()


def saved_buttons_keyboard(buttons: object) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура из сохранённых кнопок приветствия (по 2 в ряду).

    :param buttons: список ``{"text": ..., "url": ...}`` из настроек чата.
    """
    return richtext.saved_buttons_markup(buttons)


def content_prompt_keyboard(chat_id: int, prefix: str) -> InlineKeyboardMarkup:
    """Возврат из ожидания текста правил/приветствия.

    :param chat_id: идентификатор чата.
    :param prefix: ``rules`` или ``greeting``.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔙 Назад",
        callback_data=f"{CB_RULES if prefix == 'rules' else CB_GREETING}menu:{chat_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Админ-панель владельца бота (``callback_data`` с префиксом ``adm:``)
# ---------------------------------------------------------------------------
def _paging_buttons(
    builder: InlineKeyboardBuilder,
    section: str,
    page: int,
    pages: int,
) -> None:
    """Добавить ряд пагинации ``[◀️] [Стр. X/N] [▶️]``, если страниц больше одной.

    :param builder: билдер клавиатуры.
    :param section: раздел панели (``chats``, ``banned``, ``log`` и т. п.).
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    if pages <= 1:
        return
    previous = max(1, int(page) - 1)
    following = min(int(pages), int(page) + 1)
    builder.row(
        _inline_button("◀️ Назад", f"{CB_ADM}{section}:{previous}"),
        _inline_button(f"Стр. {page}/{pages}", f"{CB_ADM}{section}:{page}"),
        _inline_button("Вперёд ▶️", f"{CB_ADM}{section}:{following}"),
    )


def admin_panel_keyboard(open_complaints: int = 0) -> InlineKeyboardMarkup:
    """Главная клавиатура админ-панели.

    Раскладка: список чатов, затем пары «Пользователи/Статистика» и
    «Забаненные/Лог», подменю быстрых действий, поиск и рассылка.

    :param open_complaints: сколько жалоб ещё открыто (для подписи кнопки).
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💬  С П И С О К   Ч А Т О В  💬",
        callback_data=f"{CB_ADM}chats",
    )
    builder.button(text="👥 Пользователи", callback_data=f"{CB_ADM}users")
    builder.button(text="📊 Статистика", callback_data=f"{CB_ADM}stats")
    complaints_title = (
        f"📩  Ж А Л О Б Ы  ({open_complaints} новых!)  📩"
        if open_complaints
        else "📩  Ж А Л О Б Ы  📩"
    )
    builder.button(text=complaints_title, callback_data=f"{CB_ADM}complaints")
    builder.button(text="🚫 Забаненные", callback_data=f"{CB_ADM}banned")
    builder.button(text="📋 Лог действий", callback_data=f"{CB_ADM}log")
    builder.button(text="⚡ Быстрые действия", callback_data=f"{CB_ADM}quick")
    builder.button(text="🔍 Поиск юзера", callback_data=f"{CB_ADM}search")
    builder.button(text="📢  Р А С С Ы Л К А  📢", callback_data=f"{CB_ADM}broadcast")
    builder.adjust(1, 2, 1, 2, 1, 1, 1)
    return builder.as_markup()


def admin_chats_keyboard(
    items: Sequence[tuple[int, str]],
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    """Список чатов: каждая строка — кнопка на карточку чата.

    :param items: пары ``(chat_id, название чата)`` текущей страницы.
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    builder = InlineKeyboardBuilder()
    for index, (chat_id, title) in enumerate(items, start=(page - 1) * len(items) + 1):
        builder.row(
            _inline_button(f"💬 Чат №{index}: {title}", f"{CB_ADM}chat:{chat_id}:{page}")
        )
    _paging_buttons(builder, "chats", page, pages)
    builder.row(_inline_button("🔙 В панель", f"{CB_ADM}panel"))
    return builder.as_markup()


def admin_chat_card_keyboard(chat_id: int, page: int = 1) -> InlineKeyboardMarkup:
    """Кнопки карточки чата.

    :param chat_id: идентификатор чата.
    :param page: страница списка, куда возвращаться.
    """
    builder = InlineKeyboardBuilder()
    builder.row(_inline_button("👤 Профиль владельца", f"{CB_ADM}user_from_chat:{chat_id}"))
    builder.row(_inline_button("⚠️ Отвязать чат", f"{CB_ADM}detach:{chat_id}"))
    builder.row(_inline_button("🚫 Забанить владельца", f"{CB_ADM}ban_owner:{chat_id}"))
    builder.row(_inline_button("🔙 К списку чатов", f"{CB_ADM}chats:{page}"))
    return builder.as_markup()


def admin_detach_confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Подтверждение отвязки чата."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, отвязать", callback_data=f"{CB_ADM}detach_ok:{chat_id}")
    builder.button(text="❌ Отмена", callback_data=f"{CB_ADM}chat:{chat_id}:1")
    builder.adjust(2)
    return builder.as_markup()


def admin_users_keyboard() -> InlineKeyboardMarkup:
    """Раздел «Пользователи»: списки-фильтры и поиск."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔴 Плохая репутация", callback_data=f"{CB_ADM}badrep")
    builder.button(text="🚫 Спамеры", callback_data=f"{CB_ADM}spammers")
    builder.button(text="🔨 Глобально забанены", callback_data=f"{CB_ADM}gbanned")
    builder.button(text="🔍 Поиск по ID", callback_data=f"{CB_ADM}search")
    builder.button(text="🔙 В панель", callback_data=f"{CB_ADM}panel")
    builder.adjust(1)
    return builder.as_markup()


def admin_user_list_keyboard(
    section: str,
    items: Sequence[tuple[int, str]],
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    """Список пользователей по фильтру: кнопка на админский профиль.

    :param section: раздел фильтра (``badrep``, ``spammers``, ``gbanned``).
    :param items: пары ``(user_id, имя)`` текущей страницы.
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    builder = InlineKeyboardBuilder()
    for index, (user_id, name) in enumerate(items, start=(page - 1) * len(items) + 1):
        builder.row(_inline_button(f"👤 {index}. {name}", f"{CB_ADM}user:{user_id}"))
    _paging_buttons(builder, section, page, pages)
    builder.row(_inline_button("🔙 К пользователям", f"{CB_ADM}users"))
    return builder.as_markup()


def admin_user_card_keyboard(
    user_id: int,
    *,
    has_complaints: bool = False,
    bot_banned: bool = False,
    globally_banned: bool = False,
) -> InlineKeyboardMarkup:
    """Кнопки админского профиля пользователя.

    :param user_id: идентификатор пользователя.
    :param has_complaints: показывать ли кнопку «Жалобы этого юзера».
    :param bot_banned: забанен ли пользователь в боте.
    :param globally_banned: стоит ли глобальная метка бана.
    """
    builder = InlineKeyboardBuilder()
    if has_complaints:
        builder.row(
            _inline_button("📩 Жалобы этого юзера", f"{CB_ADM}user_complaints:{user_id}")
        )
    if bot_banned:
        builder.row(_inline_button("✅ Разбанить в боте", f"{CB_ADM}unban_user:{user_id}"))
    else:
        builder.row(_inline_button("🚫 Забанить в боте", f"{CB_ADM}ban_user:{user_id}"))
    builder.row(
        _inline_button(
            "🕊 Снять глобальный бан" if globally_banned else "🔨 Глобальный бан",
            f"{CB_ADM}gban:{user_id}",
        )
    )
    builder.row(_inline_button("📨 Написать юзеру", f"{CB_ADM}msg_user:{user_id}"))
    builder.row(_inline_button("🔙 К пользователям", f"{CB_ADM}users"))
    return builder.as_markup()


def admin_ban_reasons_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Выбор причины бана в боте.

    :param user_id: кого баним.
    """
    builder = InlineKeyboardBuilder()
    for key, title in config.ADMIN_BAN_REASONS.items():
        builder.button(text=f"❌ {title}", callback_data=f"{CB_ADM}ban_reason:{user_id}:{key}")
    builder.button(text="🔙 Отмена", callback_data=f"{CB_ADM}user:{user_id}")
    builder.adjust(1)
    return builder.as_markup()


def admin_stats_keyboard() -> InlineKeyboardMarkup:
    """Экран расширенной статистики: обновление и возврат."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data=f"{CB_ADM}stats")
    builder.button(text="🔙 В панель", callback_data=f"{CB_ADM}panel")
    builder.adjust(1)
    return builder.as_markup()


def admin_banned_keyboard(
    items: Sequence[tuple[int, str]],
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    """Список забаненных: карточка пользователя и кнопка разбана.

    :param items: пары ``(user_id, имя)`` текущей страницы.
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    builder = InlineKeyboardBuilder()
    for index, (user_id, name) in enumerate(items, start=(page - 1) * len(items) + 1):
        builder.row(_inline_button(f"👤 {index}. {name}", f"{CB_ADM}user:{user_id}"))
        builder.row(_inline_button(f"🔓 Разбанить {name}", f"{CB_ADM}unban_user:{user_id}"))
    _paging_buttons(builder, "banned", page, pages)
    builder.row(_inline_button("🔙 В панель", f"{CB_ADM}panel"))
    return builder.as_markup()


def admin_log_keyboard(page: int, pages: int) -> InlineKeyboardMarkup:
    """Журнал действий: пагинация и возврат в панель.

    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    builder = InlineKeyboardBuilder()
    _paging_buttons(builder, "log", page, pages)
    builder.row(_inline_button("🔙 В панель", f"{CB_ADM}panel"))
    return builder.as_markup()


def admin_broadcast_keyboard(chats: int, owners: int) -> InlineKeyboardMarkup:
    """Экран «Рассылка»: выбор получателей.

    :param chats: сколько чатов подключено.
    :param owners: сколько уникальных владельцев.
    """
    builder = InlineKeyboardBuilder()
    builder.row(_inline_button(f"💬 Во все чаты ({chats})", f"{CB_ADM}broadcast_chats"))
    builder.row(
        _inline_button(f"👤 Всем владельцам в ЛС ({owners})", f"{CB_ADM}broadcast_owners")
    )
    builder.row(_inline_button("🔙 В панель", f"{CB_ADM}panel"))
    return builder.as_markup()


def admin_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение рассылки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить", callback_data=f"{CB_ADM}broadcast_send")
    builder.button(text="❌ Отмена", callback_data=f"{CB_ADM}broadcast_cancel")
    builder.adjust(2)
    return builder.as_markup()


def admin_prompt_keyboard(back_callback: str) -> InlineKeyboardMarkup:
    """Клавиатура ожидания ввода: одна кнопка возврата.

    :param back_callback: ``callback_data`` кнопки возврата.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Отмена", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()


def admin_complaints_section_keyboard(
    open_count: int,
    accepted_count: int,
    rejected_count: int,
) -> InlineKeyboardMarkup:
    """Раздел «Жалобы»: статистика и кнопки списков.

    :param open_count: сколько жалоб открыто.
    :param accepted_count: сколько жалоб принято.
    :param rejected_count: сколько жалоб отклонено.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        _inline_button(
            f"⏳ Открытые жалобы ({open_count})", f"{CB_ADM}complaints:open"
        )
    )
    builder.row(
        _inline_button(f"✅ Принятые ({accepted_count})", f"{CB_ADM}complaints:accepted"),
        _inline_button(f"❌ Отклонённые ({rejected_count})", f"{CB_ADM}complaints:rejected"),
    )
    builder.row(_inline_button("🔍 Поиск по ID жалобы", f"{CB_ADM}complaint_search"))
    builder.row(_inline_button("🔙 В панель", f"{CB_ADM}panel"))
    return builder.as_markup()


def admin_complaint_list_keyboard(
    kind: str,
    items: Sequence[int],
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    """Список жалоб по статусу: каждая жалоба — кнопка открытия.

    :param kind: ``open`` / ``accepted`` / ``rejected`` (для пагинации).
    :param items: номера жалоб текущей страницы.
    :param page: текущая страница (1-based).
    :param pages: всего страниц.
    """
    builder = InlineKeyboardBuilder()
    for complaint_id in items:
        builder.row(_inline_button(f"📩 Жалоба #{complaint_id}", f"{CB_ADM}complaint:{complaint_id}"))
    _paging_buttons(builder, f"complaints:{kind}", page, pages)
    builder.row(_inline_button("🔙 К жалобам", f"{CB_ADM}complaints"))
    return builder.as_markup()


def admin_complaint_card_keyboard(
    complaint_id: int,
    *,
    is_open: bool,
    user_id: int,
    has_history: bool = False,
) -> InlineKeyboardMarkup:
    """Кнопки карточки жалобы.

    :param complaint_id: номер жалобы.
    :param is_open: открыта ли жалоба (для открытых — принять/отклонить).
    :param user_id: идентификатор автора (для «все жалобы» и профиля).
    :param has_history: были ли у автора жалобы раньше.
    """
    builder = InlineKeyboardBuilder()
    if is_open:
        builder.row(
            _inline_button("✅ Принять жалобу", f"{CB_ADM}complaint:accept:{complaint_id}"),
            _inline_button("❌ Отклонить", f"{CB_ADM}complaint:reject:{complaint_id}"),
        )
    else:
        builder.row(
            _inline_button("🔄 Открыть заново", f"{CB_ADM}complaint:reopen:{complaint_id}")
        )
    if has_history:
        builder.row(
            _inline_button("📩 Все жалобы юзера", f"{CB_ADM}complaint:list:{user_id}")
        )
    builder.row(_inline_button("💬 Ответить юзеру", f"{CB_ADM}complaint:reply:{complaint_id}"))
    builder.row(_inline_button("👤 Профиль юзера", f"{CB_ADM}user:{user_id}"))
    return builder.as_markup()


def admin_complaint_decision_keyboard(
    complaint_id: int,
    *,
    accepted: bool,
) -> InlineKeyboardMarkup:
    """Кнопки выбора ответа по жалобе (стандартный ответ или отмена).

    :param complaint_id: номер жалобы.
    :param accepted: ``True`` — принятие, ``False`` — отклонение.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📝 Стандартный ответ",
        callback_data=f"{CB_ADM}complaint:standard:{complaint_id}:{'accept' if accepted else 'reject'}",
    )
    builder.button(text="🔙 Отмена", callback_data=f"{CB_ADM}complaint:{complaint_id}")
    builder.adjust(1)
    return builder.as_markup()


def admin_complaint_response_keyboard(complaint_id: int) -> InlineKeyboardMarkup:
    """Клавиатура отмены ввода ответа по жалобе.

    :param complaint_id: номер жалобы.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Отмена", callback_data=f"{CB_ADM}complaint:{complaint_id}")
    builder.adjust(1)
    return builder.as_markup()


def admin_quick_actions_keyboard() -> InlineKeyboardMarkup:
    """Подменю «Быстрые действия» админ-панели."""
    builder = InlineKeyboardBuilder()
    builder.row(_inline_button("🔄 Перезагрузить кэш админов", f"{CB_ADM}quick:cache"))
    builder.row(_inline_button("📊 Обновить счётчики", f"{CB_ADM}quick:counters"))
    builder.row(_inline_button("🗑 Очистить старые логи", f"{CB_ADM}quick:logs"))
    builder.row(_inline_button("💾 Бэкап БД", f"{CB_ADM}quick:backup"))
    builder.row(_inline_button("🔙 В панель", f"{CB_ADM}panel"))
    return builder.as_markup()


def admin_user_complaints_keyboard(
    user_id: int,
    items: Sequence[tuple[int, str]],
) -> InlineKeyboardMarkup:
    """Жалобы конкретного пользователя в админском профиле.

    :param user_id: чей профиль открыт (кнопка возврата).
    :param items: пары ``(complaint_id, подпись)``.
    """
    builder = InlineKeyboardBuilder()
    for complaint_id, title in items:
        builder.row(_inline_button(title, f"{CB_ADM}complaint:{complaint_id}"))
    builder.row(_inline_button("🔙 Назад к профилю", f"{CB_ADM}user:{user_id}"))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Система жалоб пользователей (``callback_data`` с префиксом ``complaint:``)
# ---------------------------------------------------------------------------
def complaint_reasons_keyboard() -> InlineKeyboardMarkup:
    """Выбор категории жалобы (по одной кнопке в ряду)."""
    builder = InlineKeyboardBuilder()
    for key, title in config.COMPLAINT_REASONS.items():
        builder.button(text=title, callback_data=f"{CB_COMPLAINT}reason:{key}")
    builder.button(text="❌ Отмена", callback_data=f"{CB_COMPLAINT}cancel")
    builder.adjust(1)
    return builder.as_markup()


def complaint_photo_keyboard() -> InlineKeyboardMarkup:
    """Шаг с фото: пропустить или отменить."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ Пропустить", callback_data=f"{CB_COMPLAINT}skip_photo")
    builder.button(text="❌ Отмена", callback_data=f"{CB_COMPLAINT}cancel")
    builder.adjust(1)
    return builder.as_markup()


def complaint_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение отправки жалобы."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить", callback_data=f"{CB_COMPLAINT}confirm")
    builder.button(text="❌ Отмена", callback_data=f"{CB_COMPLAINT}cancel")
    builder.adjust(2)
    return builder.as_markup()


def complaint_prompt_keyboard() -> InlineKeyboardMarkup:
    """Возврат из ожидания описания жалобы."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data=f"{CB_COMPLAINT}open")
    builder.adjust(1)
    return builder.as_markup()


