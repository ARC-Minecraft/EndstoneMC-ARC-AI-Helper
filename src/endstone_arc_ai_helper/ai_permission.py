"""Three-tier AI command permission levels for ARC AI Helper."""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Any, Mapping

_EXECUTE_ASSISTANT = re.compile(
    r"\brun\s+(tp|teleport|effect|give|spawnpoint)\b",
    re.IGNORECASE,
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


class AIPermissionLevel(IntEnum):
    """AI Helper permission tiers (low → high)."""

    ASSISTANT = 1
    ADMIN = 2
    PROXY_OWNER = 3


LEVEL_ALIASES: dict[str, AIPermissionLevel] = {
    "assistant": AIPermissionLevel.ASSISTANT,
    "admin": AIPermissionLevel.ADMIN,
    "proxy_owner": AIPermissionLevel.PROXY_OWNER,
    "proxyowner": AIPermissionLevel.PROXY_OWNER,
    "owner": AIPermissionLevel.PROXY_OWNER,
    "助手": AIPermissionLevel.ASSISTANT,
    "管理员": AIPermissionLevel.ADMIN,
    "代理服主": AIPermissionLevel.PROXY_OWNER,
    "服主": AIPermissionLevel.PROXY_OWNER,
}


LEVEL_DISPLAY: dict[AIPermissionLevel, str] = {
    AIPermissionLevel.ASSISTANT: "助手",
    AIPermissionLevel.ADMIN: "管理员",
    AIPermissionLevel.PROXY_OWNER: "代理服主",
}


# Only proxy owner may run these root commands.
PROXY_OWNER_ONLY_ROOTS = frozenset(
    {
        "stop",
        "kill",
        "op",
        "deop",
        "ban",
        "ban-ip",
        "banlist",
        "pardon",
        "pardon-ip",
        "kick",
        "permission",
        "permissions",
        "whitelist",
        "setmaxplayers",
        "reload",
        "restart",
        "save-all",
        "save-on",
        "save-off",
    }
)


# Assistant may only use these roots (plus limited execute).
ASSISTANT_ALLOWED_ROOTS = frozenset(
    {
        "tp",
        "teleport",
        "effect",
        "give",
        "spawnpoint",
        "tell",
        "msg",
        "w",
        "whisper",
        "particle",
        "playsound",
        "execute",
    }
)


def parse_permission_level(raw: Any, default: AIPermissionLevel = AIPermissionLevel.ASSISTANT) -> AIPermissionLevel:
    """Parse a config / payload permission level string."""
    text = str(raw or "").strip().lower()
    if not text:
        return default
    if text.isdigit():
        try:
            value = int(text)
            if value in (1, 2, 3):
                return AIPermissionLevel(value)
        except ValueError:
            pass
    return LEVEL_ALIASES.get(text, default)


def level_display(level: AIPermissionLevel) -> str:
    return LEVEL_DISPLAY.get(level, "助手")


def resolve_permission_level(
    *,
    player: Any = None,
    chat_config: Mapping[str, Any] | None = None,
    payload_level: Any = None,
    payload_is_op: bool = False,
    op_maps_to_admin: bool = True,
) -> AIPermissionLevel:
    """Resolve the effective AI permission level for a player or Hub payload."""
    cfg = chat_config or {}
    default = parse_permission_level(cfg.get("default_permission_level"), AIPermissionLevel.ASSISTANT)
    level = default

    overrides = cfg.get("permission_overrides") or {}
    if isinstance(overrides, dict):
        keys: list[str] = []
        if player is not None:
            xuid = str(getattr(player, "xuid", "") or "").strip()
            name = str(getattr(player, "name", "") or "").strip()
            if xuid:
                keys.append(xuid)
            if name:
                keys.append(name)
                keys.append(name.lower())
        for key in keys:
            if key in overrides:
                level = max(level, parse_permission_level(overrides[key], default))
                break

    perm_api = getattr(player, "has_permission", None) if player is not None else None
    if callable(perm_api):
        if perm_api("arc_ai_helper.permission.proxy_owner"):
            level = AIPermissionLevel.PROXY_OWNER
        elif perm_api("arc_ai_helper.permission.admin"):
            level = max(level, AIPermissionLevel.ADMIN)
        elif perm_api("arc_ai_helper.permission.assistant"):
            level = max(level, AIPermissionLevel.ASSISTANT)

    if op_maps_to_admin and player is not None and bool(getattr(player, "is_op", False)):
        level = max(level, AIPermissionLevel.ADMIN)

    if payload_level not in (None, ""):
        level = max(level, parse_permission_level(payload_level, default))

    if payload_is_op and op_maps_to_admin:
        level = max(level, AIPermissionLevel.ADMIN)

    return level


def validate_command_for_level(
    command: str,
    level: AIPermissionLevel,
    *,
    bound_player_name: str = "",
    is_bound_self_help: bool = False,
) -> tuple[bool, str]:
    """Return whether a console command is allowed for the given AI permission level."""
    normalized = str(command or "").strip().lstrip("/").strip()
    if not normalized:
        return False, "命令为空"

    parts = normalized.split()
    root = parts[0].lower() if parts else ""

    if level >= AIPermissionLevel.PROXY_OWNER:
        return True, ""

    if root in PROXY_OWNER_ONLY_ROOTS:
        return False, f"该指令仅代理服主级别可用: /{root}"

    if is_bound_self_help and level < AIPermissionLevel.ADMIN:
        from .bound_self_help import validate_bound_self_help_command

        return validate_bound_self_help_command(normalized, bound_player_name)

    if level >= AIPermissionLevel.ADMIN:
        return True, ""

    if root not in ASSISTANT_ALLOWED_ROOTS:
        return False, f"助手级别不可用 /{root}，需要管理员及以上权限"

    if root == "execute":
        if not _EXECUTE_ASSISTANT.search(normalized):
            return False, "execute 仅允许 tp / effect / give / spawnpoint"
        return True, ""

    if root == "effect" and len(parts) >= 3 and parts[2].lower() in _HARMFUL_EFFECTS:
        return False, "助手级别不允许使用负面效果"

    if root == "give" and len(parts) >= 2 and parts[1].startswith("@"):
        return False, "助手级别 give 不可使用 @ 选择器"

    return True, ""


def require_admin_level(level: AIPermissionLevel) -> str:
    """Return an error message when admin+ is required, or empty string if allowed."""
    if level >= AIPermissionLevel.ADMIN:
        return ""
    return f"没有权限：该功能需要管理员（{level_display(AIPermissionLevel.ADMIN)}）及以上级别。"
