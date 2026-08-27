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
    "mc_skyeye_events": "skyeye_events",
    "mc_stock_leaderboard": "stock_leaderboard",
    "mc_stock_quote": "stock_quote",
    "mc_player_ip": "player_ip",
    "mc_devotion_status": "devotion_status",
    "mc_devotion_adjust": "devotion_adjust",
    "mc_player_inventory": "player_inventory",
    "mc_accept_offering": "accept_offering",
    "mc_grant_blessing": "grant_blessing",
    "mc_divine_intervention": "divine_intervention",
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


def build_devotion_agent_tools() -> List[Dict[str, Any]]:
    return [
        _fn(
            "mc_devotion_status",
            "查询玩家长期好感、近期好感（上限=长期）、称号与献祭/祈祷次数。"
            "任何神恩前先查状态。",
            {
                "player_name": {
                    "type": "string",
                    "description": "可选；默认当前对话玩家",
                }
            },
        ),
        _fn(
            "mc_devotion_adjust",
            "调整长期/近期好感。祈祷赞美献祭后由你判定增量："
            "先补近期（不超过长期上限），长期单次最多+5且宜缓慢增长。"
            "惩罚可扣近期或长期。赐福消耗请用 mc_divine_intervention，勿在此重复扣费。",
            {
                "short_delta": {
                    "type": "integer",
                    "description": "近期好感变化（可正可负）",
                },
                "long_delta": {
                    "type": "integer",
                    "description": "长期好感变化；祈祷/献祭时正增长单次≤5",
                },
                "reason": {"type": "string", "description": "原因"},
                "kind": {
                    "type": "string",
                    "description": "prayer / flattery / offering / punishment / adjust",
                },
                "player_name": {"type": "string", "description": "可选"},
            },
        ),
        _fn(
            "mc_player_inventory",
            "查看背包、身穿装备与身家侧写。献祭前必调；可传 offering_item_id + offering_amount 预判诚意。",
            {
                "player_name": {"type": "string", "description": "可选"},
                "offering_item_id": {
                    "type": "string",
                    "description": "可选；拟献祭物品，用于诚意评估",
                },
                "offering_amount": {
                    "type": "integer",
                    "description": "可选；拟献祭数量",
                },
            },
        ),
        _fn(
            "mc_accept_offering",
            "收取献祭：扣背包物品。必须先查背包评估诚意；身怀巨富却献少量会被拒收。",
            {
                "item_id": {"type": "string", "description": "如 diamond"},
                "amount": {"type": "integer", "description": "数量，默认 1"},
                "short_gain": {
                    "type": "integer",
                    "description": "可选；给予近期好感",
                },
                "long_gain": {
                    "type": "integer",
                    "description": "可选；给予长期好感（≤5）",
                },
                "total_gain": {
                    "type": "integer",
                    "description": "可选；未拆分时按总量自动分配",
                },
                "player_name": {"type": "string", "description": "可选"},
            },
            ["item_id"],
        ),
        _fn(
            "mc_divine_intervention",
            "施行神术：必须指定 favor_cost（消耗近期好感）。"
            "近期不足则失败。可 effect / give / tp / 自定义 command（如雷霆）。"
            "禁止给予基岩、屏障、命令方块等超模物品。",
            {
                "favor_cost": {
                    "type": "integer",
                    "description": "消耗的近期好感，由你根据神术规模自定",
                },
                "blessing": {
                    "type": "string",
                    "description": "可选；药水效果名 strength/speed 等",
                },
                "duration_seconds": {"type": "integer", "description": "效果秒数"},
                "amplifier": {"type": "integer", "description": "效果等级 0=I"},
                "item_id": {"type": "string", "description": "可选；赐予物品"},
                "item_amount": {"type": "integer", "description": "物品数量"},
                "command": {
                    "type": "string",
                    "description": "可选；控制台指令（无斜杠），如雷霆、tp",
                },
                "player_name": {"type": "string", "description": "受益玩家，默认为自己"},
            },
            ["favor_cost"],
        ),
        _fn(
            "mc_grant_blessing",
            "（兼容）等同 mc_divine_intervention 仅施放 effect；须 favor_cost。",
            {
                "favor_cost": {"type": "integer"},
                "blessing": {"type": "string"},
                "duration_seconds": {"type": "integer"},
                "amplifier": {"type": "integer"},
                "player_name": {"type": "string"},
            },
            ["favor_cost", "blessing"],
        ),
    ]


def build_local_agent_tools(
    *,
    has_prison: bool = False,
    has_arc_core: bool = False,
    has_stock: bool = False,
    has_devotion: bool = False,
) -> List[Dict[str, Any]]:
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
        _fn(
            "mc_player_ip",
            "查询本服在线玩家的连接 IP（原始地址）。"
            "需要做天气问候、IP 地理定位等时先调用本工具拿到 ip= 字段，再把该 IP 原样传给你的其它工具。"
            "不填 player_name 则查当前对话玩家；助手只能查自己，管理员可查任意在线玩家。",
            {
                "player_name": {
                    "type": "string",
                    "description": "可选；目标玩家名，默认当前对话玩家",
                }
            },
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
    if has_stock:
        tools.extend(
            [
                _fn(
                    "mc_stock_leaderboard",
                    "查询服务器模拟美股插件玩家盈亏排行榜（高手榜/接盘侠榜）。"
                    "问谁赚最多、收益率排行、某玩家股票盈亏时必须调用，禁止编造。"
                    "只读，助手级别也可用。",
                    {
                        "mode": {
                            "type": "string",
                            "description": "relative=收益率（默认）/ absolute=绝对盈亏金额",
                        },
                        "top": {
                            "type": "string",
                            "description": "前 N 名，默认 5",
                        },
                        "bottom": {
                            "type": "string",
                            "description": "倒数 N 名，默认 5",
                        },
                        "player_name": {
                            "type": "string",
                            "description": "可选；只查该玩家名次与盈亏",
                        },
                    },
                ),
                _fn(
                    "mc_stock_quote",
                    "查询单只股票/加密货币现价或走势（AAPL、TSLA、BTC-USD 等）。"
                    "问股价、涨跌、走势时必须调用，禁止编造。只读。",
                    {
                        "symbol": {
                            "type": "string",
                            "description": "股票代码，如 AAPL",
                        },
                        "period": {
                            "type": "string",
                            "description": "price=仅现价；minute/day/month=走势，默认 day",
                        },
                    },
                    ["symbol"],
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
                    "player_name 支持模糊匹配（名字不全也对）。"
                    "minutes 由用户说法换算，一天=1440。"
                    "action 可选：death/pvp/break 等类别或 PlayerDeath 精确名。",
                    {
                        "player_name": {
                            "type": "string",
                            "description": "玩家名，支持模糊/子串；可略写",
                        },
                        "minutes": {"type": "string"},
                        "action": {
                            "type": "string",
                            "description": "可选事件类型：death/pvp/pve/combat/break/place/join/quit/chat 等",
                        },
                    },
                    ["player_name"],
                ),
                _fn(
                    "mc_skyeye_combat",
                    "天眼：查打架/被打/死亡（管理员及以上）。"
                    "player_name 可空=全服；支持模糊名。"
                    "event_kind：combat(默认)/pvp/pve/death/pvp_death。",
                    {
                        "player_name": {
                            "type": "string",
                            "description": "可选。空则查全服该类型事件",
                        },
                        "minutes": {"type": "string"},
                        "event_kind": {
                            "type": "string",
                            "description": "combat|pvp|pve|death|pvp_death|pvp_hit|pve_death",
                        },
                    },
                ),
                _fn(
                    "mc_skyeye_events",
                    "天眼：按事件类型查全服或某人（管理员及以上）。"
                    "问「最近24小时谁死了」「有没有PvP」必须用本工具；可不传玩家名。"
                    "action 必填：death/pvp/pve/combat/kill/join/quit/break/place/chat/command/teleport/economy/land/shop 等，"
                    "也可 PlayerDeath 等精确名。player_name 可选且模糊。",
                    {
                        "action": {
                            "type": "string",
                            "description": "事件类型或别名，如 death、pvp、死亡、打怪",
                        },
                        "player_name": {
                            "type": "string",
                            "description": "可选模糊玩家名；空=全服该类型",
                        },
                        "minutes": {"type": "string"},
                    },
                    ["action"],
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
                        "action": {
                            "type": "string",
                            "description": "可选事件类型过滤",
                        },
                    },
                    ["x", "y", "z"],
                ),
            ]
        )
    if has_devotion:
        tools.extend(build_devotion_agent_tools())
    return tools


def resolve_tool_action(tool_name: str) -> str:
    name = str(tool_name or "").strip()
    return TOOL_NAME_TO_ACTION.get(name, name)
