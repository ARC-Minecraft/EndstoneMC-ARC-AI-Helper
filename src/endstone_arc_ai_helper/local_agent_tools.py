"""OpenAI-compatible tool schemas for local (non-AstrBot) Agent mode."""

from __future__ import annotations

from typing import Any, Dict, List

# Hub / OpenAI tool name → run_ai_tool action
TOOL_NAME_TO_ACTION: Dict[str, str] = {
    "mc_list_players": "list",
    "mc_get_tps": "tps",
    "mc_server_info": "info",
    "mc_run_command": "cmd",
    "mc_economy": "economy",
    "mc_land": "land",
    "mc_landmarks": "landmarks",
    "mc_jail_player": "jail",
    "mc_release_player": "release",
    "mc_list_prisoners": "prisoners",
    "mc_skyeye_player": "skyeye_player",
    "mc_skyeye_combat": "skyeye_combat",
    "mc_skyeye_location": "skyeye_location",
}


def _fn(name: str, description: str, properties: Dict[str, Any], required: List[str] | None = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def build_local_agent_tools(*, has_prison: bool = False, has_arc_core: bool = False) -> List[Dict[str, Any]]:
    """Return OpenAI tools list for local Agent loop."""
    tools: List[Dict[str, Any]] = [
        _fn(
            "mc_list_players",
            "列出当前服务器在线玩家。",
            {},
        ),
        _fn(
            "mc_get_tps",
            "查询服务器 TPS / MSPT / Tick 使用率。",
            {},
        ),
        _fn(
            "mc_server_info",
            "查询服务器名称、版本、运行时长、在线人数等。",
            {},
        ),
        _fn(
            "mc_run_command",
            "在服务器控制台执行一条 Minecraft 指令（不要带开头斜杠）。"
            "权限受三档限制：助手仅 tp/give/effect 等；管理员大部分指令；代理服主全部。"
            "禁止滥用 stop/kill；敏感权限指令仅代理服主。",
            {
                "command": {
                    "type": "string",
                    "description": "不含开头斜杠的控制台指令，例如 give Steve diamond 1",
                }
            },
            ["command"],
        ),
    ]
    if has_prison:
        tools.extend(
            [
                _fn(
                    "mc_jail_player",
                    "把玩家关进监狱（管理员及以上）。minutes 为分钟，-1 或「无期」为无期徒刑。",
                    {
                        "player_name": {"type": "string", "description": "玩家名"},
                        "minutes": {
                            "type": "string",
                            "description": "刑期分钟，可空；-1/无期=无期",
                        },
                        "reason": {"type": "string", "description": "入狱原因，可空"},
                    },
                    ["player_name"],
                ),
                _fn(
                    "mc_release_player",
                    "释放在押玩家（管理员及以上）。",
                    {"player_name": {"type": "string", "description": "玩家名"}},
                    ["player_name"],
                ),
                _fn(
                    "mc_list_prisoners",
                    "查看当前在押名单。",
                    {},
                ),
            ]
        )
    if has_arc_core:
        tools.extend(
            [
                _fn(
                    "mc_landmarks",
                    "查询本服公开地标：出生点、公共传送点(Warp)、公共领地/功能区。"
                    "玩家问路标或功能建筑时优先看系统提示里的地标清单；需要最新数据再调用本工具。"
                    "只读，助手级别也可用。",
                    {},
                ),
                _fn(
                    "mc_economy",
                    "弧光银行：query 查余额（查自己无需管理员）；"
                    "transfer 从自己账户发红包/转账（targets 收款人，to_online=true 发给当前在线玩家，amount 每人金额）；"
                    "change 加减钱需管理员。",
                    {
                        "player_name": {"type": "string"},
                        "xuid": {"type": "string"},
                        "sub_action": {
                            "type": "string",
                            "description": "query / transfer / change",
                        },
                        "delta": {
                            "type": "number",
                            "description": "change 时的变动金额，正加负减",
                        },
                        "amount": {
                            "type": "number",
                            "description": "transfer 时每人金额，或 change 的 delta",
                        },
                        "targets": {
                            "type": "string",
                            "description": "transfer 收款人，逗号分隔；发全员红包可填 online",
                        },
                        "to_online": {
                            "type": "string",
                            "description": "true 时发给当前服在线且非自己的玩家",
                        },
                    },
                ),
                _fn(
                    "mc_land",
                    "弧光领地：list/info/at（管理员及以上）。",
                    {
                        "player_name": {"type": "string"},
                        "xuid": {"type": "string"},
                        "sub_action": {
                            "type": "string",
                            "description": "list / info / at",
                        },
                        "land_id": {"type": "integer"},
                        "dimension": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                    },
                ),
                _fn(
                    "mc_arc_tp",
                    "弧光传送：home / warp / pos（管理员及以上）。玩家须在线。",
                    {
                        "player_name": {"type": "string"},
                        "sub_action": {
                            "type": "string",
                            "description": "home / warp / pos",
                        },
                        "home_name": {"type": "string"},
                        "warp_name": {"type": "string"},
                        "name": {"type": "string"},
                        "dimension": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                    },
                    ["player_name"],
                ),
                _fn(
                    "mc_skyeye_player",
                    "天眼：查玩家位置与近期行为（管理员及以上）。不要求在线。"
                    "minutes 由用户说法换算，一天=1440。",
                    {
                        "player_name": {"type": "string"},
                        "minutes": {"type": "string"},
                        "action": {"type": "string"},
                    },
                    ["player_name"],
                ),
                _fn(
                    "mc_skyeye_combat",
                    "天眼：查玩家打架/被打/死亡（管理员及以上）。",
                    {
                        "player_name": {"type": "string"},
                        "minutes": {"type": "string"},
                    },
                    ["player_name"],
                ),
                _fn(
                    "mc_skyeye_location",
                    "天眼：查坐标附近活动（管理员及以上）。",
                    {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "radius": {"type": "number"},
                        "dimension": {"type": "string"},
                        "minutes": {"type": "string"},
                    },
                    ["x", "y", "z"],
                ),
            ]
        )
    return tools


def resolve_tool_action(tool_name: str) -> str:
    name = str(tool_name or "").strip()
    return TOOL_NAME_TO_ACTION.get(name, name)
