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
from .local_agent_tools import build_local_agent_tools, resolve_tool_action

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
        self._arc_core_landmarks_cache_until: float = 0.0

    def on_enable(self) -> None:
        self.logger.info("[ARC AI Helper] on_enable called")
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

    def _resolve_permission_level(
        self,
        player=None,
        payload: Dict[str, Any] | None = None,
    ) -> AIPermissionLevel:
        """Resolve AI permission level from player, config, and Hub payload."""
        data = payload if isinstance(payload, dict) else {}
        op_maps = bool(self.chat_config.get("op_maps_to_admin", True))
        return resolve_permission_level(
            player=player,
            chat_config=self.chat_config,
            payload_level=data.get("permission_level"),
            payload_is_op=bool(data.get("is_op", False)),
            op_maps_to_admin=op_maps,
        )

    def run_ai_tool(self, action: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Execute an AstrBot MC control tool on the game server.

        Args:
            action: ``list`` / ``tps`` / ``info`` / ``cmd`` / ``jail`` / ``release`` / ``prisoners`` /
                ``skyeye_*`` / ``economy`` / ``land`` / ``arc_tp``.
            args: Extra arguments, including ``command`` / ``is_op`` / ``permission_level``.

        Returns:
            JSON-serializable dict with ``ok`` and ``text`` or ``error``.
        """
        payload = args if isinstance(args, dict) else {}
        name = str(action or "").strip().lower()
        if name.startswith("mc_"):
            name = resolve_tool_action(name)
        try:
            if name == "list":
                text = self._run_on_server_thread(self._tool_list_players)
            elif name == "tps":
                text = self._run_on_server_thread(self._tool_get_tps)
            elif name == "info":
                text = self._run_on_server_thread(self._tool_server_info)
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
            elif name in ("skyeye_location", "sky_eye_location"):
                text = self._run_on_server_thread(lambda: self._tool_skyeye_location(payload))
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
        if name == "cmd":
            cmd = str(payload.get("command") or "").strip()
            if cmd:
                bits.append(f"cmd=/{cmd.lstrip('/')}")
        else:
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
        self._sky_eye_log_agent(detail=detail, target_name=caller)

    def _sky_eye_log_agent(self, *, detail: str, target_name: str = "") -> None:
        core = self._get_arc_core_plugin()
        if core is None:
            return
        logger_api = getattr(core, "api_sky_eye_log", None)
        if not callable(logger_api):
            return
        agent_name = str(self.chat_config.get("assistant_name") or "弧光天星").strip()
        try:
            logger_api(
                "AiAgent",
                player_name=agent_name or "弧光天星",
                player_xuid="",
                detail=str(detail or "")[:500],
                target_name=str(target_name or "").strip(),
                target_type="player" if target_name else "",
            )
        except Exception as error:
            self.logger.debug(f"[ARC AI Helper] 天眼留档失败: {error}")

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
            self._sky_eye_log_agent(
                detail=(
                    f"tool=cmd; level={level_display(level)}; "
                    f"cmd=/{normalized}; status=denied; error={deny[:120]}"
                ),
                target_name=caller,
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
        self._sky_eye_log_agent(
            detail=(
                f"tool=cmd; level={level_display(level)}; "
                f"cmd=/{normalized}; status={'ok' if success else 'fail'}"
            ),
            target_name=caller,
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
            return "玩家名为空"
        minutes = self._parse_skyeye_minutes(payload, 30)
        action = str(payload.get("action") or "").strip()
        parts: List[str] = []
        if callable(player_now):
            now_info = player_now(player_name=player_name)
            if isinstance(now_info, dict) and now_info.get("source"):
                name = now_info.get("player_name") or player_name
                if now_info.get("online") and now_info.get("x") is not None:
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
                    heading=f"{player_name} 近 {minutes} 分钟行为",
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
        if not player_name:
            return "玩家名为空"
        minutes = self._parse_skyeye_minutes(payload, 30)
        return query_text(
            player_name=player_name,
            minutes=minutes,
            combat_role="both",
            heading=f"{player_name} 近 {minutes} 分钟战斗（打了谁 / 被谁打 / 死亡）",
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
        return query_text(
            x=x,
            y=y,
            z=z,
            radius=radius,
            dimension=dimension,
            minutes=minutes,
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
                    player = job.get("player")
                    player_name = str(job.get("player_name") or "")
                    user_content = str(job.get("user_content") or "")
                    permission_level = job.get("permission_level")
                    is_op = bool(job.get("is_op", False))
                    channel = str(job.get("channel") or job_type or "public")

                    if job_type == "public":
                        self._process_public_job(
                            player, player_name, user_content, permission_level, is_op, channel
                        )
                except Exception as error:
                    try:
                        player = job.get("player")
                        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
                        player.send_message(f"§c[{assistant_name}] 处理请求出错: {error}")
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
                player_xuid=self._player_xuid(player),
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
        )
        level_value = int(level)
        is_admin = level >= AIPermissionLevel.ADMIN

        def _execute_local_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
            args = dict(tool_args or {})
            args.setdefault("is_op", is_admin)
            args.setdefault("permission_level", level_value)
            if player is not None:
                args.setdefault("caller_player_name", str(getattr(player, "name", "") or ""))
                args.setdefault("caller_xuid", self._player_xuid(player))
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
        self, player, player_name: str, user_content: str, permission_level: AIPermissionLevel | int | None, is_op: bool, channel: str = "public"
    ) -> None:
        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        assistant_tag = f"[{assistant_name}]"

        if player is None:
            return

        with self.history_lock:
            self._append_public_history_unlocked(player_name, "user", user_content)

        success, reply = self._complete_chat(
            player_name,
            user_content,
            permission_level,
            is_op,
            channel or "public",
            player=player,
        )
        if not success:
            player.send_message(f"§c{assistant_tag} 对话失败: {reply}")
            return

        reply_text = str(reply).strip()
        if not reply_text:
            return

        reply_text = self._handle_ai_reply_commands(reply_text, player)
        if not reply_text:
            return

        with self.history_lock:
            self._append_public_history_unlocked(assistant_name, "assistant", reply_text)

        header = self._format_assistant_header()
        self.server.broadcast_message(f"{header}\n{reply_text}")
        level = permission_level
        if not isinstance(level, AIPermissionLevel):
            level = self._resolve_permission_level(player=player)
        preview = reply_text.replace("\n", " ")[:200]
        self._sky_eye_log_agent(
            detail=(
                f"tool=reply; channel={channel or 'public'}; "
                f"level={level_display(level)}; text={preview}"
            ),
            target_name=player_name,
        )

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

    def _build_capability_prompt(self) -> str:
        """Capability / policy prompt only. Persona is not included."""
        parts: List[str] = []
        base_prompt = (self.system_prompt or "").strip()
        if base_prompt:
            parts.append(base_prompt)
        parts.append(
            "【弧光Agent】无论是否连接 AstrBot，都应优先通过工具完成查服、执行指令、"
            "银行/领地/传送/天眼/监狱等操作；不要编造工具本可查询的数据。"
        )
        newbie_guide_text = self._get_arc_core_newbie_guide_text()
        if newbie_guide_text:
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
                "必须调用 mc_skyeye_player / mc_skyeye_combat / mc_skyeye_location，禁止编造。"
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
                self._sky_eye_log_agent(
                    detail=(
                        f"tool=execution_command; level={level_display(level)}; "
                        f"cmd=/{normalized_command_line}; status=denied; "
                        f"error={(reason or '拦截')[:120]}"
                    ),
                    target_name=str(getattr(sender, "name", "") or ""),
                )
                continue

            try:
                self.server.dispatch_command(self.server.command_sender, normalized_command_line)
                self._sky_eye_log_agent(
                    detail=(
                        f"tool=execution_command; level={level_display(level)}; "
                        f"cmd=/{normalized_command_line}; status=ok"
                    ),
                    target_name=str(getattr(sender, "name", "") or ""),
                )
            except Exception as error:
                if sender is not None:
                    sender.send_message(f"§c{assistant_tag} 执行指令失败: {error}")
                self._sky_eye_log_agent(
                    detail=(
                        f"tool=execution_command; level={level_display(level)}; "
                        f"cmd=/{normalized_command_line}; status=fail; "
                        f"error={str(error)[:120]}"
                    ),
                    target_name=str(getattr(sender, "name", "") or ""),
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
            final_content = f"{player_name}(AI权限:{status_text}): {current_content}"

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
                "player": player,
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

