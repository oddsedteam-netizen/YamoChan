"""Парсер времени для команд модерации.

Поддерживаются любые комбинации русских и английских обозначений:

    * ``1д``, ``2ч``, ``30м``, ``15с``
    * ``1d``, ``2h``, ``30m``, ``15s``
    * ``1д2ч30м``, ``1d2h30m``, ``1д2h30м`` (смешанный формат)
    * слова-синонимы: ``навсегда``, ``forever``, ``вечно``
    * число без букв трактуется как минуты (``30`` → 30 минут)

Все функции чистые и не бросают исключений на «мусорном» вводе:
непонятный токен просто считается частью причины.
"""

from __future__ import annotations

import re
from typing import Final, Optional, Sequence

config import MAX_PUNISH_TIME_SECONDS, MIN_PUNISH_TIME_SECONDS

# ---------------------------------------------------------------------------
# Единицы измерения
# ---------------------------------------------------------------------------
#: Слова-маркеры бессрочного наказания.
PERMANENT_WORDS: Final[frozenset[str]] = frozenset(
    {
        "навсегда",
        "навечно",
        "бессрочно",
        "вечно",
        "перманент",
        "перманентно",
        "forever",
        "permanent",
        "perm",
        "inf",
        "infinity",
        "infinite",
        "never",
        "∞",
        "0",
    }
)

#: Слова-маркеры, что срок не указан (эквивалент бессрочного наказания).
NO_TIME_WORDS: Final[frozenset[str]] = frozenset({"нет", "none", "без", "не", "-"})

#: Алиасы единиц измерения: название → количество секунд.
_UNIT_ALIASES: Final[dict[str, int]] = {
    # дни
    "дней": 86400,
    "дня": 86400,
    "день": 86400,
    "дн": 86400,
    "д": 86400,
    "days": 86400,
    "day": 86400,
    "d": 86400,
    # часы
    "часов": 3600,
    "часа": 3600,
    "час": 3600,
    "ч": 3600,
    "hours": 3600,
    "hour": 3600,
    "h": 3600,
    # минуты
    "минут": 60,
    "минуты": 60,
    "минуту": 60,
    "мин": 60,
    "м": 60,
    "minutes": 60,
    "minute": 60,
    "mins": 60,
    "min": 60,
    "m": 60,
    # секунды
    "секунд": 1,
    "секунды": 1,
    "секунду": 1,
    "сек": 1,
    "с": 1,
    "seconds": 1,
    "second": 1,
    "secs": 1,
    "sec": 1,
    "s": 1,
}

#: Части регулярного выражения для единиц (сначала самые длинные названия).
_UNIT_PATTERN: Final[str] = "|".join(
    re.escape(alias) for alias in sorted(_UNIT_ALIASES, key=len, reverse=True)
)

#: Основная регулярка: число + единица измерения (``1д``, ``30 m``, ``2 часа``).
TIME_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    rf"(\d+)\s*({_UNIT_PATTERN})",
    re.IGNORECASE,
)

def is_permanent_word(token: Optional[str]) -> bool:
    """Проверить, означает ли токен бессрочное наказание."""
    if not token:
        return False
    return token.strip().lower().rstrip(".!") in PERMANENT_WORDS


def is_minutes_number(token: Optional[str]) -> bool:
    """Проверить, что токен — просто число (трактуется как минуты)."""
    if not token:
        return False
    return token.strip().isdigit()


def has_time_marker(token: Optional[str]) -> bool:
    """Проверить, содержит ли токен хоть одно обозначение времени."""
    if not token:
        return False
    raw = token.strip().lower()
    if not raw:
        return False
    return bool(TIME_TOKEN_RE.search(raw)) or is_minutes_number(raw) or is_permanent_word(raw)


def parse_time_token(token: Optional[str]) -> Optional[int]:
    """Разобрать токен времени и вернуть количество секунд.

    :param token: например ``"1д2ч30м"``, ``"30"`` или ``"forever"``.
    :returns: количество секунд или ``None``, если токен бессрочный либо
        не является обозначением времени.
    """
    if not token:
        return None
    raw = token.strip().lower().rstrip(".!")
    if not raw:
        return None
    if is_permanent_word(raw):
        return None
    if raw.isdigit():
        # Число без букв = минуты по умолчанию.
        minutes = int(raw)
        return minutes * 60 if minutes > 0 else None

    total_seconds = 0
    matched = False
    position = 0
    for match in TIME_TOKEN_RE.finditer(raw):
        gap = raw[position : match.start()].strip()
        if gap:
            if not gap.isdigit():
                # Непонятный текст между обозначениями — значит это не время.
                return None
            # Число без букв = минуты по умолчанию («2ч30» → 2 часа 30 минут).
            total_seconds += int(gap) * 60
        position = match.end()
        value = int(match.group(1))
        unit_seconds = _UNIT_ALIASES.get(match.group(2).lower(), 0)
        total_seconds += value * unit_seconds
        matched = True

    tail = raw[position:].strip()
    if tail:
        if not tail.isdigit():
            return None
        # Хвост без единицы измерения — тоже минуты.
        total_seconds += int(tail) * 60
        matched = True

    if not matched:
        return None
    return total_seconds if total_seconds > 0 else None


def normalize_duration(seconds: Optional[int]) -> Optional[int]:
    """Привести длительность к допустимому Telegram диапазону.

    :returns: ``None`` для бессрочного наказания, иначе секунды в пределах
        от :data:`config.MIN_PUNISH_TIME_SECONDS` до
        :data:`config.MAX_PUNISH_TIME_SECONDS`.
    """
    if seconds is None:
        return None
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > MAX_PUNISH_TIME_SECONDS:
        return MAX_PUNISH_TIME_SECONDS
    if value < MIN_PUNISH_TIME_SECONDS:
        return MIN_PUNISH_TIME_SECONDS
    return value


def format_duration(seconds: Optional[int]) -> str:
    """Красиво отформатировать длительность на русском языке.

    :param seconds: длительность в секундах или ``None`` для бессрочного.
    """
    if seconds is None:
        return "навсегда"
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "навсегда"
    if value <= 0:
        return "навсегда"

    days, remainder = divmod(value, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    if secs and not parts:
        parts.append(f"{secs}с")
    return " ".join(parts) or "навсегда"


def human_duration(seconds: Optional[int]) -> str:
    """Человекочитаемая длительность для интерфейса (окна антирейда/антиспама).

    :param seconds: длительность в секундах.
    :returns: строка вида ``5 мин``, ``1 ч``, ``30 сек``, ``1 мин 30 сек``.
    """
    if seconds is None:
        return "бессрочно"
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "бессрочно"
    if value <= 0:
        return "0 сек"

    days, remainder = divmod(value, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs and not days and not hours:
        parts.append(f"{secs} сек")
    return " ".join(parts)


def looks_like_time(token: Optional[str]) -> bool:
    """Определить, похож ли токен на обозначение срока."""
    if not token:
        return False
    return is_permanent_word(token) or has_time_marker(token)


def extract_duration(tokens: Sequence[str]) -> tuple[Optional[int], bool, list[str]]:
    """Вытащить срок наказания из списка аргументов команды.

    Сроком считается только первый токен: в формате
    ``.бан [цель] [время] [причина]`` время всегда идёт первым.

    :param tokens: аргументы команды (после цели).
    :returns: кортеж ``(секунды, был_ли_указан_срок, оставшиеся_токены)``.
    """
    remaining = [token for token in tokens if token]
    if not remaining:
        return None, False, []
    first = remaining[0]
    if is_permanent_word(first):
        return None, True, remaining[1:]
    if is_minutes_number(first):
        return parse_time_token(first), True, remaining[1:]
    if has_time_marker(first):
        seconds = parse_time_token(first)
        if seconds is not None:
            return seconds, True, remaining[1:]
        # Похоже на срок, но разобрать не удалось — считаем это частью причины,
        # чтобы случайно не выдать бессрочное наказание.
        return None, False, remaining
    return None, False, remaining
