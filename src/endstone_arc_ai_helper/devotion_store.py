"""Per-player dual-layer devotion (long-term / short-term faith) for deity-mode AI."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Mapping, Tuple

# max_long_term <= 0 means uncapped.
_UNLIMITED_LONG = 2_147_483_647

DEFAULT_DEVOTION_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "mode": "deity",
    "max_long_term": 0,
    "default_long_term": 1,
    "long_growth_cap": 3,
    "admin_bypass_favor": True,
    "scripture_path": "scripture.txt",
    "titles": [
        {"min": 0, "title": "陌生者"},
        {"min": 10, "title": "初见信徒"},
        {"min": 100, "title": "虔信者"},
        {"min": 1000, "title": "神选之仆"},
        {"min": 10000, "title": "圣眷牧者"},
    ],
}

BLESSING_EFFECTS: Dict[str, str] = {
    "minor_blessing": "regeneration",
    "strength": "strength",
    "speed": "speed",
    "regeneration": "regeneration",
    "resistance": "resistance",
    "night_vision": "night_vision",
    "haste": "haste",
    "fire_resistance": "fire_resistance",
    "absorption": "absorption",
    "jump_boost": "jump_boost",
    "water_breathing": "water_breathing",
    "invisibility": "invisibility",
}

GROWTH_KINDS = frozenset({"prayer", "flattery", "offering", "dialogue"})

_VAGUE_SHORT_LOW = (
    "太过贪得无厌",
    "你所求甚于所能承载",
    "信仰之火将熄，休要再索",
    "神恩有价，你今日已透支虔诚",
)
_VAGUE_LONG_LOW = (
    "你还不够虔诚",
    "弧光尚未记住你的名字",
    "宿命的羁绊太浅，不配仰望神座",
    "凡心未诚，神不听呼",
)
_VAGUE_FORBIDDEN = (
    "此等造物非汝可承",
    "禁忌之物，天星不予",
)


def vague_short_insufficient() -> str:
    return _VAGUE_SHORT_LOW[0]


def vague_long_insufficient() -> str:
    return _VAGUE_LONG_LOW[0]


def narrative_hint_for_status(long_term: int, short_term: int, title: str, max_long: int) -> str:
    """Suggest how 天星 may speak to the player without numbers."""
    lines = [f"【信徒在你眼中的位格】{title or '陌生者'}"]
    if long_term < 10:
        lines.append(f"【当面可暗示】{vague_long_insufficient()}；可鼓励其多祈祷、献祭以「被世界记住」")
    elif long_term < 100:
        lines.append("【当面可暗示】虔诚初萌，神已垂目，但尚不可索重恩")
    elif long_term < 1000:
        lines.append("【当面可暗示】已是常客，可予小利，重恩仍需漫长积淀")
    elif long_term < 10000:
        lines.append("【当面可暗示】宿缘深厚，仍不可无度索取")
    else:
        lines.append("【当面可暗示】圣眷之位，神恩可酌，仍须代价")

    if short_term <= 0:
        lines.append(f"【若索恩被拒】{vague_short_insufficient()}")
    elif short_term < max(3, long_term // 4):
        lines.append("【若索恩】信仰余烬不多，仅宜小微神迹")
    elif short_term < max(1, long_term // 2):
        lines.append("【若索恩】可施中等神恩，勿应离谱之求")
    else:
        lines.append("【若索恩】近期虔诚充盈，可酌情应允（仍须扣费）")

    lines.append("【禁令】向玩家透露具体好感、点数、工具名、百分比；只用隐喻与神谕。")
    return "\n".join(lines)


def merge_devotion_config(raw: Mapping[str, Any] | None) -> Dict[str, Any]:
    merged = dict(DEFAULT_DEVOTION_CONFIG)
    if not isinstance(raw, Mapping):
        return merged
    for key, value in raw.items():
        if key == "titles" and isinstance(value, list):
            merged[key] = list(value)
        else:
            merged[key] = value
    return merged


class DevotionStore:
    def __init__(self, path: str, config: Mapping[str, Any] | None = None) -> None:
        self.path = str(path)
        self.config = merge_devotion_config(config)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"players": {}}
        self._load()
        # Ensure the data file exists so operators can find it on disk.
        with self._lock:
            if not os.path.exists(self.path):
                self._save_unlocked()

    def reload_config(self, config: Mapping[str, Any] | None) -> None:
        with self._lock:
            self.config = merge_devotion_config(config)

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                self._data = data
        except Exception:
            self._data = {"players": {}}

    def _save_unlocked(self) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump(self._data, file, ensure_ascii=False, indent=2)

    @staticmethod
    def _player_key(xuid: str, name: str) -> str:
        xuid = str(xuid or "").strip()
        if xuid:
            return f"xuid:{xuid}"
        return f"name:{str(name or '').strip().lower()}"

    def is_long_term_uncapped(self) -> bool:
        try:
            return int(self.config.get("max_long_term", 0) or 0) <= 0
        except Exception:
            return True

    def max_long_term(self) -> int:
        if self.is_long_term_uncapped():
            return _UNLIMITED_LONG
        try:
            return max(1, int(self.config.get("max_long_term", 100)))
        except Exception:
            return _UNLIMITED_LONG

    def long_growth_cap(self) -> int:
        try:
            return max(1, int(self.config.get("long_growth_cap", 3)))
        except Exception:
            return 3

    def default_long_term(self) -> int:
        try:
            return max(1, int(self.config.get("default_long_term", 1)))
        except Exception:
            return 1

    def title_for_long_term(self, long_term: int) -> str:
        titles = self.config.get("titles") or []
        current = "信徒"
        best_min = -1
        for entry in titles:
            if not isinstance(entry, dict):
                continue
            try:
                minimum = int(entry.get("min", 0))
            except Exception:
                minimum = 0
            title = str(entry.get("title") or "").strip()
            if minimum >= best_min and long_term >= minimum and title:
                best_min = minimum
                current = title
        return current

    def _migrate_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        if "long_term" not in record:
            legacy = int(record.get("favor", 0) or 0)
            record["long_term"] = max(self.default_long_term(), legacy)
        if "short_term" not in record:
            record["short_term"] = 0
        record["long_term"] = max(1, min(self.max_long_term(), int(record.get("long_term", 1) or 1)))
        short_cap = int(record["long_term"])
        record["short_term"] = max(0, min(short_cap, int(record.get("short_term", 0) or 0)))
        record.pop("favor", None)
        return record

    def get_record(self, *, xuid: str = "", name: str = "") -> Dict[str, Any]:
        key = self._player_key(xuid, name)
        with self._lock:
            players = self._data.setdefault("players", {})
            record = players.get(key)
            created = False
            if not isinstance(record, dict):
                record = {
                    "name": str(name or "").strip(),
                    "xuid": str(xuid or "").strip(),
                    "long_term": self.default_long_term(),
                    "short_term": 0,
                    "total_offerings": 0,
                    "total_prayers": 0,
                    "updated_at": 0.0,
                }
                players[key] = record
                created = True
            if name and not record.get("name"):
                record["name"] = str(name).strip()
            if xuid and not record.get("xuid"):
                record["xuid"] = str(xuid).strip()
            record = self._migrate_record(record)
            long_term = int(record["long_term"])
            record["title"] = self.title_for_long_term(long_term)
            if created:
                self._save_unlocked()
            return dict(record)

    def format_status(self, *, xuid: str = "", name: str = "") -> str:
        record = self.get_record(xuid=xuid, name=name)
        long_term = int(record.get("long_term", 1) or 1)
        short_term = int(record.get("short_term", 0) or 0)
        title = str(record.get("title") or self.title_for_long_term(long_term))
        offerings = int(record.get("total_offerings", 0) or 0)
        prayers = int(record.get("total_prayers", 0) or 0)
        long_disp = str(long_term) if self.is_long_term_uncapped() else f"{long_term}/{self.max_long_term()}"
        internal = (
            f"【内部数值·勿告玩家】长期={long_disp}; "
            f"近期={short_term}/{long_term}; 称号={title}; "
            f"累计献祭={offerings}; 累计祈祷={prayers}"
        )
        hint = narrative_hint_for_status(long_term, short_term, title, self.max_long_term())
        return f"{internal}\n{hint}"

    @staticmethod
    def auto_split_gain(total: int, long_term: int, short_term: int, *, long_cap: int) -> Tuple[int, int]:
        """Fill short-term first (up to long-term cap), then slow long-term growth."""
        try:
            points = max(0, int(total))
        except Exception:
            points = 0
        if points <= 0:
            return 0, 0
        room_short = max(0, int(long_term) - int(short_term))
        to_short = min(points, room_short)
        remaining = points - to_short
        to_long = min(remaining, max(1, int(long_cap)))
        return to_short, to_long

    def adjust_faith(
        self,
        *,
        xuid: str = "",
        name: str = "",
        short_delta: int = 0,
        long_delta: int = 0,
        reason: str = "",
        kind: str = "adjust",
    ) -> Tuple[bool, str, Dict[str, int]]:
        try:
            short_change = int(short_delta)
            long_change = int(long_delta)
        except Exception:
            return False, "short_delta / long_delta 必须是整数", {"long_term": 0, "short_term": 0}

        if short_change == 0 and long_change == 0:
            record = self.get_record(xuid=xuid, name=name)
            return True, "好感度未变化", {
                "long_term": int(record.get("long_term", 1) or 1),
                "short_term": int(record.get("short_term", 0) or 0),
            }

        kind_text = str(kind or "adjust").strip().lower()
        reason_text = str(reason or "").strip()

        if long_change > 0 and kind_text in GROWTH_KINDS:
            long_change = min(long_change, self.long_growth_cap())

        key = self._player_key(xuid, name)
        with self._lock:
            players = self._data.setdefault("players", {})
            record = players.get(key)
            if not isinstance(record, dict):
                record = {
                    "name": str(name or "").strip(),
                    "xuid": str(xuid or "").strip(),
                    "long_term": self.default_long_term(),
                    "short_term": 0,
                    "total_offerings": 0,
                    "total_prayers": 0,
                    "updated_at": 0.0,
                }
                players[key] = record
            record = self._migrate_record(record)

            long_term = int(record["long_term"])
            short_term = int(record["short_term"])

            if short_change < 0:
                need = abs(short_change)
                if short_term < need:
                    return (
                        False,
                        f"【拒斥·贪求】近期信仰不足（内部 当前{short_term} 需{need}）。"
                        f"勿向玩家透露数字；可曰「{vague_short_insufficient()}」",
                        {"long_term": long_term, "short_term": short_term},
                    )
                short_term -= need
            elif short_change > 0:
                short_term = min(long_term, short_term + short_change)

            if long_change < 0:
                long_term = max(1, long_term + long_change)
                short_term = min(short_term, long_term)
            elif long_change > 0:
                long_term = min(self.max_long_term(), long_term + long_change)
                short_term = min(short_term, long_term)

            record["long_term"] = long_term
            record["short_term"] = short_term
            record["name"] = str(name or record.get("name") or "").strip()
            record["xuid"] = str(xuid or record.get("xuid") or "").strip()
            record["updated_at"] = time.time()
            if kind_text == "offering" and (short_change > 0 or long_change > 0):
                record["total_offerings"] = int(record.get("total_offerings", 0) or 0) + 1
            if kind_text in ("prayer", "flattery") and (short_change > 0 or long_change > 0):
                record["total_prayers"] = int(record.get("total_prayers", 0) or 0) + 1
            if reason_text:
                record["last_reason"] = reason_text
            record["title"] = self.title_for_long_term(long_term)
            self._save_unlocked()

            title = str(record.get("title") or "")
            long_disp = str(long_term) if self.is_long_term_uncapped() else f"{long_term}/{self.max_long_term()}"
            if short_change != 0 or long_change != 0:
                message = (
                    f"【内部】近期={short_term}/{long_term} 长期={long_disp}（{title}）。"
                    "向玩家只用神谕语气反馈，勿报数字。"
                )
            else:
                message = "【内部】信仰未变。向玩家可沉默或淡淡回应。"
            return True, message, {"long_term": long_term, "short_term": short_term}

    def consume_short_favor(
        self,
        *,
        xuid: str = "",
        name: str = "",
        cost: int,
        reason: str = "",
    ) -> Tuple[bool, str, Dict[str, int]]:
        try:
            amount = max(0, int(cost))
        except Exception:
            return False, "favor_cost 必须是正整数", {"long_term": 0, "short_term": 0}
        if amount <= 0:
            return False, "favor_cost 必须大于 0", {"long_term": 0, "short_term": 0}
        return self.adjust_faith(
            xuid=xuid,
            name=name,
            short_delta=-amount,
            long_delta=0,
            reason=reason,
            kind="blessing",
        )

    def resolve_blessing_effect(self, blessing: str) -> str:
        key = str(blessing or "").strip().lower()
        return BLESSING_EFFECTS.get(key, key)
