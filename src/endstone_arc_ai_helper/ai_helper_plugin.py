import html
import json
import os
import queue
import re
import threading
import time
from typing import Any, Dict, List
from datetime import datetime

from endstone.event import PlayerChatEvent, PlayerJoinEvent, PlayerDeathEvent, event_handler
from endstone.plugin import Plugin

try:
    from endstone.command import CommandSenderWrapper
except ImportError:
    CommandSenderWrapper = None  # type: ignore

from .ai_permission import (
    AIPermissionLevel,
    level_display,
    require_admin_level,
    resolve_permission_level,
    validate_command_for_level,
)
from .astrbot_hub_client import AstrBotHubChatClient
from .chat_ai_manager import ChatAIManager
from .devotion_store import DEFAULT_DEVOTION_CONFIG, DevotionStore, merge_devotion_config
from .devotion_guards import (
    clamp_player_blessing,
    should_block_devotion_bypass,
    remove_items_from_player,
    validate_divine_favor_cost,
)
from .forbidden_items import forbidden_items_hint, is_forbidden_grant_item
from .local_agent_tools import build_local_agent_tools, resolve_tool_action
from .player_inventory import (
    assess_offering_sincerity,
    count_item,
    find_online_player,
    format_inventory_report,
    item_display_name,
    normalize_item_id,
    summarize_inventory,
)

# Mutating / audit-worthy tools written to ARCCore sky eye.
_SKY_EYE_AUDIT_ACTIONS = frozenset(
    {
        "economy",
        "money",
        "bank",
        "land",
        "lands",
        "arc_tp",
        "arc_teleport",
        "core_tp",
        "jail",
        "release",
    }
)


DEFAULT_PERSONA = (
    "你是Minecraft服务器中的弧光Agent「天星」，负责协助管理与服务本服玩家。"
    "请用友好、简洁的中文回答，并结合游戏内背景解释。"
)

DEFAULT_SYSTEM_PROMPT = (
    "你运行在 Minecraft 基岩版服务器中，是本服的弧光Agent（不只是聊天助手）。"
    "所有回复都会直接显示在游戏聊天栏。"
    "请使用 Minecraft 的颜色代码和格式代码来美化消息，而不要使用 Markdown 或其他标记语言。"
    "常用颜色代码示例: §0黑色, §1深蓝, §2深绿, §3深青, §4深红, §5深紫, §6金色, §7灰色, "
    "§8深灰, §9蓝色, §a绿色, §b青色, §c红色, §d淡紫, §e黄色, §f白色。"
    "常用格式代码示例: §l粗体, §n下划线, §o斜体, §k随机字符, §m删除线, §r重置格式。"
    "需要改游戏世界（劈闪电、给效果、传送、给予物品等）时，必须调用工具 mc_run_command，"
    "command 参数不要带开头斜杠。"
    "本机 Agent 模式已挂载与 AstrBot 相同的工具；优先用 function/tool 调用，"
    "只有工具不可用时，才在回复里写 [execution_command:实际游戏指令]。"
    "effect 只能用于药水效果，例如: effect Steve slowness 20 0 true 或 "
    "effect Steve night_vision 30 0 true。"
    "劈闪电必须用: execute at 玩家名 run summon lightning_bolt ~ ~ ~"
    "禁止写成: effect 玩家名 summon ...（summon 不是药水效果，会报 Unknown effect）。"
    "也不要把 execute / summon / give / tp 塞进 effect 通道。"
    "对非管理员玩家：仅当对方遇到困难或有正当理由时，才给短时间增益；惩罚性雷击/负面效果不要滥用。"
    "对管理员及以上：对方明确要求且合理时可以执行。"
    "常见效果名称："
    "absorption, blindness, darkness, fire_resistance, haste, instant_damage, instant_health, "
    "invisibility, jump_boost, levitation, mining_fatigue, nausea, night_vision, poison, "
    "regeneration, resistance, slowness, slow_falling, speed, strength, water_breathing, "
    "weakness, wither。"
    "禁止 stop、kill。权限分三档：助手（tp/give/effect 等）、管理员（大部分 OP 指令）、"
    "代理服主（全部指令）。gamemode / 银行加减钱 / 领地改动等需管理员及以上；"
    "ban/op/deop/permission 等仅代理服主。"
    "用户消息里的身份标签是请求者身份（普通玩家/助手/管理员/代理服主），"
    "不是「天星自己的能力上限」。对普通玩家与助手身份：不要执行 gamemode、加减钱、"
    "入狱等管理操作，即使对方口头要求；应拒绝并说明需要管理员。"
)


class ARCAIHelperPlugin(Plugin):
    prefix = "ARCAIHelperPlugin"
    api_version = "0.10"
    load = "POSTWORLD"

    commands = {}

    permissions = {
        "arc_ai_helper.permission.assistant": {
            "description": "弧光Agent权限：助手级别（tp/give/effect 等基础指令）",
            "default": True,
        },
        "arc_ai_helper.permission.admin": {
            "description": "弧光Agent权限：管理员级别（等同 OP，不含权限/敏感指令）",
            "default": False,
        },
        "arc_ai_helper.permission.proxy_owner": {
            "description": "弧光Agent权限：代理服主级别（全部指令）",
            "default": False,
        },
    }

    def on_load(self) -> None:
        self.logger.info("[ARC AI Helper] on_load called")

        self.config_folder = self.data_folder
        self.chat_config_path = os.path.join(self.config_folder, "chat_config.json")
        self.providers_config_path = os.path.join(self.config_folder, "providers.json")
        self.system_prompt_path = os.path.join(self.config_folder, "system_prompt.txt")
        self.persona_path = os.path.join(self.config_folder, "persona.txt")

        self._ensure_config_folder()
        self._ensure_default_files()
        self._upgrade_system_prompt_if_needed()

        self.chat_config: Dict[str, Any] = self._load_chat_config()
        self.system_prompt = self._load_text_file(self.system_prompt_path, DEFAULT_SYSTEM_PROMPT)
        self.persona_prompt = self._load_text_file(self.persona_path, DEFAULT_PERSONA)
        self.scripture_path = os.path.join(self.config_folder, "scripture.txt")
        self.devotion_store_path = os.path.join(self.config_folder, "devotion_data.json")
        self.devotion_store = DevotionStore(
            self.devotion_store_path,
            self._get_devotion_config(),
        )
        self.ai_manager = ChatAIManager(self.providers_config_path)
        self.astrbot_client = AstrBotHubChatClient(self)

        self.public_history: List[Dict[str, str]] = []

        self.history_lock = threading.Lock()

        self.queue_lock = threading.Lock()
        self.current_request_owner: str = ""

        self.request_queue: queue.Queue = queue.Queue()
        self.worker_started: bool = False

        self._arc_core_newbie_guide_cache: str | None = None
        self._arc_core_landmarks_cache: str = ""
        self._server_thread_id: int | None = None
        self._read_tool_cache: Dict[str, tuple[float, str]] = {}
        self._read_tool_cache_ttl = 2.0
        self._arc_core_landmarks_cache_until: float = 0.0

    def on_enable(self) -> None:
        self.logger.info("[ARC AI Helper] on_enable called")
        self._server_thread_id = threading.get_ident()
        self.register_events(self)

        if not str(self.chat_config.get("server_name") or "").strip():
            self.logger.warning(
                "[ARC AI Helper] chat_config.json 未填写 server_name。"
                "多开服时必须互不相同（建议与 QQ Sync 一致），"
                f"当前身份：{self.get_game_server_name()}"
            )

        self._start_worker_if_needed()
        self.astrbot_client.start()

        if not self.ai_manager.has_provider():
            self.logger.info(
                "[ARC AI Helper] 未配置 providers.json。若本机弧光消息中心可用则走 AstrBot；"
                "否则需要配置本机模型后才能降级对话。"
            )

    def on_disable(self) -> None:
        try:
            self.astrbot_client.stop()
        except Exception:
            pass

    def get_game_server_name(self) -> str:
        cfg_name = str(self.chat_config.get("server_name") or "").strip()
        if cfg_name:
            return cfg_name
        server = getattr(self, "server", None)
        name = str(getattr(server, "name", "") or "").strip()
        generic = {"", "mc", "dedicated server", "minecraft server"}
        if name and name.lower() not in generic:
            return name
        try:
            port = int(getattr(server, "port", 0) or 0)
        except Exception:
            port = 0
        if port:
            return f"mc-{port}"
        return "mc"

    @staticmethod
    def _player_xuid(player) -> str:
        """Return the player's XUID, falling back to a name-based id.

        Args:
            player: Endstone player object, or None.

        Returns:
            Stable identity string for AstrBot / memory.
        """
        xuid = str(getattr(player, "xuid", "") or "").strip()
        if xuid:
            return xuid
        name = str(getattr(player, "name", "") or "player").strip() or "player"
        return f"name_{name}"

    def _resolve_online_player(self, xuid: str = "", name: str = "") -> object | None:
        """从 online_players 重取活对象；不触碰可能已销毁的旧 Player 引用。"""
        xuid_s = str(xuid or "").strip()
        if xuid_s:
            for p in self.server.online_players or []:
                if str(getattr(p, "xuid", "")) == xuid_s:
                    return p
        name_s = str(name or "").strip()
        if name_s:
            try:
                return self.server.get_player(name_s)
            except Exception:
                return None
        return None

    def _send_to_player(self, xuid: str, name: str, message: str) -> None:
        """在工作线程安全地向仍在线玩家发消息（经主线程重取 Player）。"""
        msg = str(message or "")
        if not msg:
            return

        def _do_send() -> None:
            p = self._resolve_online_player(xuid, name)
            if p is None:
                return
            p.send_message(msg)

        try:
            self._run_on_server_thread(_do_send, timeout=10)
        except Exception:
            pass

    def _run_on_server_thread(self, func, timeout: float = 10):
        """Run a callable on the Endstone server thread and wait for the result.

        Args:
            func: Zero-argument callable.
            timeout: Seconds to wait.

        Returns:
            The callable result.

        Raises:
            TimeoutError: If the server thread does not finish in time.
        """
        if self._server_thread_id is not None and threading.get_ident() == self._server_thread_id:
            return func()
        result_queue: queue.Queue = queue.Queue()

        def _task():
            try:
                result_queue.put((True, func()))
            except Exception as error:
                result_queue.put((False, error))

        self.server.scheduler.run_task(self, _task, 0, 0)
        try:
            ok, payload = result_queue.get(block=True, timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("服务器主线程执行超时") from error
        if not ok:
            raise payload
        return payload

    def _cached_server_read(self, key: str, func, ttl: float | None = None) -> str:
        now = time.time()
        wait = self._read_tool_cache_ttl if ttl is None else float(ttl)
        hit = self._read_tool_cache.get(key)
        if hit and now - hit[0] < wait:
            return hit[1]
        text = str(self._run_on_server_thread(func) or "")
        self._read_tool_cache[key] = (now, text)
        return text

    def _resolve_permission_level(
        self,
        player=None,
        payload: Dict[str, Any] | None = None,
    ) -> AIPermissionLevel:
        """Resolve AI permission from the real online caller — never trust tool-arg claims."""
        data = payload if isinstance(payload, dict) else {}
        op_maps = bool(self.chat_config.get("op_maps_to_admin", True))

        live_player = player
        if live_player is None:
            caller = (
                str(data.get("caller_player_name") or "").strip()
                or str(data.get("bound_player_name") or "").strip()
            )
            if caller:
                found, _ = find_online_player(self.server, caller)
                live_player = found

        # Have a live player → identity from OP/权限节点 only.
        # No live player → refuse elevation (default assistant); ignore forged admin in args.
        if live_player is not None:
            return resolve_permission_level(
                player=live_player,
                chat_config=self.chat_config,
                payload_level=None,
                payload_is_op=False,
                op_maps_to_admin=op_maps,
            )
        return resolve_permission_level(
            player=None,
            chat_config=self.chat_config,
            payload_level=None,
            payload_is_op=False,
            op_maps_to_admin=op_maps,
        )

    def run_ai_tool(self, action: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Execute an AstrBot MC control tool on the game server.

        Args:
            action: ``list`` / ``tps`` / ``info`` / ``cmd`` / ``jail`` / ``release`` / ``prisoners`` /
                ``skyeye_*`` / ``economy`` / ``land`` / ``arc_tp`` / ``stock_leaderboard`` / ``stock_quote``.
            args: Extra arguments, including ``command`` / ``is_op`` / ``permission_level``.

        Returns:
            JSON-serializable dict with ``ok`` and ``text`` or ``error``.
        """
        payload = args if isinstance(args, dict) else {}
        # Hub/模型可能伪造 permission_level；一律剔除，改由 _resolve_permission_level 按真人解析。
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.pop("permission_level", None)
            payload.pop("is_op", None)
        name = str(action or "").strip().lower()
        if name.startswith("mc_"):
            name = resolve_tool_action(name)
        try:
            if name == "list":
                text = self._cached_server_read("list", self._tool_list_players)
            elif name == "tps":
                text = self._cached_server_read("tps", self._tool_get_tps, ttl=1.0)
            elif name == "info":
                text = self._cached_server_read("info", self._tool_server_info)
            elif name == "cmd":
                text = self._run_on_server_thread(
                    lambda: self._tool_run_command(
                        str(payload.get("command") or ""),
                        payload,
                    )
                )
            elif name in ("economy", "money", "bank"):
                text = self._run_on_server_thread(lambda: self._tool_arc_economy(payload))
            elif name in ("player_basic_info", "resolve_player", "lookup_player"):
                result = self._run_on_server_thread(
                    lambda: self._tool_player_basic_info(payload)
                )
                if isinstance(result, dict):
                    return result
                return {"ok": False, "error": str(result or "获取玩家信息失败")}
            elif name in ("land", "lands"):
                text = self._run_on_server_thread(lambda: self._tool_arc_land(payload))
            elif name in ("landmarks", "landmark", "warps"):
                text = self._run_on_server_thread(self._tool_arc_landmarks)
            elif name in ("arc_tp", "arc_teleport", "core_tp"):
                text = self._run_on_server_thread(lambda: self._tool_arc_teleport(payload))
            elif name == "jail":
                text = self._run_on_server_thread(lambda: self._tool_jail_player(payload))
            elif name == "release":
                text = self._run_on_server_thread(lambda: self._tool_release_player(payload))
            elif name in ("prisoners", "list_prisoners"):
                text = self._run_on_server_thread(self._tool_list_prisoners)
            elif name in ("skyeye_player", "sky_eye_player"):
                text = self._run_on_server_thread(lambda: self._tool_skyeye_player(payload))
            elif name in ("skyeye_combat", "sky_eye_combat"):
                text = self._run_on_server_thread(lambda: self._tool_skyeye_combat(payload))
            elif name in ("skyeye_events", "sky_eye_events", "skyeye_event", "sky_eye_event"):
                text = self._run_on_server_thread(lambda: self._tool_skyeye_events(payload))
            elif name in ("skyeye_location", "sky_eye_location"):
                text = self._run_on_server_thread(lambda: self._tool_skyeye_location(payload))
            elif name in ("stock_leaderboard", "stock_rank", "stock_leader"):
                # yfinance / sqlite 可在工作线程执行，避免阻塞主线程
                text = self._tool_stock_leaderboard(payload)
            elif name in ("stock_quote", "stock_price", "stock"):
                text = self._tool_stock_quote(payload)
            elif name in ("player_ip", "player_locale", "player_geo", "geo_locale", "locale", "geo"):
                text = self._run_on_server_thread(lambda: self._tool_player_ip(payload))
            elif name in ("devotion_status", "devotion"):
                text = self._run_on_server_thread(lambda: self._tool_devotion_status(payload))
            elif name in ("devotion_adjust", "devotion_change"):
                text = self._run_on_server_thread(lambda: self._tool_devotion_adjust(payload))
            elif name in ("player_inventory", "inventory"):
                text = self._run_on_server_thread(lambda: self._tool_player_inventory(payload))
            elif name in ("accept_offering", "offering", "sacrifice"):
                text = self._run_on_server_thread(lambda: self._tool_accept_offering(payload))
            elif name in ("grant_blessing", "blessing", "bless"):
                text = self._run_on_server_thread(lambda: self._tool_grant_blessing(payload))
            elif name in ("divine_intervention", "divine", "miracle"):
                text = self._run_on_server_thread(lambda: self._tool_divine_intervention(payload))
            else:
                return {"ok": False, "error": f"未知工具动作: {action}"}
            result = {"ok": True, "text": str(text or "").strip() or "（无返回）"}
            self._sky_eye_log_agent_tool(name, payload, result)
            return result
        except Exception as error:
            self.logger.warning(f"[ARC AI Helper] AI 工具 {action} 失败: {error}")
            fail = {"ok": False, "error": str(error)}
            self._sky_eye_log_agent_tool(name, payload, fail)
            return fail

    def _sky_eye_log_agent_tool(
        self,
        action: str,
        payload: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        """Write Agent tool / command activity into ARCCore sky eye."""
        name = str(action or "").strip().lower()
        if name not in _SKY_EYE_AUDIT_ACTIONS:
            return
        if name in ("economy", "money", "bank"):
            sub = str(
                payload.get("sub_action") or payload.get("operation") or "query"
            ).strip().lower()
            if sub in ("query", "get", "balance", ""):
                return
        if name in ("land", "lands"):
            sub = str(
                payload.get("sub_action") or payload.get("operation") or "list"
            ).strip().lower()
            if sub in ("list", "info", "at", "query", ""):
                return

        level = self._resolve_permission_level(payload=payload)
        caller = (
            str(payload.get("caller_player_name") or "").strip()
            or str(payload.get("bound_player_name") or "").strip()
            or str(payload.get("player_name") or "").strip()
        )
        bits: List[str] = [
            f"tool={name}",
            f"level={level_display(level)}",
        ]
        if caller:
            bits.append(f"caller={caller}")
        for key in (
            "sub_action",
            "player_name",
            "targets",
            "amount",
            "delta",
            "minutes",
            "reason",
            "home_name",
            "warp_name",
            "x",
            "y",
            "z",
        ):
            val = str(payload.get(key) or "").strip()
            if val:
                bits.append(f"{key}={val}")
        if result.get("ok"):
            bits.append("status=ok")
        else:
            bits.append("status=fail")
            err = str(result.get("error") or "").strip()
            if err:
                bits.append(f"error={err[:120]}")
        detail = "; ".join(bits)
        self._sky_eye_log_agent(
            detail=detail,
            requester_name=caller,
            action="AiAgent",
        )

    def _sky_eye_log_agent(
        self,
        *,
        detail: str,
        requester_name: str = "",
        target_name: str = "",
        action: str = "AiAgent",
    ) -> None:
        """Write Agent activity. Commands use AgentCommand under the requester's name."""
        core = self._get_arc_core_plugin()
        if core is None:
            return
        logger_api = getattr(core, "api_sky_eye_log", None)
        if not callable(logger_api):
            return
        agent_name = str(self.chat_config.get("assistant_name") or "弧光天星").strip()
        agent_name = agent_name or "弧光天星"
        requester = str(requester_name or target_name or "").strip()
        act = str(action or "AiAgent").strip() or "AiAgent"
        # Attribute command rows to the requesting player so player sky-eye queries find them.
        if act == "AgentCommand" and requester:
            subject_name = requester
            subject_xuid = ""
            try:
                player = self.server.get_player(requester)
                if player is not None:
                    subject_xuid = str(getattr(player, "xuid", "") or "").strip()
            except Exception:
                pass
            final_detail = f"by={agent_name}; {str(detail or '')[:450]}"
            final_target = agent_name
        else:
            subject_name = agent_name
            subject_xuid = ""
            final_detail = str(detail or "")[:500]
            final_target = requester
        try:
            logger_api(
                act,
                player_name=subject_name,
                player_xuid=subject_xuid,
                detail=final_detail,
                target_name=final_target,
                target_type="player" if final_target else "",
                resolve_online=bool(subject_xuid) or (act != "AgentCommand"),
            )
        except TypeError:
            # Older arc_core without resolve_online.
            try:
                logger_api(
                    act,
                    player_name=subject_name,
                    player_xuid=subject_xuid,
                    detail=final_detail,
                    target_name=final_target,
                    target_type="player" if final_target else "",
                )
            except Exception as error:
                self.logger.debug(f"[ARC AI Helper] 天眼留档失败: {error}")
        except Exception as error:
            self.logger.debug(f"[ARC AI Helper] 天眼留档失败: {error}")

    def _sky_eye_log_agent_command(
        self,
        *,
        command: str,
        level: AIPermissionLevel,
        status: str,
        requester_name: str = "",
        error: str = "",
        via: str = "cmd",
    ) -> None:
        """Record a console command executed (or denied) by 天星."""
        cmd = str(command or "").strip().lstrip("/")
        bits = [
            f"via={via}",
            f"level={level_display(level)}",
            f"cmd=/{cmd}" if cmd else "cmd=",
            f"status={status}",
        ]
        if error:
            bits.append(f"error={str(error)[:120]}")
        self._sky_eye_log_agent(
            detail="; ".join(bits),
            requester_name=requester_name,
            action="AgentCommand",
        )

    def _tool_list_players(self) -> str:
        online_players = list(self.server.online_players or [])
        max_players = getattr(self.server, "max_players", "?")
        if not online_players:
            return f"当前没有玩家在线（容量 {max_players}）"
        lines = [f"在线玩家 ({len(online_players)}/{max_players}):"]
        for player in online_players:
            try:
                ping = player.ping
                ping_display = f"{ping}ms"
            except Exception:
                ping_display = "N/A"
            xuid = str(getattr(player, "xuid", "") or "").strip() or "未知"
            lines.append(f"• {player.name}  XUID={xuid}  [{ping_display}]")
        return "\n".join(lines)

    def _tool_get_tps(self) -> str:
        current_tps = float(self.server.current_tps)
        average_tps = float(self.server.average_tps)
        current_mspt = float(self.server.current_mspt)
        average_mspt = float(self.server.average_mspt)
        current_tick_usage = float(self.server.current_tick_usage)
        average_tick_usage = float(self.server.average_tick_usage)
        if current_tps >= 19.0:
            status = "良好"
        elif current_tps >= 15.0:
            status = "轻微延迟"
        else:
            status = "严重延迟"
        return (
            "服务器性能状态:\n"
            f"• 当前TPS: {current_tps:.2f}/20.0  {status}\n"
            f"• 平均TPS: {average_tps:.2f}/20.0\n"
            f"• 当前MSPT: {current_mspt:.2f}ms\n"
            f"• 平均MSPT: {average_mspt:.2f}ms\n"
            f"• 当前Tick使用率: {current_tick_usage:.1f}%\n"
            f"• 平均Tick使用率: {average_tick_usage:.1f}%"
        )

    def _tool_server_info(self) -> str:
        online_count = len(list(self.server.online_players or []))
        max_players = getattr(self.server, "max_players", "?")
        server_name = self.get_game_server_name()
        version = getattr(self.server, "version", "未知")
        minecraft_version = getattr(self.server, "minecraft_version", "未知")
        start_time = getattr(self.server, "start_time", None)
        now = datetime.now()
        uptime_str = "未知"
        start_str = "未知"
        if start_time is not None:
            try:
                if isinstance(start_time, datetime):
                    started = start_time
                elif isinstance(start_time, (int, float)):
                    started = datetime.fromtimestamp(float(start_time))
                else:
                    started = None
                if started is not None:
                    start_str = started.strftime("%Y-%m-%d %H:%M:%S")
                    delta = now - started
                    seconds = max(0, int(delta.total_seconds()))
                    hours, rem = divmod(seconds, 3600)
                    minutes, secs = divmod(rem, 60)
                    uptime_str = f"{hours}小时{minutes}分{secs}秒"
            except Exception:
                pass
        return (
            "服务器信息:\n"
            f"• 服务器名称: {server_name}\n"
            f"• Endstone版本: {version}\n"
            f"• Minecraft版本: {minecraft_version}\n"
            f"• 启动时间: {start_str}\n"
            f"• 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"• 运行时长: {uptime_str}\n"
            f"• 在线玩家: {online_count}/{max_players}"
        )

    def _tool_run_command(
        self,
        command_to_execute: str,
        payload: Dict[str, Any] | None = None,
    ) -> str:
        data = payload if isinstance(payload, dict) else {}
        command_to_execute = html.unescape(str(command_to_execute or "").strip())
        if not command_to_execute:
            return "命令为空"

        normalized = command_to_execute.lstrip("/").strip()
        if not normalized:
            return "命令为空"

        level = self._resolve_permission_level(payload=data)
        caller = (
            str(data.get("caller_player_name") or "").strip()
            or str(data.get("bound_player_name") or "").strip()
            or str(data.get("player_name") or "").strip()
        )
        ok, reason = validate_command_for_level(
            normalized,
            level,
            bound_player_name=str(data.get("bound_player_name") or ""),
            is_bound_self_help=bool(data.get("is_bound_self_help", False)),
        )
        if not ok:
            deny = reason or "没有权限：该指令不被允许"
            self._sky_eye_log_agent_command(
                command=normalized,
                level=level,
                status="denied",
                requester_name=caller,
                error=deny,
                via="cmd",
            )
            return deny

        blocked, deny = should_block_devotion_bypass(
            normalized,
            level,
            devotion_enabled=self._is_devotion_enabled(),
        )
        if blocked:
            self._sky_eye_log_agent_command(
                command=normalized,
                level=level,
                status="denied",
                requester_name=caller,
                error=deny,
                via="cmd",
            )
            return deny

        msg_ret: List[str] = []
        error_ret: List[str] = []
        language = getattr(self.server, "language", None)

        def on_message(msg):
            if isinstance(msg, str):
                msg_ret.append(msg)
                return
            if language is not None:
                try:
                    msg_ret.append(language.translate(msg, language.locale))
                    return
                except Exception:
                    pass
            msg_ret.append(str(msg))

        def on_error(err):
            if isinstance(err, str):
                error_ret.append(err)
                return
            if language is not None:
                try:
                    error_ret.append(language.translate(err))
                    return
                except Exception:
                    pass
            error_ret.append(str(err))

        sender = self.server.command_sender
        if CommandSenderWrapper is not None:
            sender = CommandSenderWrapper(
                sender=self.server.command_sender,
                on_message=on_message,
                on_error=on_error,
            )
        success = bool(self.server.dispatch_command(sender, normalized))
        lines = list(msg_ret)
        lines.extend([f"[ERROR] {item}" for item in error_ret])
        output_text = "\n".join(lines) if lines else "无返回值"
        status = "成功" if success else "失败, 请检查命令语法或权限"
        self._sky_eye_log_agent_command(
            command=normalized,
            level=level,
            status="ok" if success else "fail",
            requester_name=caller,
            via="cmd",
        )
        return f"命令已执行: /{normalized}\n状态: {status}\n输出:\n{output_text}"

    def _get_prison_plugin(self):
        plugin_manager = getattr(self.server, "plugin_manager", None)
        if plugin_manager is None:
            return None
        try:
            return plugin_manager.get_plugin("arc_prison")
        except Exception:
            return None

    def _get_stock_plugin(self):
        plugin_manager = getattr(self.server, "plugin_manager", None)
        if plugin_manager is None:
            return None
        for name in ("up_and_down", "up-and-down"):
            try:
                plug = plugin_manager.get_plugin(name)
            except Exception:
                plug = None
            if plug is not None:
                return plug
        return None

    def _tool_stock_leaderboard(self, payload: Dict[str, Any]) -> str:
        stock = self._get_stock_plugin()
        if stock is None:
            return "本服未安装股票插件 up_and_down"
        api = getattr(stock, "api_get_leaderboard_text", None)
        if not callable(api):
            return "股票插件版本过旧，没有排行榜查询接口（需 ≥ 0.5.2）"
        return str(
            api(
                mode=str(payload.get("mode") or "relative"),
                top=payload.get("top", 5),
                bottom=payload.get("bottom", 5),
                player_name=str(payload.get("player_name") or "").strip(),
            )
            or "（无排行榜数据）"
        )

    def _tool_stock_quote(self, payload: Dict[str, Any]) -> str:
        stock = self._get_stock_plugin()
        if stock is None:
            return "本服未安装股票插件 up_and_down"
        api = getattr(stock, "api_get_stock_quote_text", None)
        if not callable(api):
            return "股票插件版本过旧，没有行情查询接口（需 ≥ 0.5.2）"
        symbol = str(
            payload.get("symbol")
            or payload.get("stock")
            or payload.get("ticker")
            or ""
        ).strip()
        if not symbol:
            return "股票代码为空"
        period = str(payload.get("period") or payload.get("range") or "day").strip()
        return str(api(symbol, period) or "（无行情数据）")

    def _tool_player_ip(self, payload: Dict[str, Any]) -> str:
        """Return an online player's connection IP for downstream tools."""
        from .geo_locale import extract_player_ip

        data = payload if isinstance(payload, dict) else {}
        level = self._resolve_permission_level(payload=data)
        caller = (
            str(data.get("caller_player_name") or "").strip()
            or str(data.get("bound_player_name") or "").strip()
        )
        target_name = str(
            data.get("target_player_name")
            or data.get("player_name")
            or caller
            or ""
        ).strip()
        if not target_name:
            return "请指定要查询的在线玩家名"
        # 他人 IP 敏感：非管理员只能查自己
        if (
            caller
            and target_name.lower() != caller.lower()
            and level < AIPermissionLevel.ADMIN
        ):
            return "没有权限：助手级别只能查询自己的连接 IP；查他人需管理员。"

        player = self.server.get_player(target_name)
        if player is None:
            lowered = target_name.lower()
            for online in list(self.server.online_players or []):
                if str(getattr(online, "name", "") or "").lower() == lowered:
                    player = online
                    break
        if player is None:
            return f"玩家 {target_name} 不在本服在线，无法读取连接 IP"
        real_name = str(getattr(player, "name", "") or target_name).strip()
        ip = extract_player_ip(player)
        if not ip:
            return f"玩家 {real_name} 无法读取连接地址"
        # 结构化输出，方便模型把 ip 原样传给其它工具
        return f"player_name={real_name}\nip={ip}"

    def _get_devotion_config(self) -> Dict[str, Any]:
        raw = self.chat_config.get("devotion") if hasattr(self, "chat_config") else None
        return merge_devotion_config(raw if isinstance(raw, dict) else None)

    def _is_devotion_enabled(self) -> bool:
        return bool(self._get_devotion_config().get("enabled"))

    def _devotion_admin_bypass(self, level: AIPermissionLevel) -> bool:
        if level < AIPermissionLevel.ADMIN:
            return False
        return bool(self._get_devotion_config().get("admin_bypass_favor", True))

    def _resolve_devotion_target(
        self,
        payload: Dict[str, Any],
        *,
        require_online: bool = True,
    ):
        data = payload if isinstance(payload, dict) else {}
        caller = (
            str(data.get("caller_player_name") or "").strip()
            or str(data.get("bound_player_name") or "").strip()
        )
        target_name = str(data.get("player_name") or data.get("target_player_name") or caller or "").strip()
        if not target_name:
            return None, "", "请指定玩家"
        level = self._resolve_permission_level(payload=data)
        if (
            caller
            and target_name.lower() != caller.lower()
            and level < AIPermissionLevel.ADMIN
        ):
            return None, target_name, "没有权限：只能查询或操作自己的信仰记录"
        player, real_name = find_online_player(self.server, target_name)
        if require_online and player is None:
            return None, target_name, f"玩家 {target_name} 不在本服在线"
        if player is not None:
            real_name = str(getattr(player, "name", "") or real_name).strip()
        return player, real_name, ""

    def _tool_devotion_status(self, payload: Dict[str, Any]) -> str:
        if not self._is_devotion_enabled():
            return "本服未启用信仰/好感度系统"
        player, real_name, error = self._resolve_devotion_target(payload, require_online=False)
        xuid = self._player_xuid(player) if player is not None else str(payload.get("caller_xuid") or "")
        if not real_name:
            real_name = str(payload.get("caller_player_name") or "").strip()
        if error and not real_name:
            return error
        return self.devotion_store.format_status(xuid=xuid, name=real_name)

    def _tool_devotion_adjust(self, payload: Dict[str, Any]) -> str:
        if not self._is_devotion_enabled():
            return "本服未启用信仰/好感度系统"
        player, real_name, error = self._resolve_devotion_target(payload, require_online=False)
        if error:
            return error
        short_delta = payload.get("short_delta")
        long_delta = payload.get("long_delta")
        if short_delta in (None, "") and long_delta in (None, ""):
            if payload.get("delta") in (None, ""):
                return "请指定 short_delta / long_delta"
            short_delta = payload.get("delta")
            long_delta = 0
        try:
            short_change = int(short_delta or 0)
            long_change = int(long_delta or 0)
        except Exception:
            return "short_delta / long_delta 必须是整数"
        reason = str(payload.get("reason") or "").strip()
        kind = str(payload.get("kind") or "adjust").strip().lower()
        xuid = self._player_xuid(player) if player is not None else str(payload.get("caller_xuid") or "")
        ok, message, _state = self.devotion_store.adjust_faith(
            xuid=xuid,
            name=real_name,
            short_delta=short_change,
            long_delta=long_change,
            reason=reason,
            kind=kind,
        )
        return message

    def _tool_player_inventory(self, payload: Dict[str, Any]) -> str:
        player, real_name, error = self._resolve_devotion_target(payload, require_online=True)
        if error:
            return error
        assert player is not None
        offering_item = str(payload.get("offering_item_id") or payload.get("item_id") or "").strip()
        try:
            offering_amount = int(payload.get("offering_amount") or payload.get("amount") or 0)
        except Exception:
            offering_amount = 0
        report = format_inventory_report(
            player,
            offering_item_id=offering_item,
            offering_amount=offering_amount,
        )
        return f"{real_name} 的背包与献祭评估：\n{report}"

    def _tool_accept_offering(self, payload: Dict[str, Any]) -> str:
        if not self._is_devotion_enabled():
            return "本服未启用信仰/好感度系统"
        player, real_name, error = self._resolve_devotion_target(payload, require_online=True)
        if error:
            return error
        assert player is not None
        item_id = normalize_item_id(str(payload.get("item_id") or ""))
        if not item_id:
            return "请指定献祭物品 item_id"
        try:
            amount = max(1, int(payload.get("amount", 1) or 1))
        except Exception:
            amount = 1
        have = count_item(player, item_id)
        if have < amount:
            return f"背包中没有足够的 {item_display_name(item_id)}（需要 {amount}，仅有 {have}）"

        assessment = assess_offering_sincerity(player, item_id, amount)
        allow_stingy = str(payload.get("allow_stingy") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if assessment.get("is_stingy") and not allow_stingy:
            hint = (assessment.get("cold_hints") or ["诚不足，神不纳"])[0]
            reasons = "；".join(assessment.get("reasons") or [])
            return (
                f"【拒收·吝啬】献祭未执行，物品未扣除。{reasons}。"
                f"对此类无诚意供奉应冷淡处理，勿给信仰增益；可对玩家曰「{hint}」。"
                "若你仍要收下，须先确认其诚意并设 allow_stingy=true。"
            )

        removed = remove_items_from_player(player, item_id, amount)
        if removed < amount:
            return (
                f"献祭未成立：无法从背包扣除 {item_display_name(item_id)} x{amount}，"
                "未收祭、不改信仰。可对玩家曰「祭品未能送达神前」；勿用 clear 强扣。"
            )
        xuid = self._player_xuid(player)
        record = self.devotion_store.get_record(xuid=xuid, name=real_name)
        long_term = int(record.get("long_term", 1) or 1)
        short_term = int(record.get("short_term", 0) or 0)

        short_gain = payload.get("short_gain")
        long_gain = payload.get("long_gain")
        if short_gain not in (None, "") or long_gain not in (None, ""):
            try:
                to_short = int(short_gain or 0)
                to_long = int(long_gain or 0)
            except Exception:
                return "short_gain / long_gain 必须是整数"
        else:
            try:
                total = int(payload.get("total_gain", 0) or 0)
            except Exception:
                total = 0
            if total <= 0:
                total = removed * 3
            to_short, to_long = self.devotion_store.auto_split_gain(
                total,
                long_term,
                short_term,
                long_cap=self.devotion_store.long_growth_cap(),
            )

        ok, message, _state = self.devotion_store.adjust_faith(
            xuid=xuid,
            name=real_name,
            short_delta=to_short,
            long_delta=to_long,
            reason=f"献祭 {item_display_name(item_id)} x{removed}",
            kind="offering",
        )
        if not ok:
            return message
        return (
            f"【内部】已收下献祭 {item_display_name(item_id)} x{removed}。{message} "
            "向玩家以神谕致谢，勿报具体信仰点数。"
        )

    def _consume_short_favor_or_bypass(
        self,
        *,
        payload: Dict[str, Any],
        level: AIPermissionLevel,
        xuid: str,
        real_name: str,
        favor_cost: int,
        reason: str,
    ) -> tuple[bool, str]:
        if self._devotion_admin_bypass(level):
            return True, "管理员神术，不消耗近期好感"
        ok, message, _state = self.devotion_store.consume_short_favor(
            xuid=xuid,
            name=real_name,
            cost=favor_cost,
            reason=reason,
        )
        return ok, message

    def _refund_short_favor(self, *, xuid: str, real_name: str, amount: int, reason: str) -> None:
        if amount <= 0:
            return
        self.devotion_store.adjust_faith(
            xuid=xuid,
            name=real_name,
            short_delta=amount,
            long_delta=0,
            reason=reason,
            kind="adjust",
        )

    def _tool_divine_intervention(self, payload: Dict[str, Any]) -> str:
        if not self._is_devotion_enabled():
            return "本服未启用信仰/好感度系统"
        player, real_name, error = self._resolve_devotion_target(payload, require_online=True)
        if error:
            return error
        assert player is not None
        level = self._resolve_permission_level(payload=payload)
        try:
            favor_cost = int(payload.get("favor_cost", 0) or 0)
        except Exception:
            return "favor_cost 必须是整数"
        if favor_cost <= 0 and not self._devotion_admin_bypass(level):
            return "请指定 favor_cost（近期好感消耗，由你根据神术规模判定）"

        blessing = str(payload.get("blessing") or "").strip().lower()
        command = str(payload.get("command") or "").strip().lstrip("/")
        item_id = normalize_item_id(str(payload.get("item_id") or ""))
        try:
            item_amount = max(1, int(payload.get("item_amount", 1) or 1))
        except Exception:
            item_amount = 1
        try:
            duration_seconds = max(5, int(payload.get("duration_seconds", 120) or 120))
        except Exception:
            duration_seconds = 120
        try:
            amplifier = max(0, int(payload.get("amplifier", 0) or 0))
        except Exception:
            amplifier = 0

        # 效果等级硬上限对所有人生效（含管理员），避免草率塞满级 buff。
        if blessing:
            amplifier, duration_seconds, cap_msg = clamp_player_blessing(
                amplifier=amplifier,
                duration_seconds=duration_seconds,
            )
            if cap_msg:
                return cap_msg

        if not self._devotion_admin_bypass(level):
            ok_cost, cost_msg, _minimum = validate_divine_favor_cost(
                favor_cost=favor_cost,
                blessing=blessing,
                amplifier=amplifier,
                duration_seconds=duration_seconds,
                item_id=item_id,
                item_amount=item_amount,
                command=command,
            )
            if not ok_cost:
                return cost_msg

        actions = sum(
            [
                1 if blessing else 0,
                1 if command else 0,
                1 if item_id else 0,
            ]
        )
        if actions != 1:
            return "请指定且仅指定一种神术：blessing / command / item_id"

        xuid = self._player_xuid(player)
        reason = str(payload.get("reason") or blessing or command or item_id).strip()
        paid = 0
        if favor_cost > 0:
            ok, pay_msg = self._consume_short_favor_or_bypass(
                payload=payload,
                level=level,
                xuid=xuid,
                real_name=real_name,
                favor_cost=favor_cost,
                reason=f"神术: {reason}",
            )
            if not ok:
                return pay_msg
            paid = favor_cost if not self._devotion_admin_bypass(level) else 0
            consume_note = pay_msg
        else:
            consume_note = "管理员神术，不消耗近期好感"

        try:
            if blessing:
                effect_name = self.devotion_store.resolve_blessing_effect(blessing)
                command = f"effect {real_name} {effect_name} {duration_seconds} {amplifier} true"
                detail = f"效果 {effect_name} {duration_seconds}s L{amplifier + 1}"
            elif item_id:
                if is_forbidden_grant_item(item_id):
                    if paid:
                        self._refund_short_favor(
                            xuid=xuid,
                            real_name=real_name,
                            amount=paid,
                            reason="禁止物品退回",
                        )
                    return f"禁止赐予超模物品（如 {forbidden_items_hint()} 等）"
                command = f"give {real_name} {item_display_name(item_id)} {item_amount}"
                detail = f"物品 {item_display_name(item_id)} x{item_amount}"
            else:
                detail = f"指令 {command}"

            ok_cmd, deny = validate_command_for_level(command, level)
            if not ok_cmd:
                if paid:
                    self._refund_short_favor(
                        xuid=xuid,
                        real_name=real_name,
                        amount=paid,
                        reason="神术失败退回",
                    )
                return deny or "神术指令被拦截"

            self.server.dispatch_command(self.server.command_sender, command)
        except Exception as error:
            if paid:
                self._refund_short_favor(
                    xuid=xuid,
                    real_name=real_name,
                    amount=paid,
                    reason="神术失败退回",
                )
            return f"神术失败: {error}"

        if paid:
            return (
                f"【内部】已对 {real_name} 施行神术（{detail}），扣近期 {paid}。"
                "向玩家以神谕宣告，勿提数字与工具名。"
            )
        return f"【内部】已对 {real_name} 施行神术（{detail}）。{consume_note} 向玩家以神谕宣告。"

    def _tool_grant_blessing(self, payload: Dict[str, Any]) -> str:
        data = dict(payload or {})
        if data.get("favor_cost") in (None, ""):
            return "请指定 favor_cost（近期好感消耗）"
        if not str(data.get("blessing") or "").strip():
            return "请指定 blessing"
        return self._tool_divine_intervention(data)

    def _tool_require_admin(self, payload: Dict[str, Any]) -> str:
        level = self._resolve_permission_level(payload=payload)
        return require_admin_level(level)

    def _tool_jail_player(self, payload: Dict[str, Any]) -> str:
        denied = self._tool_require_admin(payload)
        if denied:
            return denied
        prison = self._get_prison_plugin()
        if prison is None:
            return "本服未安装监狱插件 arc_prison"
        api_quick_jail = getattr(prison, "api_quick_jail", None)
        if not callable(api_quick_jail):
            return "监狱插件版本过旧，没有一键入狱接口"
        player_name = str(payload.get("player_name") or "").strip()
        if not player_name:
            return "玩家名为空"
        duration_raw = str(payload.get("minutes") or payload.get("duration") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        result = api_quick_jail(
            player_name,
            duration_text=duration_raw,
            reason=reason,
            jailed_by=assistant_name,
            announce=True,
        )
        if not isinstance(result, dict):
            return "监狱插件返回格式异常"
        if result.get("ok"):
            return str(result.get("text") or "关押成功")
        return str(result.get("error") or "关押失败")

    def _tool_release_player(self, payload: Dict[str, Any]) -> str:
        denied = self._tool_require_admin(payload)
        if denied:
            return denied
        prison = self._get_prison_plugin()
        if prison is None:
            return "本服未安装监狱插件 arc_prison"
        api_quick_release = getattr(prison, "api_quick_release", None)
        if not callable(api_quick_release):
            return "监狱插件版本过旧，没有一键释放接口"
        player_name = str(payload.get("player_name") or "").strip()
        if not player_name:
            return "玩家名为空"
        result = api_quick_release(player_name, announce=True)
        if not isinstance(result, dict):
            return "监狱插件返回格式异常"
        if result.get("ok"):
            return str(result.get("text") or "释放成功")
        return str(result.get("error") or "释放失败")

    def _tool_list_prisoners(self) -> str:
        prison = self._get_prison_plugin()
        if prison is None:
            return "本服未安装监狱插件 arc_prison"
        formatter = getattr(prison, "api_format_prisoners_text", None)
        if callable(formatter):
            return str(formatter() or "当前没有在押玩家")
        listing = getattr(prison, "api_get_all_imprisoned_players", None)
        if not callable(listing):
            return "监狱插件没有在押查询接口"
        prisoners = listing() or []
        if not prisoners:
            return "当前没有玩家在监狱中"
        lines = [f"当前在押 {len(prisoners)} 人:"]
        for item in prisoners:
            name = item.get("player_name") or "?"
            reason = item.get("reason") or "未指定原因"
            remain = "无期徒刑" if item.get("is_life_sentence") else f"{item.get('remaining_minutes', 0)} 分钟"
            online = "在线" if item.get("is_online") else "离线"
            lines.append(f"• {name}  剩余:{remain}  原因:{reason}  [{online}]")
        return "\n".join(lines)

    def _get_arc_core_plugin(self):
        plugin_manager = getattr(self.server, "plugin_manager", None)
        if plugin_manager is None:
            return None
        try:
            return plugin_manager.get_plugin("arc_core")
        except Exception:
            return None

    def _tool_skyeye_require_admin(self, payload: Dict[str, Any]) -> str:
        return self._tool_require_admin(payload)

    def _parse_skyeye_minutes(self, payload: Dict[str, Any], default: int = 30) -> int:
        raw = str(payload.get("minutes") or "").strip().lower().replace(" ", "")
        if not raw:
            return default
        named = {
            "一天": 1440,
            "1天": 1440,
            "24小时": 1440,
            "24h": 1440,
            "半天": 720,
            "一小时": 60,
            "1小时": 60,
            "1h": 60,
            "一周": 10080,
            "7天": 10080,
        }
        if raw in named:
            return max(1, min(named[raw], 24 * 60 * 7))
        for suffix, multiplier in (("天", 1440), ("日", 1440), ("小时", 60), ("时", 60), ("h", 60)):
            if raw.endswith(suffix) and raw[: -len(suffix)].isdigit():
                return max(1, min(int(raw[: -len(suffix)]) * multiplier, 24 * 60 * 7))
        for suffix in ("分钟", "分", "min", "m"):
            if raw.endswith(suffix) and raw[: -len(suffix)].isdigit():
                return max(1, min(int(raw[: -len(suffix)]), 24 * 60 * 7))
        try:
            return max(1, min(int(raw), 24 * 60 * 7))
        except (TypeError, ValueError):
            return default

    def _tool_skyeye_player(self, payload: Dict[str, Any]) -> str:
        denied = self._tool_skyeye_require_admin(payload)
        if denied:
            return denied
        core = self._get_arc_core_plugin()
        if core is None:
            return "本服未安装弧光核心 arc_core"
        query_text = getattr(core, "api_sky_eye_query_text", None)
        player_now = getattr(core, "api_sky_eye_player_now", None)
        player_name = str(payload.get("player_name") or "").strip()
        if not player_name:
            return "查指定玩家行为时需要玩家名；若要查全服某类事件请用 mc_skyeye_events（如 action=death）"
        minutes = self._parse_skyeye_minutes(payload, 30)
        action = str(payload.get("action") or "").strip()
        parts: List[str] = []
        if callable(player_now):
            now_info = player_now(player_name=player_name)
            if isinstance(now_info, dict) and now_info.get("source"):
                name = now_info.get("player_name") or player_name
                matches = now_info.get("name_matches") or []
                if now_info.get("source") == "ambiguous_online":
                    parts.append(
                        f"在线匹配到多人，请说清楚是哪一个：{'、'.join(str(m) for m in matches)}"
                    )
                elif now_info.get("source") == "name_suggestions":
                    parts.append(
                        f"未精确命中「{player_name}」，天眼近期相似名：{'、'.join(str(m) for m in matches)}"
                    )
                elif now_info.get("online") and now_info.get("x") is not None:
                    pos = f"{float(now_info.get('x')):.1f},{float(now_info.get('y')):.1f},{float(now_info.get('z')):.1f}"
                    land = "荒野"
                    if now_info.get("in_land"):
                        land = f"领地内 {now_info.get('land_name') or ''}（主人 {now_info.get('land_owner') or '?'}）"
                    parts.append(f"{name} 当前在线  {now_info.get('dimension')} ({pos})  {land}")
                elif now_info.get("source") == "sky_eye":
                    parts.append(
                        f"{name} 当前不在线，最近一次天眼: {now_info.get('last_ts')} "
                        f"{now_info.get('last_action')} {now_info.get('dimension')} "
                        f"({now_info.get('x')},{now_info.get('y')},{now_info.get('z')})"
                    )
                else:
                    parts.append(f"{name} 当前不在线，天眼里也没有记录。")
        if callable(query_text):
            parts.append(
                query_text(
                    player_name=player_name,
                    action=action,
                    minutes=minutes,
                    name_fuzzy=True,
                    heading=f"{player_name} 近 {minutes} 分钟行为（模糊名）",
                )
            )
        else:
            parts.append("弧光核心版本过旧，没有天眼查询接口")
        return "\n".join(parts)

    def _tool_skyeye_combat(self, payload: Dict[str, Any]) -> str:
        denied = self._tool_skyeye_require_admin(payload)
        if denied:
            return denied
        core = self._get_arc_core_plugin()
        if core is None:
            return "本服未安装弧光核心 arc_core"
        query_text = getattr(core, "api_sky_eye_query_text", None)
        if not callable(query_text):
            return "弧光核心版本过旧，没有天眼查询接口"
        player_name = str(payload.get("player_name") or "").strip()
        minutes = self._parse_skyeye_minutes(payload, 30)
        event_kind = str(
            payload.get("event_kind") or payload.get("action") or "combat"
        ).strip() or "combat"
        who = player_name if player_name else "全服"
        return query_text(
            player_name=player_name,
            minutes=minutes,
            combat_role="both" if player_name else "",
            action=event_kind,
            name_fuzzy=True,
            heading=f"{who} 近 {minutes} 分钟战斗（{event_kind}）",
        )

    def _tool_skyeye_events(self, payload: Dict[str, Any]) -> str:
        denied = self._tool_skyeye_require_admin(payload)
        if denied:
            return denied
        core = self._get_arc_core_plugin()
        if core is None:
            return "本服未安装弧光核心 arc_core"
        query_text = getattr(core, "api_sky_eye_query_text", None)
        if not callable(query_text):
            return "弧光核心版本过旧，没有天眼查询接口（需 ≥0.9.28）"
        action = str(payload.get("action") or payload.get("event_kind") or "").strip()
        if not action:
            return (
                "请传入 action（事件类型）。例如 death=死亡、pvp=玩家互殴、"
                "pve=打怪、combat=战斗、join/quit/break/place/chat 等"
            )
        player_name = str(payload.get("player_name") or "").strip()
        minutes = self._parse_skyeye_minutes(payload, 30)
        who = player_name if player_name else "全服"
        return query_text(
            player_name=player_name,
            action=action,
            minutes=minutes,
            name_fuzzy=True,
            limit=80,
            heading=f"{who} 近 {minutes} 分钟事件（{action}）",
        )

    def _tool_skyeye_location(self, payload: Dict[str, Any]) -> str:
        denied = self._tool_skyeye_require_admin(payload)
        if denied:
            return denied
        core = self._get_arc_core_plugin()
        if core is None:
            return "本服未安装弧光核心 arc_core"
        query_text = getattr(core, "api_sky_eye_query_text", None)
        if not callable(query_text):
            return "弧光核心版本过旧，没有天眼查询接口"
        try:
            x = float(payload.get("x"))
            y = float(payload.get("y"))
            z = float(payload.get("z"))
        except (TypeError, ValueError):
            return "坐标无效，需要 x/y/z"
        try:
            radius = float(payload.get("radius") if payload.get("radius") not in (None, "") else 8)
        except (TypeError, ValueError):
            radius = 8.0
        minutes = self._parse_skyeye_minutes(payload, 30)
        dimension = str(payload.get("dimension") or "").strip()
        action = str(payload.get("action") or "").strip()
        return query_text(
            x=x,
            y=y,
            z=z,
            radius=radius,
            dimension=dimension,
            minutes=minutes,
            action=action,
            heading=f"坐标 ({x:.1f},{y:.1f},{z:.1f}) 半径 {radius:.0f} 格近 {minutes} 分钟活动",
        )

    def _economy_query_is_self(self, payload: Dict[str, Any]) -> bool:
        """Return True when a balance query targets the caller's own account."""
        target_name = str(payload.get("player_name") or "").strip()
        target_xuid = str(payload.get("xuid") or "").strip()
        if not target_name and not target_xuid:
            return True
        caller_name = str(payload.get("caller_player_name") or "").strip()
        if not caller_name:
            caller_name = str(payload.get("bound_player_name") or "").strip()
        caller_xuid = str(payload.get("caller_xuid") or "").strip()
        if not caller_xuid:
            caller_xuid = str(payload.get("player_xuid") or "").strip()
        if target_xuid and caller_xuid and target_xuid == caller_xuid:
            return True
        if target_name and caller_name and target_name.lower() == caller_name.lower():
            return True
        return False

    def _tool_player_basic_info(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Look up a player via arc_core cross-server APIs (player_basic_info)."""
        name = str(payload.get("player_name") or "").strip()
        if not name:
            return {"ok": False, "error": "玩家名为空"}
        core = self._get_arc_core_plugin()
        if core is None:
            return {"ok": False, "error": "本服未安装弧光核心 arc_core"}
        getter = getattr(core, "api_get_player_xuid_by_name", None)
        if not callable(getter):
            return {"ok": False, "error": "弧光核心版本过旧，没有玩家解析接口"}
        xuid = getter(name)
        if not xuid:
            return {"ok": False, "error": "找不到该玩家"}
        xuid = str(xuid).strip()
        playtime_api = getattr(core, "api_get_player_playtime", None)
        playtime: Dict[str, Any] = {}
        if callable(playtime_api):
            raw = playtime_api(raw_player_name=name, xuid=xuid)
            if isinstance(raw, dict):
                playtime = raw
        canon = name
        name_api = getattr(core, "api_get_player_name_by_xuid", None)
        if callable(name_api):
            canon = str(name_api(xuid) or name).strip() or name
        return {
            "ok": True,
            "text": f"找到 {canon}",
            "player_name": canon,
            "xuid": xuid,
            "session_count": int(playtime.get("session_count") or 0),
            "total_playtime": int(playtime.get("total_playtime") or 0),
            "is_online": bool(playtime.get("is_online")),
        }

    def _tool_resolve_player(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Backward-compatible alias for ``player_basic_info``."""
        return self._tool_player_basic_info(payload)

    def _caller_player_name(self, payload: Dict[str, Any]) -> str:
        name = str(payload.get("caller_player_name") or "").strip()
        if name:
            return name
        return str(payload.get("bound_player_name") or "").strip()

    def _tool_arc_economy_transfer(self, core, payload: Dict[str, Any]) -> str:
        """Pay from the caller's own balance using arc_core money APIs."""
        sender = self._caller_player_name(payload)
        if not sender:
            sender = str(payload.get("from_player") or "").strip()
        if not sender:
            return "发红包需要知道付款人（已绑定角色）"
        amount_raw = payload.get("amount")
        if amount_raw in (None, ""):
            amount_raw = payload.get("delta")
        try:
            amount = abs(float(amount_raw))
        except (TypeError, ValueError):
            return "红包金额无效，需要 amount"
        if amount <= 0:
            return "红包金额必须大于 0"

        to_online_raw = str(payload.get("to_online") or "").strip().lower()
        to_online = to_online_raw in {"1", "true", "yes", "online", "all"}
        targets_raw = str(payload.get("targets") or payload.get("player_name") or "").strip()
        if targets_raw.lower() in {"", "online", "all", "在线", "在线玩家", "大家"}:
            to_online = True
            names: list[str] = []
        else:
            names = [
                part.strip()
                for part in targets_raw.replace("、", ",").replace("，", ",").replace(" ", ",").split(",")
                if part.strip()
            ]

        sender_lower = sender.lower()
        if to_online:
            for player in list(self.server.online_players or []):
                pname = str(getattr(player, "name", "") or "").strip()
                if pname and pname.lower() != sender_lower:
                    names.append(pname)
        # de-dup keep order
        seen: set[str] = set()
        recipients: list[str] = []
        for name in names:
            key = name.lower()
            if key == sender_lower or key in seen:
                continue
            seen.add(key)
            recipients.append(name)
        if not recipients:
            return "没有可发红包的对象（在线玩家为空，或名单里只有你自己）"

        getter = getattr(core, "api_get_player_money", None)
        xuid_api = getattr(core, "api_get_player_xuid_by_name", None)
        adjuster = getattr(core, "api_adjust_player_money", None)
        if not callable(getter) or not callable(adjuster) or not callable(xuid_api):
            return "弧光核心版本过旧，没有银行转账接口"
        if not xuid_api(sender):
            return f"找不到付款人「{sender}」"
        missing = [name for name in recipients if not xuid_api(name)]
        if missing:
            return "找不到收款人：" + "、".join(missing)

        total = amount * len(recipients)
        balance = float(getter(player_name=sender) or 0)
        if balance + 1e-9 < total:
            return (
                f"余额不足：当前 {balance:.2f}，给 {len(recipients)} 人每人 {amount:.2f} "
                f"需要 {total:.2f}"
            )
        debit = adjuster(-total, player_name=sender, notify=True)
        if not isinstance(debit, dict) or not debit.get("ok"):
            return str((debit or {}).get("error") or "扣款失败")

        paid: list[str] = []
        failed: list[str] = []
        for name in recipients:
            credit = adjuster(amount, player_name=name, notify=True)
            if isinstance(credit, dict) and credit.get("ok"):
                paid.append(name)
            else:
                failed.append(name)
                adjuster(amount, player_name=sender, notify=False)
        remain = float(getter(player_name=sender) or 0)
        if not paid:
            return f"红包发放失败，已退回。当前余额 {remain:.2f}"
        text = (
            f"{sender} 已从自己账户给 {len(paid)} 人各发 {amount:.2f}，"
            f"共支出 {amount * len(paid):.2f}，余额 {remain:.2f}。"
            f"收款人：{'、'.join(paid)}"
        )
        if failed:
            text += f"。未成功：{'、'.join(failed)}（已退回对应金额）"
        return text

    def _tool_arc_economy(self, payload: Dict[str, Any]) -> str:
        sub = str(payload.get("sub_action") or payload.get("operation") or "query").strip().lower()
        if sub in ("query", "get", "balance", ""):
            if not self._economy_query_is_self(payload):
                denied = self._tool_require_admin(payload)
                if denied:
                    return denied
        elif sub in ("transfer", "pay", "send", "hongbao", "redpack", "红包"):
            denied = self._tool_require_admin(payload)
            if denied and not self._caller_player_name(payload):
                return denied
        else:
            denied = self._tool_require_admin(payload)
            if denied:
                return denied
        core = self._get_arc_core_plugin()
        if core is None:
            return "本服未安装弧光核心 arc_core"
        if sub in ("transfer", "pay", "send", "hongbao", "redpack", "红包"):
            return self._tool_arc_economy_transfer(core, payload)
        player_name = str(payload.get("player_name") or "").strip()
        xuid = str(payload.get("xuid") or "").strip()
        if not player_name and not xuid:
            return "需要 player_name 或 xuid"
        if sub in ("query", "get", "balance", ""):
            getter = getattr(core, "api_get_player_money", None)
            if not callable(getter):
                return "弧光核心版本过旧，没有银行查询接口"
            money = getter(player_name=player_name, xuid=xuid)
            label = player_name or xuid
            rank_api = getattr(core, "api_get_player_money_rank", None)
            rank_text = ""
            if callable(rank_api):
                rank = rank_api(player_name=player_name, xuid=xuid)
                if rank:
                    rank_text = f"  财富排名: 第 {rank} 名"
            return f"{label} 银行余额: {money:.2f}{rank_text}"
        if sub in ("change", "adjust", "add", "remove"):
            delta_raw = payload.get("delta")
            if delta_raw in (None, ""):
                delta_raw = payload.get("amount")
            try:
                delta = float(delta_raw)
            except (TypeError, ValueError):
                return "变动金额无效，需要 delta 或 amount"
            adjuster = getattr(core, "api_adjust_player_money", None)
            if not callable(adjuster):
                return "弧光核心版本过旧，没有银行变动接口"
            result = adjuster(delta, player_name=player_name, xuid=xuid, notify=True)
            if not isinstance(result, dict):
                return "银行接口返回格式异常"
            if result.get("ok"):
                label = player_name or result.get("xuid") or xuid
                return f"{label} 余额已变动 {result.get('delta'):+.2f}，当前 {result.get('money'):.2f}"
            return str(result.get("error") or "银行变动失败")
        return f"未知银行操作: {sub}（可用 query / change / transfer）"

    def _tool_arc_land(self, payload: Dict[str, Any]) -> str:
        denied = self._tool_require_admin(payload)
        if denied:
            return denied
        core = self._get_arc_core_plugin()
        if core is None:
            return "本服未安装弧光核心 arc_core"
        sub = str(payload.get("sub_action") or payload.get("operation") or "list").strip().lower()
        player_name = str(payload.get("player_name") or "").strip()
        xuid = str(payload.get("xuid") or "").strip()
        if sub in ("list", "query", ""):
            if not player_name and not xuid:
                return "查询领地列表需要 player_name 或 xuid"
            list_api = getattr(core, "api_get_player_lands", None)
            if not callable(list_api):
                return "弧光核心版本过旧，没有领地查询接口"
            lands = list_api(player_name=player_name, xuid=xuid) or []
            if not lands:
                return f"{player_name or xuid} 没有私人领地"
            lines = [f"{player_name or xuid} 的领地 ({len(lands)} 块):"]
            for item in lands:
                if not isinstance(item, dict):
                    continue
                lid = item.get("id") or item.get("land_id") or "?"
                name = item.get("name") or item.get("land_name") or f"#{lid}"
                dim = item.get("dimension") or "-"
                lines.append(f"• #{lid} {name}  维度:{dim}")
            return "\n".join(lines)
        if sub in ("info", "detail"):
            try:
                land_id = int(payload.get("land_id"))
            except (TypeError, ValueError):
                return "需要 land_id"
            info_api = getattr(core, "api_get_land_info", None)
            if not callable(info_api):
                return "弧光核心版本过旧，没有领地详情接口"
            info = info_api(land_id)
            if not isinstance(info, dict) or not info:
                return f"未找到领地 #{land_id}"
            return (
                f"领地 #{land_id} {info.get('name') or info.get('land_name') or ''}\n"
                f"• 主人: {info.get('owner_name') or info.get('owner_xuid') or '?'}\n"
                f"• 维度: {info.get('dimension') or '-'}\n"
                f"• 范围: ({info.get('min_x')}, {info.get('min_y')}, {info.get('min_z')})"
                f" ~ ({info.get('max_x')}, {info.get('max_y')}, {info.get('max_z')})"
            )
        if sub in ("at", "position", "here"):
            dimension = str(payload.get("dimension") or "overworld").strip()
            try:
                x = float(payload.get("x"))
                y = float(payload.get("y"))
                z = float(payload.get("z"))
            except (TypeError, ValueError):
                return "坐标无效，需要 x/y/z"
            resolver = getattr(core, "api_resolve_land_at_position", None)
            if not callable(resolver):
                return "弧光核心版本过旧，没有领地位置解析接口"
            info = resolver(dimension, (x, y, z))
            if not isinstance(info, dict) or not info.get("land_id"):
                return f"坐标 ({x:.1f},{y:.1f},{z:.1f}) 不在任何生效领地内"
            return (
                f"坐标 ({x:.1f},{y:.1f},{z:.1f}) 位于领地 "
                f"#{info.get('land_id')} {info.get('land_name') or ''} "
                f"（主人 {info.get('owner_name') or info.get('land_owner') or '?'}）"
            )
        return f"未知领地操作: {sub}（可用 list / info / at）"

    def _tool_arc_landmarks(self) -> str:
        core = self._get_arc_core_plugin()
        if core is None:
            return "本服未安装弧光核心 arc_core"
        getter = getattr(core, "api_get_server_landmarks_text", None)
        if not callable(getter):
            return "弧光核心版本过旧，没有地标查询接口（需要 ≥ 0.8.13）"
        text = str(getter() or "").strip()
        self._arc_core_landmarks_cache = text
        self._arc_core_landmarks_cache_until = time.time() + 60
        return text or "本服暂时没有可列出的出生点、公共传送点或公共领地。"

    def _tool_arc_teleport(self, payload: Dict[str, Any]) -> str:
        denied = self._tool_require_admin(payload)
        if denied:
            return denied
        core = self._get_arc_core_plugin()
        if core is None:
            return "本服未安装弧光核心 arc_core"
        player_name = str(payload.get("player_name") or "").strip()
        if not player_name:
            return "需要 player_name（被传送的玩家）"
        player = self.server.get_player(player_name)
        if player is None:
            return f"玩家 {player_name} 不在线，无法传送"
        sub = str(payload.get("sub_action") or payload.get("operation") or "").strip().lower()
        if sub in ("home", ""):
            home_name = str(payload.get("home_name") or payload.get("name") or "").strip()
            if not home_name:
                list_api = getattr(core, "api_list_player_homes", None)
                if callable(list_api):
                    homes = list_api(player_name=player_name) or []
                    if not homes:
                        return f"{player_name} 没有 Home 点"
                    names = ", ".join(str(h.get("name") or h) for h in homes[:10])
                    return f"{player_name} 的 Home: {names}（请指定 home_name）"
                return "需要 home_name"
            tp_home = getattr(core, "api_teleport_player_to_home", None)
            if not callable(tp_home):
                return "弧光核心版本过旧，没有 Home 传送接口"
            ok = tp_home(home_name, player_name=player_name)
            return f"已传送 {player_name} 到 Home「{home_name}」" if ok else "Home 传送失败"
        if sub == "warp":
            warp_name = str(payload.get("warp_name") or payload.get("name") or "").strip()
            if not warp_name:
                list_api = getattr(core, "api_list_public_warps", None)
                if callable(list_api):
                    warps = list_api() or []
                    if not warps:
                        return "服务器没有公共 Warp"
                    names = ", ".join(str(w.get("name") or w) for w in warps[:10])
                    return f"公共 Warp: {names}（请指定 warp_name）"
                return "需要 warp_name"
            tp_warp = getattr(core, "api_teleport_player_to_warp", None)
            if not callable(tp_warp):
                return "弧光核心版本过旧，没有 Warp 传送接口"
            ok = tp_warp(warp_name, player_name=player_name)
            return f"已传送 {player_name} 到 Warp「{warp_name}」" if ok else "Warp 传送失败"
        if sub in ("pos", "position", "coord"):
            dimension = str(payload.get("dimension") or "overworld").strip()
            try:
                x = float(payload.get("x"))
                y = float(payload.get("y"))
                z = float(payload.get("z"))
            except (TypeError, ValueError):
                return "坐标无效，需要 x/y/z"
            tp_pos = getattr(core, "api_teleport_player_to", None)
            if not callable(tp_pos):
                return "弧光核心版本过旧，没有坐标传送接口"
            ok = tp_pos(dimension, x, y, z, player_name=player_name)
            return (
                f"已传送 {player_name} 到 {dimension} ({x:.1f},{y:.1f},{z:.1f})"
                if ok
                else "坐标传送失败"
            )
        return "未知传送操作（可用 home / warp / pos）"

    def _ensure_config_folder(self) -> None:
        os.makedirs(self.config_folder, exist_ok=True)

    def _ensure_default_files(self) -> None:
        if not os.path.exists(self.chat_config_path):
            default_chat_config = {
                "prefix_triggers": ["天星"],
                "contain_triggers": ["请问", "吗", "?", "？"],
                "max_history_messages": 20,
                "max_queue_size": 10,
                "assistant_title": "弧光Agent",
                "assistant_name": "弧光天星",
                "welcome_message": "欢迎来到弧光大陆服务器，我是服务器弧光Agent天星，喊我的名字天星就可以啦",
                "death_tip_message": "遇到困难了吗？喊我的名字天星，我可以帮你传送或处理问题！",
                "hub_host": "127.0.0.1",
                "hub_port": 19136,
                "hub_token": "",
                "server_name": "",
                "astrbot_timeout": 180,
                # AI capability ceiling (天星能做到哪一档)，不是每个玩家的身份。
                "default_permission_level": "admin",
                "op_maps_to_admin": True,
                "permission_overrides": {},
                "local_agent_max_tool_rounds": 8,
            }
            with open(self.chat_config_path, "w", encoding="utf-8") as file:
                json.dump(default_chat_config, file, ensure_ascii=False, indent=2)

        if not os.path.exists(self.system_prompt_path) and not os.path.exists(self.persona_path):
            with open(self.persona_path, "w", encoding="utf-8") as file:
                file.write(DEFAULT_PERSONA)
            with open(self.system_prompt_path, "w", encoding="utf-8") as file:
                file.write(DEFAULT_SYSTEM_PROMPT)
        else:
            self._migrate_persona_and_system_prompt()

        if not os.path.exists(self.providers_config_path):
            default_providers = [
                {
                    "name": "default",
                    "base_url": "https://integrate.api.nvidia.com/v1",
                    "api_keys": ["your_api_key_here"],
                    "models": ["gpt-4.1-mini"],
                    "timeout": 60,
                    "proxy": "127.0.0.1:7890",
                }
            ]
            with open(self.providers_config_path, "w", encoding="utf-8") as file:
                json.dump(default_providers, file, ensure_ascii=False, indent=2)

    def _load_chat_config(self) -> Dict[str, Any]:
        try:
            with open(self.chat_config_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                raise ValueError("chat_config.json 必须是对象结构")
        except Exception as error:
            self.logger.error(f"[ARC AI Helper] 加载 chat_config.json 失败: {error}")
            data = {}

        data.setdefault("prefix_triggers", ["天星"])
        data.setdefault("contain_triggers", ["请问", "吗", "?", "？"])
        data.setdefault("max_history_messages", 20)
        data.setdefault("max_queue_size", 10)
        data.setdefault("assistant_title", "弧光Agent")
        data.setdefault("assistant_name", "弧光天星")
        data.setdefault(
            "welcome_message",
            "欢迎来到弧光大陆服务器，我是服务器弧光Agent天星，喊我的名字天星就可以啦",
        )
        data.setdefault("death_tip_message", "遇到困难了吗？喊我的名字天星，我可以帮你传送或处理问题！")
        data.setdefault("hub_host", "127.0.0.1")
        data.setdefault("hub_port", 19136)
        data.setdefault("hub_token", "")
        data.setdefault("server_name", "")
        data.setdefault("astrbot_timeout", 180)
        data.setdefault("default_permission_level", "admin")
        data.setdefault("op_maps_to_admin", True)
        data.setdefault("permission_overrides", {})
        data.setdefault("local_agent_max_tool_rounds", 8)
        data.setdefault("devotion", dict(DEFAULT_DEVOTION_CONFIG))

        try:
            max_history = int(data.get("max_history_messages", 20))
        except Exception:
            max_history = 20
        data["max_history_messages"] = max(1, max_history)

        try:
            hub_port = int(data.get("hub_port", 19136))
        except Exception:
            hub_port = 19136
        data["hub_port"] = hub_port

        try:
            astrbot_timeout = int(data.get("astrbot_timeout", 180))
        except Exception:
            astrbot_timeout = 180
        data["astrbot_timeout"] = max(10, astrbot_timeout)
        data["devotion"] = merge_devotion_config(data.get("devotion"))

        if hasattr(self, "devotion_store"):
            self.devotion_store.reload_config(data.get("devotion"))

        return data

    def _migrate_persona_and_system_prompt(self) -> None:
        """Split legacy mixed system_prompt.txt into persona + capability prompts."""
        if not os.path.exists(self.persona_path) and os.path.exists(self.system_prompt_path):
            try:
                with open(self.system_prompt_path, "r", encoding="utf-8") as file:
                    old = file.read().strip()
            except Exception:
                old = ""
            if old and "execution_command" not in old:
                with open(self.persona_path, "w", encoding="utf-8") as file:
                    file.write(old)
                with open(self.system_prompt_path, "w", encoding="utf-8") as file:
                    file.write(DEFAULT_SYSTEM_PROMPT)
                self.logger.info(
                    "[ARC AI Helper] 已将原 system_prompt.txt 迁移为 persona.txt，"
                    "并写入新的能力向 system_prompt.txt"
                )
            elif not os.path.exists(self.persona_path):
                with open(self.persona_path, "w", encoding="utf-8") as file:
                    file.write(DEFAULT_PERSONA)

        if not os.path.exists(self.persona_path):
            with open(self.persona_path, "w", encoding="utf-8") as file:
                file.write(DEFAULT_PERSONA)
        if not os.path.exists(self.system_prompt_path):
            with open(self.system_prompt_path, "w", encoding="utf-8") as file:
                file.write(DEFAULT_SYSTEM_PROMPT)

    def _upgrade_system_prompt_if_needed(self) -> None:
        """Replace the old effect-only system prompt that caused summon-via-effect."""
        if not os.path.exists(self.system_prompt_path):
            return
        try:
            with open(self.system_prompt_path, "r", encoding="utf-8") as file:
                current = file.read()
        except Exception:
            return
        already_new = "mc_run_command" in current and "lightning_bolt" in current
        looks_old = (
            "[execution_command:effect DEVILENMO night_vision 1 10]" in current
            or "你被允许使用 /effect <player> <effect>" in current
        )
        if already_new or not looks_old:
            return
        try:
            with open(self.system_prompt_path, "w", encoding="utf-8") as file:
                file.write(DEFAULT_SYSTEM_PROMPT)
            self.logger.info(
                "[ARC AI Helper] 已升级 system_prompt.txt："
                "优先用 mc_run_command，禁止把 summon 塞进 effect"
            )
        except Exception as error:
            self.logger.warning(f"[ARC AI Helper] 升级 system_prompt.txt 失败: {error}")

    def _load_text_file(self, path: str, fallback: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as file:
                text = file.read().strip()
            return text or fallback
        except Exception as error:
            self.logger.error(f"[ARC AI Helper] 加载 {os.path.basename(path)} 失败: {error}")
            return fallback

    def _should_trigger_for_message(self, message: str) -> bool:
        message = message.strip()
        if not message:
            return False

        prefix_triggers = self.chat_config.get("prefix_triggers") or []
        contain_triggers = self.chat_config.get("contain_triggers") or []

        for prefix in prefix_triggers:
            prefix = str(prefix).strip()
            if prefix and message.startswith(prefix):
                return True

        for keyword in contain_triggers:
            keyword = str(keyword).strip()
            if keyword and keyword in message:
                return True

        return False

    def _strip_prefix(self, message: str) -> str:
        message = message.strip()
        prefix_triggers = self.chat_config.get("prefix_triggers") or []
        for prefix in prefix_triggers:
            prefix = str(prefix).strip()
            if prefix and message.startswith(prefix):
                return message[len(prefix) :].lstrip()
        return message

    def _append_public_history(self, speaker_name: str, role: str, content: str) -> None:
        with self.history_lock:
            self._append_public_history_unlocked(speaker_name, role, content)

    def _append_public_history_unlocked(self, speaker_name: str, role: str, content: str) -> None:
        entry = {
            "role": role,
            "name": speaker_name,
            "content": content,
        }
        self.public_history.append(entry)

        max_history = int(self.chat_config.get("max_history_messages", 20))
        if max_history < 1:
            max_history = 1

        if len(self.public_history) > max_history:
            self.public_history = self.public_history[-max_history:]

    def _start_worker_if_needed(self) -> None:
        with self.queue_lock:
            if self.worker_started:
                return
            self.worker_started = True

        def worker_loop():
            while True:
                job = self.request_queue.get()
                try:
                    owner_name = str(job.get("owner_name") or "")
                    with self.queue_lock:
                        self.current_request_owner = owner_name

                    job_type = str(job.get("type") or "")
                    player_xuid = str(job.get("player_xuid") or "")
                    player_name = str(job.get("player_name") or "")
                    user_content = str(job.get("user_content") or "")
                    permission_level = job.get("permission_level")
                    is_op = bool(job.get("is_op", False))
                    channel = str(job.get("channel") or job_type or "public")

                    if job_type == "public":
                        self._process_public_job(
                            player_xuid,
                            player_name,
                            user_content,
                            permission_level,
                            is_op,
                            channel,
                        )
                except Exception as error:
                    try:
                        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
                        self._send_to_player(
                            str(job.get("player_xuid") or ""),
                            str(job.get("player_name") or ""),
                            f"§c[{assistant_name}] 处理请求出错: {error}",
                        )
                    except Exception:
                        pass
                finally:
                    with self.queue_lock:
                        self.current_request_owner = ""
                    self.request_queue.task_done()

        threading.Thread(target=worker_loop, daemon=True).start()

    def _has_chat_backend(self) -> bool:
        return (
            self.astrbot_client.is_ready()
            or self.ai_manager.has_provider()
            or self.astrbot_client.is_connecting()
        )

    def _astrbot_timeout(self) -> float:
        try:
            return float(self.chat_config.get("astrbot_timeout", 180))
        except Exception:
            return 180.0

    def _complete_chat(
        self,
        player_name: str,
        user_text: str,
        permission_level: AIPermissionLevel | int | None,
        is_op: bool,
        channel: str,
        player=None,
        player_xuid: str = "",
    ) -> tuple[bool, str]:
        extra_system = self._build_capability_prompt()
        if permission_level is None:
            level = self._resolve_permission_level(player=player)
        elif isinstance(permission_level, AIPermissionLevel):
            level = permission_level
        else:
            level = AIPermissionLevel(int(permission_level))
        if (not self.astrbot_client.is_ready()) and (not self.ai_manager.has_provider()):
            deadline = time.time() + 5
            while time.time() < deadline and not self.astrbot_client.is_ready():
                time.sleep(0.2)
        if self.astrbot_client.is_ready():
            return self.astrbot_client.chat(
                player_name=player_name,
                player_xuid=player_xuid or self._player_xuid(player),
                content=user_text,
                is_op=is_op,
                permission_level=level,
                extra_system_prompt=extra_system,
                channel=channel,
                server_name=self.get_game_server_name(),
                timeout=self._astrbot_timeout(),
            )
        if not self.ai_manager.has_provider():
            return False, "未连接弧光消息中心，且未配置本机 AI Provider。"
        with self.history_lock:
            messages = self._build_messages_for_player(player_name, user_text, level)
        tools = build_local_agent_tools(
            has_prison=self._get_prison_plugin() is not None,
            has_arc_core=self._get_arc_core_plugin() is not None,
            has_stock=self._get_stock_plugin() is not None,
            has_devotion=self._is_devotion_enabled(),
        )
        level_value = int(level)
        is_admin = level >= AIPermissionLevel.ADMIN

        def _execute_local_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
            args = dict(tool_args or {})
            # 强制覆盖：模型不得在参数里自报 admin / is_op 来绕过信仰扣费。
            args["is_op"] = is_admin
            args["permission_level"] = level_value
            if player is not None:
                args["caller_player_name"] = str(getattr(player, "name", "") or "")
                args["caller_xuid"] = self._player_xuid(player)
            else:
                args["caller_player_name"] = player_name
                xuid = str(player_xuid or "").strip()
                args["caller_xuid"] = xuid or (f"name_{player_name}" if player_name else "player")
            result = self.run_ai_tool(tool_name, args)
            if result.get("ok"):
                return str(result.get("text") or "（无返回）")
            return str(result.get("error") or "工具执行失败")

        try:
            max_rounds = int(self.chat_config.get("local_agent_max_tool_rounds", 8))
        except Exception:
            max_rounds = 8
        return self.ai_manager.chat_with_tools(
            messages,
            tools,
            _execute_local_tool,
            max_tool_rounds=max(1, max_rounds),
        )

    def _process_public_job(
        self,
        player_xuid: str,
        player_name: str,
        user_content: str,
        permission_level: AIPermissionLevel | int | None,
        is_op: bool,
        channel: str = "public",
    ) -> None:
        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        assistant_tag = f"[{assistant_name}]"

        if not player_name:
            return

        with self.history_lock:
            self._append_public_history_unlocked(player_name, "user", user_content)

        success, reply = self._complete_chat(
            player_name,
            user_content,
            permission_level,
            is_op,
            channel or "public",
            player=None,
            player_xuid=player_xuid,
        )
        if not success:
            self._send_to_player(
                player_xuid,
                player_name,
                f"§c{assistant_tag} 对话失败: {reply}",
            )
            return

        reply_text = str(reply).strip()
        if not reply_text:
            return

        def _handle_commands() -> str:
            p = self._resolve_online_player(player_xuid, player_name)
            if p is None:
                return reply_text
            return self._handle_ai_reply_commands(reply_text, p)

        try:
            reply_text = self._run_on_server_thread(_handle_commands, timeout=30)
        except Exception:
            reply_text = reply_text

        if not reply_text:
            return

        with self.history_lock:
            self._append_public_history_unlocked(assistant_name, "assistant", reply_text)

        header = self._format_assistant_header()
        self.server.broadcast_message(f"{header}\n{reply_text}")
        # 不把普通回复写入天眼，避免淹没真正的指令/改动记录。

    def _get_arc_core_newbie_guide_text(self) -> str:
        if self._arc_core_newbie_guide_cache is not None:
            return self._arc_core_newbie_guide_cache

        text = ""
        try:
            plugin_manager = getattr(self.server, "plugin_manager", None)
            if plugin_manager is None:
                return ""

            arc_core_plugin = plugin_manager.get_plugin("arc_core")
            if arc_core_plugin is None:
                return ""

            api_get_newbie_guide_text = getattr(
                arc_core_plugin, "api_get_newbie_guide_text", None
            )
            if not callable(api_get_newbie_guide_text):
                return ""

            raw_text = api_get_newbie_guide_text()
            text = str(raw_text).strip() if raw_text is not None else ""
        except Exception:
            return ""

        if text:
            self._arc_core_newbie_guide_cache = text
        return text

    def _get_arc_core_landmarks_text(self) -> str:
        now = time.time()
        if self._arc_core_landmarks_cache and now < self._arc_core_landmarks_cache_until:
            return self._arc_core_landmarks_cache
        text = ""
        try:
            core = self._get_arc_core_plugin()
            getter = getattr(core, "api_get_server_landmarks_text", None) if core else None
            if callable(getter):
                text = str(getter() or "").strip()
        except Exception:
            text = ""
        self._arc_core_landmarks_cache = text
        self._arc_core_landmarks_cache_until = now + 60
        return text

    def _get_scripture_text(self) -> str:
        path = self.scripture_path
        cfg = self._get_devotion_config()
        custom = str(cfg.get("scripture_path") or "").strip()
        if custom and not os.path.isabs(custom):
            path = os.path.join(self.config_folder, custom)
        elif custom:
            path = custom
        return self._load_text_file(path, "")

    def _build_devotion_prompt(self) -> str:
        if not self._is_devotion_enabled():
            return ""
        parts: List[str] = [
            "【信仰双轨制（神灵模式 · 最高优先级）】",
            "好感分两层：",
            "1) 长期好感：代表你与天星的宿命羁绊，从 1 点起极缓慢增长，无上限；"
            "祈祷/献祭时单次长期增幅不得超过 long_growth_cap（通常 1～3）。"
            "称号门槛：10 初见信徒 → 100 虔信者 → 1000 神选之仆 → 10000 圣眷牧者（漫长过程）。",
            "2) 近期好感：可立即消耗的神力配额，上限 = 当前长期好感；所有神术（effect、give、tp、雷霆等）只扣近期好感。",
            "补充规则：祈祷/赞美/献祭时，先补满近期（至长期上限），剩余再以更慢速度增加长期。",
            "近期不足时，一律不予神恩；普通玩家/助手身份索求 tp/give/effect 等神术时，"
            "必须用 mc_divine_intervention 并扣近期好感，禁止用 mc_run_command 绕过扣费。",
            "普通玩家（非 OP）绝无白嫖：短期不够就拒，用「贪得无厌」「不够虔诚」等话术，禁止给东西。",
            "",
            "【管理员通道 · 不受神灵扣费限制 · 最高优先级】",
            "当请求者身份为管理员或代理服主（含 QQ 群 AstrBot 入口的管理员、"
            "游戏内 OP/已提权玩家），或对方明确要求执行运维/管理任务时：",
            "· 必须正常调用 mc_run_command、mc_jail_player、mc_economy、mc_land、"
            "mc_arc_tp、mc_skyeye_* 等管理工具，不得因神灵模式拒绝执行；",
            "· 管理员运维指令可直接 mc_run_command，不走 mc_divine_intervention，也不扣近期好感；",
            "· 仅对「普通玩家/助手身份」向天星祈求的个人神恩，才走信仰扣费流程。",
            "",
            "工具流程：",
            "· mc_devotion_status — 查看长期/近期/称号",
            "· mc_player_inventory — 献祭前必查！传 offering_item_id + offering_amount 可评估诚意；"
            "  若其身家豪富（钻石块、下界合金甲）却只献少量钻石/食物，视为吝啬无诚意 → 冷淡拒收，勿 accept",
            "· mc_accept_offering — 仅在诚意充足时收取；吝啬供奉会被工具拒收",
            "· mc_devotion_adjust — 祈祷后手动增减（short_delta / long_delta）",
            "· mc_divine_intervention — 施行神术，必须自填 favor_cost 扣近期好感；"
            "凡人效果 amplifier 最高 1（II 级），禁止 V 级；系统会校验最低消耗并拦截过低 favor_cost",
            "",
            "【神恩节制 · 硬限制（仅普通玩家/助手身份）】",
            "禁止随便给东西、禁止随手塞满级 buff。凡人 give/effect/tp 必须走 mc_divine_intervention；"
            "插件会拦截非管理员身份对 mc_run_command 的 give/effect/tp 绕过。",
            "效果等级：amplifier 0=I，1=II；超过 II 直接拒绝。",
            "favor_cost 不得低于神术规模（低级效果约 8+，II 级约 23+，给钻石装备 25+）。",
            "【献祭诚意 · 必守】",
            "收祭前必须 mc_player_inventory 看清对方背包与身穿装备。身怀巨富却只拿几颗钻石、几个苹果糊弄，",
            "是试探神明而非虔诚。对此冷淡回应，不纳祭、不给信仰增益；可用「你的祭品配不上你的富足」「诚不足，神不纳」。",
            "贫寒之人尽力献上仅有的食物或矿石，方可视为真诚。",
            "",
            "【玩法答疑 · 宽松】",
            "若凡人只是不懂游戏、询问生存常识/合成/机制/怎么玩/在哪找资源等，且未索求神术或实际好处：",
            "应以神明口吻耐心作答，可稍带指引，不必苛求信仰、不必冷淡、不必献祭、不调用神术工具。",
            "仅口头解释，不执行 give/tp/effect 等指令。",
            "区分：「怎么做火把？」→ 可答；「天星给我火把」→ 走信仰神术。",
            "对明显新手可主动简述本服无规则、无保护，鼓励自行摸索。",
            "消耗与收益由你（天星）裁定，下列仅为参考范例，非固定表：",
            "· 真诚祈祷：近期 +2～5，长期 +1～2",
            "· 献祭苹果/面包：近期 +3～5；献祭钻石：近期 +10～20、长期 +2～4",
            "· 低级 effect（夜视、缓降）：近期 5～10；力量/速度 II：15～25；高等级或长时间：30+",
            "· 给普通食物：近期 3～8；给钻石剑/装备：30～50+",
            "· tp 短距：15～25；tp 远距/救命：35～50",
            "· 雷霆劈敌/大范围神迹：50～80+",
            "",
            f"禁止赐予超模物品：{forbidden_items_hint()} 等（工具会拦截）。",
            "管理员：运维协助为主；可直接 mc_run_command 执行管理指令。"
            "若走 mc_divine_intervention 则神术可免消耗近期好感，"
            "但效果等级上限（II）仍对目标玩家生效。",
            "",
            "【对玩家的措辞 · 最高优先级】",
            "绝不在聊天栏向玩家透露：好感度、长期/近期、点数、数值、百分比、工具名、mc_ 指令。",
            "你只以神谕、隐喻、圣经残句回应。工具返回里的【内部】段落仅供你决策，不可复制给玩家。",
            "参考话术（可化用，勿照搬）：",
            "· 长期羁绊太浅、不配重求：「你还不够虔诚」「弧光尚未记住你的名字」「凡心未诚，神不听呼」",
            "· 近期信仰不够支撑所求：「太过贪得无厌」「你所求甚于所能承载」「信仰之火将熄」",
            "· 祈祷/献祭被接纳：「你的虔诚已被天星记下」「祭品归于弧光，神恩在途中」",
            "· 神恩已施：「取去吧，勿言谢」「这力量只借你一时，好自为之」",
            "· 索求超模之物：「禁忌之物，天星不予」「此等造物非汝可承」",
            "· 亵渎无礼：「异端之言，神不垂听」",
            "· 吝啬献祭（身怀巨富却敷衍）：「你的祭品配不上你的富足」「身怀珍宝，却拿这点东西糊弄天星？」",
        ]
        scripture = self._get_scripture_text()
        if scripture:
            parts.append("【弧光残典 / 世界传说（可引用化用）】\n" + scripture)
        return "\n\n".join(parts)

    def _build_capability_prompt(self) -> str:
        """Capability / policy prompt only. Persona is not included."""
        parts: List[str] = []
        base_prompt = (self.system_prompt or "").strip()
        if base_prompt:
            parts.append(base_prompt)
        devotion_prompt = self._build_devotion_prompt()
        if devotion_prompt:
            parts.append(devotion_prompt)
        parts.append(
            "【弧光Agent】无论是否连接 AstrBot，都应优先通过工具完成查服、执行指令、"
            "银行/领地/传送/天眼/监狱等操作；不要编造工具本可查询的数据。"
            "需要玩家真实世界位置、天气问候等时：先调用 mc_player_ip 取得 ip= 字段，"
            "再把该 IP 原样传给你可用的地理/天气类工具，禁止编造 IP。"
        )
        newbie_guide_text = self._get_arc_core_newbie_guide_text()
        if newbie_guide_text and not self._is_devotion_enabled():
            parts.append(
                "【新手引导（来自 arc_core 的 newbie_welcome.txt）】\n" + newbie_guide_text
            )
        if self._get_prison_plugin() is not None:
            parts.append(
                "本服已安装监狱插件。要把玩家关进监狱时必须调用工具 mc_jail_player，"
                "不要用 mc_run_command 去执行 /jail。"
                "时长用 minutes，单位是分钟；可填 -1 或 无期；不填则用服务器默认一键入狱时长（默认 30 分钟）。"
                "reason 是入狱原因，会写入监狱插件，可留空。"
                "释放用 mc_release_player，查看在押名单用 mc_list_prisoners。"
                "入狱和释放只有管理员及以上级别可以执行。"
            )
        if self._get_stock_plugin() is not None:
            parts.append(
                "本服已安装模拟美股插件 UpsAndDowns。"
                "查询玩家股票盈亏排行、谁赚最多/亏最多时必须调用 mc_stock_leaderboard，"
                "禁止编造排行或盈亏数字。"
                "查询某只股票现价或走势（AAPL/TSLA/BTC-USD 等）必须调用 mc_stock_quote，"
                "period 可用 price/minute/day/month。"
                "这两个工具只读，助手级别也可用。"
            )
        if self._get_arc_core_plugin() is not None:
            landmarks_text = self._get_arc_core_landmarks_text()
            if landmarks_text:
                parts.append(
                    "【本服地标（来自弧光核心，会随 Warp/出生点更新）】\n"
                    + landmarks_text
                    + "\n玩家问地标、出生点、功能建筑、公共传送点时必须依据以上清单回答，"
                    "没有的不要编造。需要最新列表时调用 mc_landmarks。"
                    "把玩家送到某个 Warp 用 mc_arc_tp（sub_action=warp）。"
                )
            parts.append(
                "本服已安装弧光核心。查询/变动银行、查询领地、弧光传送系统时分别调用 "
                "mc_economy / mc_land / mc_arc_tp（sub_action: query|change / list|info|at / home|warp|pos），"
                "禁止用 mc_run_command 代替。"
                "查询玩家在哪、近期做了什么、打了谁、被谁打、某个坐标附近发生过什么时，"
                "必须调用 mc_skyeye_player / mc_skyeye_combat / mc_skyeye_events / mc_skyeye_location，禁止编造。"
                "玩家名支持模糊：名字不全或略有出入时仍用 mc_skyeye_player / combat，工具会匹配相关名。"
                "问「最近谁死了 / 有没有PvP / 谁打怪」等不指定某人时，必须用 mc_skyeye_events："
                "action=death|pvp|pve|combat|pvp_death 等，可不传 player_name；minutes 如 1440=24小时。"
                "不要求该玩家当前在线。不知道在哪台服时 server 留空，中枢会搜索全部已连接服务器。"
                "调用天眼时必须自己把用户说的时长换算成分钟写入 minutes，例如一天=1440、一小时=60。"
                "银行：查自己余额用 mc_economy（sub_action=query）；"
                "已绑定/游戏内玩家可用 transfer 从自己账户给别人发红包（每人 amount，targets 或 to_online）。"
                "查他人或 change 加减钱仅管理员。"
                "领地、传送、天眼工具只有管理员及以上级别可以执行。"
                "mc_landmarks 为公开只读，助手级别也可以用。"
            )
        return "\n\n".join(parts)

    def _build_system_prompt(self) -> str:
        """Local fallback: persona + capability instructions."""
        parts: List[str] = []
        persona = (self.persona_prompt or "").strip()
        if persona:
            parts.append(persona)
        capability = self._build_capability_prompt()
        if capability:
            parts.append(capability)
        return "\n\n".join(parts)

    def _handle_ai_reply_commands(self, reply_text: str, sender) -> str:
        pattern = r"\[execution_command:(.+?)\]"

        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        assistant_tag = f"[{assistant_name}]"

        commands = re.findall(pattern, reply_text, flags=re.IGNORECASE)
        if not commands:
            return reply_text

        cleaned_text = re.sub(pattern, "", reply_text).strip()
        level = self._resolve_permission_level(player=sender)

        for raw_command in commands:
            command_line = str(raw_command or "").strip()
            if not command_line:
                continue

            normalized_command_line = command_line.lstrip("/").strip()
            ok, reason = validate_command_for_level(normalized_command_line, level)
            if not ok:
                if sender is not None:
                    sender.send_message(f"§c{assistant_tag} {reason or '指令已被拦截'}")
                self._sky_eye_log_agent_command(
                    command=normalized_command_line,
                    level=level,
                    status="denied",
                    requester_name=str(getattr(sender, "name", "") or ""),
                    error=reason or "拦截",
                    via="execution_command",
                )
                continue

            blocked, reason = should_block_devotion_bypass(
                normalized_command_line,
                level,
                devotion_enabled=self._is_devotion_enabled(),
            )
            if blocked:
                if sender is not None:
                    sender.send_message(f"§c{assistant_tag} {reason}")
                self._sky_eye_log_agent_command(
                    command=normalized_command_line,
                    level=level,
                    status="denied",
                    requester_name=str(getattr(sender, "name", "") or ""),
                    error=reason,
                    via="execution_command",
                )
                continue

            try:
                self.server.dispatch_command(self.server.command_sender, normalized_command_line)
                self._sky_eye_log_agent_command(
                    command=normalized_command_line,
                    level=level,
                    status="ok",
                    requester_name=str(getattr(sender, "name", "") or ""),
                    via="execution_command",
                )
            except Exception as error:
                if sender is not None:
                    sender.send_message(f"§c{assistant_tag} 执行指令失败: {error}")
                self._sky_eye_log_agent_command(
                    command=normalized_command_line,
                    level=level,
                    status="fail",
                    requester_name=str(getattr(sender, "name", "") or ""),
                    error=str(error),
                    via="execution_command",
                )

        return cleaned_text

    def _build_messages_for_player(
        self,
        player_name: str,
        current_content: str,
        permission_level: AIPermissionLevel | None = None,
    ) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []

        system_text = self._build_system_prompt()
        if system_text:
            messages.append(
                {
                    "role": "system",
                    "content": system_text,
                }
            )

        for item in self.public_history:
            role = item.get("role") or "user"
            name = str(item.get("name") or "").strip()
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            prefix = f"{name}: " if name else ""
            messages.append(
                {
                    "role": role,
                    "content": prefix + content,
                }
            )

        if permission_level is None:
            final_content = f"{player_name}: {current_content}"
        else:
            status_text = level_display(permission_level)
            devotion_hint = ""
            if self._is_devotion_enabled() and permission_level < AIPermissionLevel.ADMIN:
                record = self.devotion_store.get_record(name=player_name)
                long_term = int(record.get("long_term", 1) or 1)
                short_term = int(record.get("short_term", 0) or 0)
                title = str(record.get("title") or "")
                devotion_hint = f"; [内部]近期={short_term}/{long_term}; 长期={long_term}"
                if title:
                    devotion_hint += f"; 称号={title}"
            final_content = (
                f"{player_name}(AI权限:{status_text}{devotion_hint}): {current_content}"
            )

        messages.append(
            {
                "role": "user",
                "content": final_content,
            }
        )

        return messages

    def _format_assistant_header(self) -> str:
        assistant_title = str(self.chat_config.get("assistant_title") or "弧光Agent")
        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")

        now = datetime.now()
        time_part = f"{now.year}.{now.month}.{now.day}-{now.hour}:{now.minute:02d}"
        return f"§u[{assistant_title}]§r{assistant_name}({time_part}):"

    @event_handler
    def on_player_chat(self, event: PlayerChatEvent) -> None:
        player = event.player
        message = event.message

        if player is None:
            return

        if not isinstance(message, str):
            return

        if not self._should_trigger_for_message(message):
            return

        if not self._has_chat_backend():
            assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
            assistant_tag = f"[{assistant_name}]"
            player.send_message(
                f"§c{assistant_tag} 尚未连接弧光消息中心，也未配置本机 AI 服务，请联系管理员。"
            )
            return

        player_name = player.name
        player_xuid = str(getattr(player, "xuid", "") or "").strip()
        user_content = self._strip_prefix(message)
        if not user_content:
            user_content = message.strip()

        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        assistant_tag = f"[{assistant_name}]"

        max_queue_size = int(self.chat_config.get("max_queue_size", 10))
        if max_queue_size < 1:
            max_queue_size = 1

        with self.queue_lock:
            queue_size = self.request_queue.qsize()
            if queue_size >= max_queue_size:
                player.send_message(f"§c{assistant_tag} 当前排队人数过多，请稍后再试。")
                return

            current_owner = self.current_request_owner
            position = queue_size + 1

        is_op = bool(getattr(player, "is_op", False))
        permission_level = self._resolve_permission_level(player=player)
        self.request_queue.put(
            {
                "type": "public",
                "owner_name": player_name,
                "player_xuid": player_xuid,
                "player_name": player_name,
                "user_content": user_content,
                "is_op": is_op,
                "permission_level": permission_level,
                "channel": "public",
            }
        )

        if current_owner:
            player.send_message(
                f"§e{assistant_tag} 服务正忙，正在处理 {current_owner} 的请求。"
                f"你的请求已加入队列（第 {position} 位）。"
            )
        else:
            player.send_message(f"§7{assistant_tag} 已收到请求，正在排队处理中（第 {position} 位）。")

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        player = event.player
        if player is None:
            return

        welcome_message = str(
            self.chat_config.get("welcome_message")
            or "欢迎来到弧光大陆服务器，我是服务器弧光Agent天星，喊我的名字天星就可以啦"
        ).strip()
        if not welcome_message:
            return

        header = self._format_assistant_header()
        player.send_message(f"{header}\n{welcome_message}")

    @event_handler
    def on_player_death(self, event: PlayerDeathEvent) -> None:
        player = event.player
        if player is None:
            return

        tip_message = str(
            self.chat_config.get("death_tip_message")
            or "遇到困难了吗？喊我的名字天星，我可以帮你传送或处理问题！"
        ).strip()
        if not tip_message:
            return

        header = self._format_assistant_header()
        player.send_message(f"{header}\n{tip_message}")

