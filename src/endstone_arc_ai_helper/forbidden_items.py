"""Items that deity mode must never grant to players."""

from __future__ import annotations

from typing import Iterable

from .player_inventory import normalize_item_id

FORBIDDEN_GRANT_ITEMS = frozenset(
    {
        "minecraft:bedrock",
        "minecraft:barrier",
        "minecraft:command_block",
        "minecraft:chain_command_block",
        "minecraft:repeating_command_block",
        "minecraft:structure_block",
        "minecraft:structure_void",
        "minecraft:jigsaw",
        "minecraft:light_block",
        "minecraft:light_block_00",
        "minecraft:light_block_01",
        "minecraft:light_block_02",
        "minecraft:light_block_03",
        "minecraft:light_block_04",
        "minecraft:light_block_05",
        "minecraft:light_block_06",
        "minecraft:light_block_07",
        "minecraft:light_block_08",
        "minecraft:light_block_09",
        "minecraft:light_block_10",
        "minecraft:light_block_11",
        "minecraft:light_block_12",
        "minecraft:light_block_13",
        "minecraft:light_block_14",
        "minecraft:light_block_15",
        "minecraft:allow",
        "minecraft:deny",
        "minecraft:border_block",
        "minecraft:moving_block",
        "minecraft:end_portal_frame",
    }
)

FORBIDDEN_ID_FRAGMENTS = (
    "command_block",
    "structure_block",
    "light_block",
    "barrier",
    "bedrock",
)


def is_forbidden_grant_item(item_id: str) -> bool:
    normalized = normalize_item_id(item_id)
    if not normalized:
        return True
    if normalized in FORBIDDEN_GRANT_ITEMS:
        return True
    short = normalized.split(":", 1)[-1]
    for fragment in FORBIDDEN_ID_FRAGMENTS:
        if fragment in short:
            return True
    return False


def forbidden_items_hint() -> str:
    samples = sorted(
        {
            item.split(":", 1)[-1]
            for item in FORBIDDEN_GRANT_ITEMS
            if item.startswith("minecraft:")
        }
    )[:8]
    return "、".join(samples)
