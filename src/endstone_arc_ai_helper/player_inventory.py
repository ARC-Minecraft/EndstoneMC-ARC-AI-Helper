"""Lightweight player inventory helpers for ARC AI Helper."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

ARMOR_ATTRS = ("helmet", "chestplate", "leggings", "boots", "off_hand", "item_in_off_hand")

# Rough wealth points for sincerity comparison (internal only).
_ITEM_WEALTH_POINTS: Dict[str, int] = {
    "minecraft:netherite_ingot": 40,
    "minecraft:ancient_debris": 35,
    "minecraft:diamond_block": 45,
    "minecraft:emerald_block": 30,
    "minecraft:gold_block": 18,
    "minecraft:iron_block": 8,
    "minecraft:enchanted_golden_apple": 50,
    "minecraft:golden_apple": 12,
    "minecraft:diamond": 8,
    "minecraft:emerald": 6,
    "minecraft:gold_ingot": 4,
    "minecraft:iron_ingot": 2,
    "minecraft:ender_pearl": 2,
    "minecraft:obsidian": 1,
}

_ARMOR_WEALTH_POINTS: Dict[str, int] = {
    "netherite": 55,
    "diamond": 35,
    "iron": 12,
    "golden": 8,
    "chainmail": 10,
    "leather": 3,
}


def item_display_name(item_id: str) -> str:
    return _item_display_name(item_id)


def normalize_item_id(item_id: str) -> str:
    text = str(item_id or "").strip().lower()
    if not text:
        return ""
    if ":" not in text:
        return f"minecraft:{text}"
    return text


def _item_type_id(stack: Any) -> str:
    item_type = getattr(stack, "type", None)
    if item_type is None:
        return ""
    ident = getattr(item_type, "id", None)
    if ident:
        return str(ident).lower()
    return str(item_type).lower()


def _item_display_name(item_id: str) -> str:
    normalized = normalize_item_id(item_id)
    if normalized.startswith("minecraft:"):
        return normalized.split(":", 1)[1]
    return normalized


def _ids_match(stored: str, requested: str) -> bool:
    left = normalize_item_id(stored)
    right = normalize_item_id(requested)
    if left == right:
        return True
    return _item_display_name(left) == _item_display_name(right)


def summarize_inventory(player: Any, *, max_slots: int = 36) -> str:
    inv = getattr(player, "inventory", None)
    if inv is None:
        return "无法读取背包"
    lines: List[str] = []
    size = int(getattr(inv, "size", 0) or 0)
    limit = min(size, max(1, int(max_slots)))
    for idx in range(limit):
        try:
            stack = inv.get_item(idx)
        except Exception:
            continue
        if stack is None:
            continue
        amount = int(getattr(stack, "amount", 0) or 0)
        if amount <= 0:
            continue
        type_id = _item_type_id(stack)
        if not type_id or type_id == "minecraft:air":
            continue
        lines.append(f"槽{idx}: {_item_display_name(type_id)} x{amount}")
    if not lines:
        return "背包为空"
    return "\n".join(lines)


def inventory_item_counts(player: Any) -> Dict[str, int]:
    inv = getattr(player, "inventory", None)
    counts: Dict[str, int] = {}
    if inv is None:
        return counts
    size = int(getattr(inv, "size", 0) or 0)
    for idx in range(size):
        try:
            stack = inv.get_item(idx)
        except Exception:
            continue
        if stack is None:
            continue
        amount = int(getattr(stack, "amount", 0) or 0)
        if amount <= 0:
            continue
        type_id = _item_type_id(stack)
        if not type_id or type_id == "minecraft:air":
            continue
        counts[type_id] = counts.get(type_id, 0) + amount
    return counts


def count_item(player: Any, item_id: str) -> int:
    requested = normalize_item_id(item_id)
    total = 0
    for stored, amount in inventory_item_counts(player).items():
        if _ids_match(stored, requested):
            total += amount
    return total


def _reduced_stack(stack: Any, new_amount: int) -> Any:
    """Build a fresh ItemStack when possible — some Endstone builds ignore in-place mutation."""
    type_id = _item_type_id(stack)
    if not type_id:
        stack.amount = new_amount
        return stack
    try:
        from endstone.inventory import ItemStack

        data = int(getattr(stack, "data", 0) or 0)
        return ItemStack(type_id, max(1, int(new_amount)), data)
    except Exception:
        stack.amount = new_amount
        return stack


def remove_item_count(player: Any, item_id: str, amount: int) -> int:
    inv = getattr(player, "inventory", None)
    if inv is None:
        return 0
    remaining = max(0, int(amount))
    removed = 0
    size = int(getattr(inv, "size", 0) or 0)
    for idx in range(size):
        if remaining <= 0:
            break
        try:
            stack = inv.get_item(idx)
        except Exception:
            continue
        if stack is None:
            continue
        stored = _item_type_id(stack)
        if not _ids_match(stored, item_id):
            continue
        have = int(getattr(stack, "amount", 0) or 0)
        if have <= 0:
            continue
        take = min(have, remaining)
        new_amount = have - take
        try:
            if new_amount <= 0:
                inv.set_item(idx, None)
            else:
                inv.set_item(idx, _reduced_stack(stack, new_amount))
        except Exception:
            continue
        remaining -= take
        removed += take
    return removed


def find_online_player(server: Any, player_name: str) -> Tuple[Optional[Any], str]:
    target = str(player_name or "").strip()
    if not target:
        return None, ""
    direct = server.get_player(target)
    if direct is not None:
        return direct, str(getattr(direct, "name", "") or target)
    lowered = target.lower()
    for online in list(server.online_players or []):
        name = str(getattr(online, "name", "") or "")
        if name.lower() == lowered:
            return online, name
    return None, target


def _wealth_points_for_item(type_id: str, amount: int) -> int:
    normalized = normalize_item_id(type_id)
    short = _item_display_name(normalized)
    per = _ITEM_WEALTH_POINTS.get(normalized, 0)
    if per <= 0:
        if "netherite" in short:
            per = 45
        elif "diamond" in short and "block" not in short:
            per = 8
        elif "diamond_block" in short:
            per = 45
        elif short.endswith("_block"):
            per = 6
        elif short in ("bread", "apple", "cooked_beef", "cooked_porkchop"):
            per = 0
        else:
            per = 1
    return max(0, int(amount)) * per


def _armor_wealth_points(type_id: str) -> int:
    short = _item_display_name(normalize_item_id(type_id))
    for material, points in _ARMOR_WEALTH_POINTS.items():
        if material in short and any(
            token in short for token in ("helmet", "chestplate", "leggings", "boots")
        ):
            return points
    return 0


def collect_inventory_snapshot(player: Any) -> Dict[str, Any]:
    """Aggregate bag + worn gear for wealth / sincerity checks."""
    inv = getattr(player, "inventory", None)
    item_counts: Dict[str, int] = dict(inventory_item_counts(player))
    worn: List[str] = []

    if inv is not None:
        for attr in ARMOR_ATTRS:
            if not hasattr(inv, attr):
                continue
            try:
                stack = getattr(inv, attr, None)
            except Exception:
                continue
            if stack is None:
                continue
            amount = int(getattr(stack, "amount", 0) or 0)
            if amount <= 0 and getattr(stack, "type", None) is None:
                continue
            type_id = _item_type_id(stack)
            if not type_id or type_id == "minecraft:air":
                continue
            worn.append(f"{_item_display_name(type_id)}")
            item_counts[type_id] = item_counts.get(type_id, 0) + max(1, amount)

    wealth_score = 0
    highlights: List[str] = []
    for type_id, amount in sorted(item_counts.items(), key=lambda kv: -_wealth_points_for_item(kv[0], kv[1])):
        points = _wealth_points_for_item(type_id, amount)
        wealth_score += points
        if points >= 40:
            highlights.append(f"{_item_display_name(type_id)}×{amount}")

    armor_score = 0
    armor_notes: List[str] = []
    for piece in worn:
        pts = 0
        for material, mat_pts in _ARMOR_WEALTH_POINTS.items():
            if material in piece.lower():
                pts = mat_pts
                break
        if pts > 0:
            armor_score += pts
            armor_notes.append(piece)

    wealth_score += armor_score
    if wealth_score >= 220:
        tier = "豪富"
    elif wealth_score >= 120:
        tier = "殷实"
    elif wealth_score >= 45:
        tier = "小康"
    else:
        tier = "贫寒"

    return {
        "wealth_score": wealth_score,
        "wealth_tier": tier,
        "highlights": highlights[:8],
        "armor": armor_notes[:4],
        "worn": worn,
        "item_counts": item_counts,
    }


def assess_offering_sincerity(
    player: Any,
    offering_item_id: str,
    offering_amount: int,
) -> Dict[str, Any]:
    snapshot = collect_inventory_snapshot(player)
    wealth_score = int(snapshot.get("wealth_score", 0) or 0)
    tier = str(snapshot.get("wealth_tier") or "贫寒")
    try:
        amount = max(1, int(offering_amount))
    except Exception:
        amount = 1
    offering_points = _wealth_points_for_item(normalize_item_id(offering_item_id), amount)
    ratio = (offering_points / wealth_score) if wealth_score > 0 else 1.0

    armor = snapshot.get("armor") or []
    top_armor = any(
        any(mat in str(piece).lower() for mat in ("netherite", "diamond"))
        for piece in armor
    )
    has_blocks = any(
        "block" in key and _wealth_points_for_item(key, cnt) >= 40
        for key, cnt in (snapshot.get("item_counts") or {}).items()
    )

    stingy = False
    insulting = False
    reasons: List[str] = []

    if wealth_score >= 120 and offering_points < 15:
        stingy = True
        reasons.append("身家殷实却仅献上微薄之物")
    if wealth_score >= 220 and offering_points < 35:
        stingy = True
        insulting = True
        reasons.append("豪富之身，祭品不成比例")
    if top_armor and offering_points < 20:
        stingy = True
        reasons.append("身着宝甲却只拿出些许祭品")
    if has_blocks and offering_points < 25:
        stingy = True
        reasons.append("身怀贵重方块，献祭却过于吝啬")
    if wealth_score >= 80 and ratio < 0.04:
        stingy = True
        reasons.append("献祭仅占其身家极小一部分")
    if tier in ("贫寒", "小康") and offering_points >= 8:
        stingy = False
        insulting = False
        reasons = ["贫寒之人尽力而为，可视为真诚"]

    cold_hints = [
        "你的祭品配不上你的富足，神不接受敷衍。",
        "身怀珍宝，却拿这点东西糊弄天星？",
        "弧光看穿你的吝啬，神恩不会降临。",
        "诚不足，神不纳。",
    ]

    return {
        "wealth_score": wealth_score,
        "wealth_tier": tier,
        "offering_points": offering_points,
        "offering_ratio": round(ratio, 4),
        "is_stingy": stingy and not (tier in ("贫寒", "小康") and offering_points >= 8),
        "is_insulting": insulting,
        "reasons": reasons,
        "highlights": snapshot.get("highlights") or [],
        "armor": armor,
        "cold_hints": cold_hints,
    }


def format_inventory_report(
    player: Any,
    *,
    offering_item_id: str = "",
    offering_amount: int = 0,
) -> str:
    lines = [summarize_inventory(player)]
    snapshot = collect_inventory_snapshot(player)
    tier = snapshot.get("wealth_tier") or "?"
    highlights = snapshot.get("highlights") or []
    armor = snapshot.get("armor") or []
    profile = [f"【内部·身家侧写】{tier}"]
    if armor:
        profile.append("身着：" + "、".join(armor))
    if highlights:
        profile.append("贵重物：" + "、".join(highlights))
    lines.append("\n".join(profile))

    if offering_item_id:
        assessment = assess_offering_sincerity(player, offering_item_id, offering_amount or 1)
        reasons = "；".join(assessment.get("reasons") or [])
        if assessment.get("is_stingy"):
            hint = (assessment.get("cold_hints") or ["诚不足"])[0]
            lines.append(
                "【内部·献祭评估】吝啬/无诚意 → 建议冷淡拒收，勿调用 mc_accept_offering。"
                f"理由：{reasons}。可对玩家暗示：「{hint}」"
            )
        elif assessment.get("reasons"):
            lines.append(f"【内部·献祭评估】可酌情接纳。{reasons}")
    return "\n\n".join(lines)
