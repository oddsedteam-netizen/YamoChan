"""Быстрая самопроверка проекта YamoChan (без сети и без запуска бота).

Запуск::

    python check.py

Проверяет синтаксис, импорты, константы, чистую логику и сборку экранов —
то, что чаще всего ломается при правках. Возвращает ненулевой код при любой
ошибке, чтобы использовать в CI.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # консоль Windows (cp1251) не умеет печатать эмодзи — чиним вывод
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

FAILURES: list[str] = []
PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """Зафиксировать результат одной проверки."""
    global PASSED
    if condition:
        PASSED += 1
        print(f"✅ {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"❌ {name} — {detail}")


def _iter_sources():
    """Все исходники проекта, кроме кеша и виртуальных окружений."""
    skip = {"__pycache__", ".venv", "venv", ".git", "node_modules"}
    for path in ROOT.rglob("*.py"):
        if skip & set(path.parts):
            continue
        yield path


def check_syntax() -> None:
    """Проверить, что все файлы компилируются."""
    ok = True
    for path in _iter_sources():
        rel = path.relative_to(ROOT)
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except Exception as exc:  # noqa: BLE001
            ok = False
            check(f"синтаксис {rel}", False, f"{type(exc).__name__}: {exc}")
    check("синтаксис всех .py", ok)


def check_config() -> None:
    """Проверить ключевые константы конфигурации."""
    import config

    parts = str(config.BOT_VERSION).split(".")
    check(
        "версия в формате X.Y.Z",
        len(parts) == 3 and all(part.isdigit() for part in parts),
        config.BOT_VERSION,
    )
    check(
        "JOIN_DEDUP_TTL <= 10с", config.JOIN_DEDUP_TTL <= 10, str(config.JOIN_DEDUP_TTL)
    )
    check(
        "MSK_OFFSET_HOURS == 3",
        config.MSK_OFFSET_HOURS == 3,
        str(config.MSK_OFFSET_HOURS),
    )

    defaults = config.DEFAULT_CHAT_SETTINGS
    required = (
        "quiet_hours_enabled",
        "quiet_hours_start",
        "quiet_hours_end",
        "quiet_hours_days",
        "quiet_hours_message_text",
        "quiet_hours_open_message_text",
        "quiet_hours_active",
        "custom_rp_commands",
        "rp_hide_admins",
    )
    missing = [key for key in required if key not in defaults]
    check("новые ключи в DEFAULT_CHAT_SETTINGS", not missing, ", ".join(missing))


def check_quiet_hours_logic() -> None:
    """Проверить парсер времени и окно «тихого времени»."""
    from services import quiet_hours as qh

    check("parse_hhmm('23:00')", qh.parse_hhmm("23:00") == (23, 0))
    check("parse_hhmm('8:5')", qh.parse_hhmm("8:5") == (8, 5))
    check("parse_hhmm('2300')", qh.parse_hhmm("2300") == (23, 0))
    check("parse_hhmm('мусор')", qh.parse_hhmm("мусор") is None)
    check("format_hhmm('8:5')", qh.format_hhmm("8:5") == "08:05")

    all_days = list(range(7))
    check(
        "окно 23:00->08:00 (внутри)",
        qh.is_within_window(
            "23:00", "08:00", all_days, moment=datetime(2024, 1, 1, 2, 0)
        )
        is True,
    )
    check(
        "окно 23:00->08:00 (вне)",
        qh.is_within_window(
            "23:00", "08:00", all_days, moment=datetime(2024, 1, 1, 12, 0)
        )
        is False,
    )
    check(
        "окно 09:00->18:00 (внутри)",
        qh.is_within_window(
            "09:00", "18:00", all_days, moment=datetime(2024, 1, 1, 12, 0)
        )
        is True,
    )
    check(
        "дни недели учитываются",
        qh.is_within_window(
            "09:00", "18:00", [0], moment=datetime(2024, 1, 2, 12, 0)
        )
        is False,
    )


def check_rp_logic() -> None:
    """Проверить антидеанон админов и нормализацию действия."""
    from handlers import roleplay as rp

    check("neutral_action('пнул(а)')", rp.neutral_action("пнул(а)") == "пнул")
    check(
        "neutral_action('извинился(ась) перед')",
        rp.neutral_action("извинился(ась) перед") == "извинился перед",
    )
    admin_line = rp.build_action_text(
        "🦵", "пнул", 1, "Вася", 2, "Петя", actor_label="Админ"
    )
    check(
        "антидеанон: имя админа не раскрывается",
        "Вася" not in admin_line and "Админ" in admin_line,
        admin_line,
    )
    normal_line = rp.build_action_text("🦵", "пнул(а)", 1, "Вася", 2, "Петя")
    check("обычная строка содержит автора", "Вася" in normal_line, normal_line)


def check_service_cleanup() -> None:
    """Убедиться, что очередь служебных сообщений не рвёт цикл."""
    path = ROOT / "services" / "service_cleanup.py"
    text = path.read_text(encoding="utf-8")
    idx = text.find("queue.extendleft(reversed(batch))")
    ok = idx != -1 and "continue" in text[idx : idx + 80]
    check("service_cleanup: continue после extendleft", ok)


def check_screens() -> None:
    """Собрать ключевые тексты и клавиатуры на фиктивных данных."""
    import config
    from database.models import ChatInfo
    from keyboards import inline
    from services import admin as admin_service
    from services import profile as profile_service

    chat = ChatInfo(chat_id=-100500, title="Тестовый чат")
    settings = dict(config.DEFAULT_CHAT_SETTINGS)
    member = type("Member", (), {"id": 42, "full_name": "Тестовый"})()

    builders = {
        "settings_keyboard": lambda: inline.settings_keyboard(
            chat.chat_id, settings
        ),
        "quiet_hours_keyboard": lambda: inline.quiet_hours_keyboard(
            chat.chat_id, settings
        ),
        "quiet_days_keyboard": lambda: inline.quiet_days_keyboard(
            chat.chat_id, settings
        ),
        "rp_custom_keyboard": lambda: inline.rp_custom_keyboard(chat.chat_id, []),
        "greeting_keyboard": lambda: inline.greeting_keyboard(
            chat.chat_id, settings
        ),
        "build_settings_text": lambda: profile_service.build_settings_text(
            chat, settings
        ),
        "build_quiet_hours_menu_text": lambda: (
            profile_service.build_quiet_hours_menu_text(chat, settings)
        ),
        "build_join_profile_summary": lambda: (
            profile_service.build_join_profile_summary(
                None, None, member, fallback_name="Тестовый"
            )
        ),
        "build_resync_chats_text": lambda: admin_service.build_resync_chats_text(3, 2, 1),
    }
    for name, factory in builders.items():
        try:
            result = factory()
            check(name, bool(result), "вернулось пусто")
        except Exception as exc:  # noqa: BLE001
            check(name, False, f"{type(exc).__name__}: {exc}")


def check_imports() -> None:
    """Импортировать все ключевые модули (ловит ошибки связей)."""
    modules = (
        "config",
        "database.db",
        "database.models",
        "database.queries",
        "services.time_parser",
        "services.profile",
        "services.quiet_hours",
        "services.rp_commands",
        "services.service_cleanup",
        "services.permissions",
        "keyboards.inline",
        "handlers.events",
        "handlers.roleplay",
        "handlers.rp_custom",
        "handlers.quiet_hours",
        "handlers.callbacks",
        "handlers.rules",
        "bot",
    )
    for name in modules:
        try:
            importlib.import_module(name)
            check(f"импорт {name}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"импорт {name}", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    """Прогнать все проверки и вернуть код возврата."""
    print("🔎 Самопроверка YamoChan\n")
    deps_ok = True
    try:
        import aiogram  # noqa: F401
    except Exception:  # noqa: BLE001
        deps_ok = False
        print(
            "⚠️ Не нашла aiogram. Установи зависимости: "
            "pip install -r requirements.txt\n"
            "Будут выполнены только проверки синтаксиса и файлов.\n"
        )

    check_syntax()
    check_service_cleanup()
    if deps_ok:
        check_config()
        check_quiet_hours_logic()
        check_rp_logic()
        check_screens()
        check_imports()

    print()
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for item in FAILURES:
            print(f"   • {item}")
        return 1
    print(f"✅ Все проверки пройдены (успешных: {PASSED}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
