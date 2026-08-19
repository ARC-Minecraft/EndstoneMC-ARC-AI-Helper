"""Validate safe self-help commands for QQ-bound players (non-admin)."""

from __future__ import annotations

import re

_FORBIDDEN_ROOTS = frozenset(
    {
        "stop",
        "kill",
        "gamemode",
        "give",
        "clear",
        "summon",
        "setblock",
        "fill",
        "clone",
        "op",
        "deop",
        "ban",
        "kick",
        "jail",
        "say",
        "tellraw",
        "scoreboard",
        "tag",
        "replaceitem",
        "enchant",
        "xp",
        "experience",
        "weather",
        "time",
        "difficulty",
        "gamerule",
        "worldbuilder",
        "permission",
    }
)

_HARMFUL_EFFECTS = frozenset(
    {
        "instant_damage",
        "poison",
        "wither",
        "fatal_poison",
        "darkness",
        "blindness",
        "mining_fatigue",
        "weakness",
        "slowness",
        "nausea",
        "levitation",
    }
)

_EXECUTE_SELF_HELP = re.compile(r"\brun\s+(tp|effect|spawnpoint)\b", re.IGNORECASE)


def _looks_like_coordinate(token: str) -> bool:
    text = str(token or "").strip().lower()
    if not text:
        return False
    if text.startswith("~"):
        return True
    try:
        float(text)
        return True
    except ValueError:
        return False


def _names_match(left: str, right: str) -> bool:
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def validate_bound_self_help_command(command: str, bound_player: str) -> tuple[bool, str]:
    """Return whether a console command is allowed for bound QQ self-help.

    Args:
        command: Raw game command without a leading slash.
        bound_player: Bound in-game player name for the QQ sender.

    Returns:
        ``(ok, error_message)``. ``error_message`` is empty when ``ok`` is True.
    """
    bound = str(bound_player or "").strip()
    if not bound:
        return False, "未找到绑定的游戏角色"

    normalized = str(command or "").strip().lstrip("/").strip()
    if not normalized:
        return False, "命令为空"

    parts = normalized.split()
    root = parts[0].lower() if parts else ""
    if root in _FORBIDDEN_ROOTS:
        return False, f"该指令不允许通过求助执行: /{root}"

    if root == "execute":
        if not _EXECUTE_SELF_HELP.search(normalized):
            return False, "execute 仅允许用于 tp / effect / spawnpoint 自救"
        return _ensure_bound_player_target(normalized, bound)

    if root == "tp":
        return _validate_tp_command(parts, bound)

    if root == "effect":
        return _validate_effect_command(parts, bound)

    if root == "spawnpoint":
        return _validate_spawnpoint_command(parts, bound)

    return False, "求助仅允许 tp / effect / spawnpoint 等自救指令"


def _ensure_bound_player_target(command: str, bound: str) -> tuple[bool, str]:
    lowered = command.lower()
    if bound.lower() not in lowered:
        return False, f"指令必须作用于您的绑定角色「{bound}」"
    if re.search(r"@[a-z]", lowered):
        return False, "求助指令不可使用 @ 选择器"
    return True, ""


def _validate_tp_command(parts: list[str], bound: str) -> tuple[bool, str]:
    if len(parts) < 2:
        return False, "tp 指令格式不完整"
    target = parts[1]
    if target.startswith("@") or not _names_match(target, bound):
        return False, f"只能对您的绑定角色「{bound}」执行 tp"
    if len(parts) >= 3 and not _looks_like_coordinate(parts[2]):
        return False, "求助 tp 只能传送到坐标，不能传送至其他玩家"
    return True, ""


def _validate_effect_command(parts: list[str], bound: str) -> tuple[bool, str]:
    if len(parts) < 2:
        return False, "effect 指令格式不完整"
    target = parts[1]
    if target.startswith("@") or not _names_match(target, bound):
        return False, f"effect 只能作用于您的绑定角色「{bound}」"
    if len(parts) >= 3 and parts[2].lower() in _HARMFUL_EFFECTS:
        return False, "求助不允许使用负面效果"
    return True, ""


def _validate_spawnpoint_command(parts: list[str], bound: str) -> tuple[bool, str]:
    if len(parts) < 2:
        return False, "spawnpoint 指令格式不完整"
    target = parts[1]
    if target.startswith("@") or not _names_match(target, bound):
        return False, f"spawnpoint 只能作用于您的绑定角色「{bound}」"
    return True, ""
