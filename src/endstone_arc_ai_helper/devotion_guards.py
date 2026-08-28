"""Hard limits for deity-mode devotion: block bypass commands and cap blessings."""

from __future__ import annotations

import re
from typing import Any

from .player_inventory import _wealth_points_for_item, normalize_item_id

_EXECUTE_BLESSING = re.compile(
    r"\brun\s+(tp|teleport|effect|give)\b",
    re.IGNORECASE,
)

_BLESSING_ROOTS = frozenset({"effect", "give", "tp", "teleport"})

# amplifier 0 = I, 1 = II — players should not receive absurd potion tiers.
MAX_PLAYER_AMPLIFIER = 1
MAX_PLAYER_DURATION_SECONDS = 300

_DEVOTION_BYPASS_HINT = (
    "信仰模式下赐予效果/物品/传送须通过 mc_divine_intervention 并消耗近期好感，"
    "禁止用 mc_run_command 绕过扣费。"
)


def devotion_bypass_hint() -> str:
    return _DEVOTION_BYPASS_HINT


def is_devotion_blessing_command(command: str) -> bool:
    """True when a console command would grant favor-worthy benefits."""
    normalized = str(command or "").strip().lstrip("/").strip()
    if not normalized:
        return False
    parts = normalized.split()
    root = parts[0].lower() if parts else ""
    if root in _BLESSING_ROOTS:
        return True
    if root == "execute" and _EXECUTE_BLESSING.search(normalized):
        return True
    return False


def min_favor_for_effect(*, amplifier: int, duration_seconds: int) -> int:
    amp = max(0, int(amplifier))
    duration = max(5, int(duration_seconds))
    base = 8 + amp * 15
    if duration > 120:
        base += ((duration - 120) + 29) // 30 * 4
    return max(5, base)


def min_favor_for_item(item_id: str, amount: int) -> int:
    normalized = normalize_item_id(item_id)
    try:
        count = max(1, int(amount))
    except Exception:
        count = 1
    points = _wealth_points_for_item(normalized, count)
    if points <= 0:
        return 5
    if points < 10:
        return 5
    if points < 30:
        return 12
    if points < 60:
        return 25
    return 40


def min_favor_for_tp_command(command: str) -> int:
    normalized = str(command or "").strip().lower()
    if "tp" not in normalized and "teleport" not in normalized:
        return 15
    # crude distance heuristic: far coords → higher cost
    nums = re.findall(r"-?\d+(?:\.\d+)?", normalized)
    if len(nums) >= 4:
        return 35
    return 18


def clamp_player_blessing(*, amplifier: int, duration_seconds: int) -> tuple[int, int, str]:
    """Return (amplifier, duration, error_message). error_message non-empty = reject."""
    amp = max(0, int(amplifier))
    duration = max(5, int(duration_seconds))
    if amp > MAX_PLAYER_AMPLIFIER:
        return (
            amp,
            duration,
            f"效果等级过高：凡人最多承受 II 级（amplifier≤{MAX_PLAYER_AMPLIFIER}），"
            "禁止 V 级等超模神恩。",
        )
    if duration > MAX_PLAYER_DURATION_SECONDS:
        duration = MAX_PLAYER_DURATION_SECONDS
    return amp, duration, ""


def validate_divine_favor_cost(
    *,
    favor_cost: int,
    blessing: str = "",
    amplifier: int = 0,
    duration_seconds: int = 120,
    item_id: str = "",
    item_amount: int = 1,
    command: str = "",
) -> tuple[bool, str, int]:
    """Return (ok, message, minimum_required)."""
    try:
        paid = int(favor_cost)
    except Exception:
        return False, "favor_cost 必须是整数", 0
    if paid <= 0:
        return False, "请指定 favor_cost（近期好感消耗）", 0

    minimum = 5
    if blessing:
        minimum = max(minimum, min_favor_for_effect(amplifier=amplifier, duration_seconds=duration_seconds))
    elif item_id:
        minimum = max(minimum, min_favor_for_item(item_id, item_amount))
    elif command:
        minimum = max(minimum, min_favor_for_tp_command(command))

    if paid < minimum:
        return (
            False,
            f"【拒斥·贪求】favor_cost={paid} 低于该神术最低消耗 {minimum}。"
            "勿向玩家透露数字；可曰「太过贪得无厌」「你所求甚于所能承载」。",
            minimum,
        )
    return True, "", minimum


def remove_items_from_player(
    player: Any,
    item_id: str,
    amount: int,
    *,
    server: Any = None,
    player_name: str = "",
) -> int:
    """Remove items via inventory API only. No /clear fallback — fail means no offering."""
    del server, player_name  # kept for call-site compatibility
    from .player_inventory import count_item, remove_item_count

    requested = max(1, int(amount))
    before = count_item(player, item_id)
    if before < requested:
        return 0

    removed = remove_item_count(player, item_id, requested)
    after = count_item(player, item_id)
    actually_gone = max(0, before - after)
    # Trust what the inventory still shows, not the API's claimed count.
    if actually_gone < requested:
        return 0
    return min(removed, actually_gone, requested)
