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
from endstone.form import ModalForm, Label, TextInput, ActionForm

try:
    from endstone.command import CommandSenderWrapper
except ImportError:
    CommandSenderWrapper = None  # type: ignore

from .astrbot_hub_client import AstrBotHubChatClient
from .bound_self_help import validate_bound_self_help_command
from .chat_ai_manager import ChatAIManager


DEFAULT_PERSONA = (
    "你是Minecraft服务器中的AI助手“天星”，需要用友好、简洁的中文回答玩家的问题，"
    "并尽量结合游戏内的背景来解释。"
)

DEFAULT_SYSTEM_PROMPT = (
    "你运行在 Minecraft 基岩版服务器中，所有回复都会直接显示在游戏聊天栏。"
    "请使用 Minecraft 的颜色代码和格式代码来美化消息，而不要使用 Markdown 或其他标记语言。"
    "常用颜色代码示例: §0黑色, §1深蓝, §2深绿, §3深青, §4深红, §5深紫, §6金色, §7灰色, "
    "§8深灰, §9蓝色, §a绿色, §b青色, §c红色, §d淡紫, §e黄色, §f白色。"
    "常用格式代码示例: §l粗体, §n下划线, §o斜体, §k随机字符, §m删除线, §r重置格式。"
    "需要改游戏世界（劈闪电、给效果、传送、给予物品等）时，必须调用工具 mc_run_command，"
    "command 参数不要带开头斜杠。"
    "只有工具不可用时，才在回复里写 [execution_command:实际游戏指令]。"
    "effect 只能用于药水效果，例如: effect Steve slowness 20 0 true 或 "
    "effect Steve night_vision 30 0 true。"
    "劈闪电必须用: execute at 玩家名 run summon lightning_bolt ~ ~ ~"
    "禁止写成: effect 玩家名 summon ...（summon 不是药水效果，会报 Unknown effect）。"
    "也不要把 execute / summon / give / tp 塞进 effect 通道。"
    "对非 OP 玩家：仅当对方遇到困难或有正当理由时，才给短时间增益；惩罚性雷击/负面效果不要滥用。"
    "对 OP 玩家：对方明确要求且合理时可以执行。"
    "常见效果名称："
    "absorption, blindness, darkness, fire_resistance, haste, instant_damage, instant_health, "
    "invisibility, jump_boost, levitation, mining_fatigue, nausea, night_vision, poison, "
    "regeneration, resistance, slowness, slow_falling, speed, strength, water_breathing, "
    "weakness, wither。"
    "禁止 stop、kill。gamemode 仅 OP 明确要求。无法安全判断就不要执行指令。"
    "用户消息里会带玩家名和是否为 OP，请据此判断。"
)


class ARCAIHelperPlugin(Plugin):
    prefix = "ARCAIHelperPlugin"
    api_version = "0.10"
    load = "POSTWORLD"

    commands = {
        "ai": {
            "description": "打开与AI助手的聊天面板",
            "usages": ["/ai"],
            "permissions": ["arc_ai_helper.command.ai"],
        }
    }

    permissions = {
        "arc_ai_helper.command.ai": {
            "description": "允许使用AI助手聊天功能",
            "default": True,
        }
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

        self.player_histories: Dict[str, List[Dict[str, str]]] = {}
        self.public_history: List[Dict[str, str]] = []

        self.history_lock = threading.Lock()

        self.queue_lock = threading.Lock()
        self.current_request_owner: str = ""

        self.request_queue: queue.Queue = queue.Queue()
        self.worker_started: bool = False

        self._arc_core_newbie_guide_cache: str | None = None

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

    def run_ai_tool(self, action: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Execute an AstrBot MC control tool on the game server.

        Args:
            action: ``list`` / ``tps`` / ``info`` / ``cmd`` / ``jail`` / ``release`` / ``prisoners`` / ``skyeye_player`` / ``skyeye_combat`` / ``skyeye_location``.
            args: Extra arguments, including ``command`` / ``is_op``.

        Returns:
            JSON-serializable dict with ``ok`` and ``text`` or ``error``.
        """
        payload = args if isinstance(args, dict) else {}
        name = str(action or "").strip().lower()
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
                        bool(payload.get("is_op", False)),
                        str(payload.get("bound_player_name") or ""),
                        bool(payload.get("is_bound_self_help", False)),
                    )
                )
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
            return {"ok": True, "text": str(text or "").strip() or "（无返回）"}
        except Exception as error:
            self.logger.warning(f"[ARC AI Helper] AI 工具 {action} 失败: {error}")
            return {"ok": False, "error": str(error)}

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
        is_op: bool,
        bound_player_name: str = "",
        is_bound_self_help: bool = False,
    ) -> str:
        command_to_execute = html.unescape(str(command_to_execute or "").strip())
        if not command_to_execute:
            return "命令为空"

        normalized = command_to_execute.lstrip("/").strip()
        if not normalized:
            return "命令为空"
        command_name = normalized.lower().split(" ", 1)[0]
        if command_name in ("stop", "kill"):
            return f"已拦截危险指令: /{normalized}"
        if command_name == "gamemode" and (not is_op):
            return "该指令仅允许 OP 玩家要求时执行，已拦截。"
        if is_bound_self_help and (not is_op):
            ok, reason = validate_bound_self_help_command(normalized, bound_player_name)
            if not ok:
                return reason or "没有权限：该求助指令不被允许"

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
        return f"命令已执行: /{normalized}\n状态: {status}\n输出:\n{output_text}"

    def _get_prison_plugin(self):
        plugin_manager = getattr(self.server, "plugin_manager", None)
        if plugin_manager is None:
            return None
        try:
            return plugin_manager.get_plugin("arc_prison")
        except Exception:
            return None

    def _tool_jail_player(self, payload: Dict[str, Any]) -> str:
        if not bool(payload.get("is_op", False)):
            return "没有权限：入狱仅管理员（OP）或 QQ 群管理可以下令。"
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
        if not bool(payload.get("is_op", False)):
            return "没有权限：释放仅管理员（OP）或 QQ 群管理可以下令。"
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
        if not bool(payload.get("is_op", False)):
            return "没有权限：天眼查询仅管理员（OP）或 QQ 群管理可以使用。"
        return ""

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

    def _ensure_config_folder(self) -> None:
        os.makedirs(self.config_folder, exist_ok=True)

    def _ensure_default_files(self) -> None:
        if not os.path.exists(self.chat_config_path):
            default_chat_config = {
                "prefix_triggers": ["天星"],
                "contain_triggers": ["请问", "吗", "?", "？"],
                "max_history_messages": 20,
                "max_queue_size": 10,
                "assistant_title": "AI助手",
                "assistant_name": "弧光天星",
                "gui_greet_message": "你好，我是弧光天星服务器小助理，请问有什么可以帮助您的？",
                "welcome_message": "欢迎来到弧光大陆服务器，我是人工智能助手弧光天星，需要我的话喊我的名字天星就可以啦",
                "death_tip_message": "遇到困难了吗？有问题可以问我哦~喊我的名字天星我就来帮助你啦！",
                "hub_host": "127.0.0.1",
                "hub_port": 19136,
                "hub_token": "",
                "server_name": "",
                "astrbot_timeout": 180,
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
        data.setdefault("assistant_title", "AI助手")
        data.setdefault("assistant_name", "弧光天星")
        data.setdefault(
            "gui_greet_message",
            "你好，我是弧光天星服务器小助理，请问有什么可以帮助您的？",
        )
        data.setdefault(
            "welcome_message",
            "欢迎来到弧光大陆服务器，我是人工智能助手弧光天星，需要找我的话喊我的名字天星就可以啦",
        )
        data.setdefault("death_tip_message", "遇到困难了吗？有问题可以问我哦~喊我的名字天星我就来帮助你啦！")
        data.setdefault("hub_host", "127.0.0.1")
        data.setdefault("hub_port", 19136)
        data.setdefault("hub_token", "")
        data.setdefault("server_name", "")
        data.setdefault("astrbot_timeout", 180)

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

    def _append_history(self, player_name: str, role: str, content: str) -> None:
        with self.history_lock:
            self._append_history_unlocked(player_name, role, content)

    def _append_history_unlocked(self, player_name: str, role: str, content: str) -> None:
        if player_name not in self.player_histories:
            self.player_histories[player_name] = []

        self.player_histories[player_name].append(
            {
                "role": role,
                "content": content,
            }
        )

        max_history = int(self.chat_config.get("max_history_messages", 20))
        if max_history < 1:
            max_history = 1

        if len(self.player_histories[player_name]) > max_history:
            self.player_histories[player_name] = self.player_histories[player_name][
                -max_history:
            ]

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
                    is_op = bool(job.get("is_op", False))
                    channel = str(job.get("channel") or job_type or "public")

                    if job_type == "gui":
                        self._process_gui_job(player, player_name, user_content, is_op, channel)
                    elif job_type == "public":
                        self._process_public_job(player, player_name, user_content, is_op, channel)
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
        is_op: bool,
        channel: str,
        player=None,
    ) -> tuple[bool, str]:
        extra_system = self._build_capability_prompt()
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
                extra_system_prompt=extra_system,
                channel=channel,
                server_name=self.get_game_server_name(),
                timeout=self._astrbot_timeout(),
            )
        if not self.ai_manager.has_provider():
            return False, "未连接弧光消息中心，且未配置本机 AI Provider。"
        with self.history_lock:
            messages = self._build_messages_for_player(player_name, user_text, is_op)
        return self.ai_manager.chat(messages)

    def _process_gui_job(
        self, player, player_name: str, user_text: str, is_op: bool, channel: str = "gui"
    ) -> None:
        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        assistant_tag = f"[{assistant_name}]"

        if player is None:
            return

        with self.history_lock:
            self._append_history_unlocked(player_name, "user", user_text)

        success, reply = self._complete_chat(
            player_name, user_text, is_op, channel or "gui", player=player
        )
        if not success:
            player.send_message(f"§c{assistant_tag} AI对话失败: {reply}")
            self._open_ai_chat_panel(player)
            return

        reply_text = str(reply).strip()
        if reply_text:
            reply_text = self._handle_ai_reply_commands(reply_text, player)
            if reply_text:
                with self.history_lock:
                    self._append_history_unlocked(player_name, "assistant", reply_text)

        self._open_ai_chat_panel(player)

    def _process_public_job(
        self, player, player_name: str, user_content: str, is_op: bool, channel: str = "public"
    ) -> None:
        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        assistant_tag = f"[{assistant_name}]"

        if player is None:
            return

        with self.history_lock:
            self._append_public_history_unlocked(player_name, "user", user_content)

        success, reply = self._complete_chat(
            player_name, user_content, is_op, channel or "public", player=player
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

    def _build_capability_prompt(self) -> str:
        """Capability / policy prompt only. Persona is not included."""
        parts: List[str] = []
        base_prompt = (self.system_prompt or "").strip()
        if base_prompt:
            parts.append(base_prompt)
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
                "入狱和释放只有管理员（OP）下令时才能执行。"
            )
        if self._get_arc_core_plugin() is not None:
            parts.append(
                "本服已安装弧光核心天眼。查询玩家在哪、近期做了什么、打了谁、被谁打、"
                "某个坐标附近发生过什么、操作是否在领地内时，必须调用 "
                "mc_skyeye_player / mc_skyeye_combat / mc_skyeye_location，禁止编造。"
                "不要求该玩家当前在线。不知道在哪台服时 server 留空，中枢会搜索全部已连接服务器。"
                "调用天眼时必须自己把用户说的时长换算成分钟写入 minutes，例如一天=1440、一小时=60。"
                "这些工具只有管理员（OP）下令时才能执行。"
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
        blacklist = ["stop", "kill"]
        op_only_commands = ["gamemode"]

        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        assistant_tag = f"[{assistant_name}]"

        commands = re.findall(pattern, reply_text, flags=re.IGNORECASE)
        if not commands:
            return reply_text

        cleaned_text = re.sub(pattern, "", reply_text).strip()

        is_op = bool(getattr(sender, "is_op", False))

        for raw_command in commands:
            command_line = str(raw_command or "").strip()
            if not command_line:
                continue

            normalized_command_line = command_line.lstrip("/").strip()
            lower_command = normalized_command_line.lower()
            command_name = lower_command.split(" ", 1)[0] if lower_command else ""

            if command_name in blacklist:
                if sender is not None:
                    sender.send_message(f"§c{assistant_tag} 尝试执行被禁止的危险指令，已拦截。")
                continue

            if (command_name in op_only_commands) and (not is_op):
                if sender is not None:
                    sender.send_message(f"§c{assistant_tag} 该指令仅允许 OP 使用，已拦截。")
                continue

            try:
                self.server.dispatch_command(self.server.command_sender, normalized_command_line)
            except Exception as error:
                if sender is not None:
                    sender.send_message(f"§c{assistant_tag} 执行指令失败: {error}")

        return cleaned_text

    def _build_messages_for_player(self, player_name: str, current_content: str, is_op: bool | None = None) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []

        system_text = self._build_system_prompt()
        if system_text:
            messages.append(
                {
                    "role": "system",
                    "content": system_text,
                }
            )

        history = self.player_histories.get(player_name) or []
        for item in history:
            role = item.get("role")
            content = str(item.get("content") or "").strip()
            if not content:
                continue

            if role == "user":
                speaker_name = player_name
            elif role == "assistant":
                speaker_name = str(self.chat_config.get("assistant_name") or "弧光天星")
            else:
                speaker_name = ""

            prefix = f"{speaker_name}: " if speaker_name else ""
            messages.append(
                {
                    "role": role or "user",
                    "content": prefix + content,
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

        if is_op is None:
            final_content = f"{player_name}: {current_content}"
        else:
            status_text = "OP玩家" if is_op else "普通玩家"
            final_content = f"{player_name}({status_text}): {current_content}"

        messages.append(
            {
                "role": "user",
                "content": final_content,
            }
        )

        return messages

    def on_command(self, sender, command, args: list[str]) -> bool:
        if command.name == "ai":
            if not hasattr(sender, "send_form"):
                sender.send_message("该命令只能在游戏内由玩家使用。")
                return True
            self._open_ai_chat_panel(sender)
            return True
        return False

    def _format_assistant_header(self) -> str:
        assistant_title = str(self.chat_config.get("assistant_title") or "AI助手")
        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")

        now = datetime.now()
        time_part = f"{now.year}.{now.month}.{now.day}-{now.hour}:{now.minute:02d}"
        return f"§u[{assistant_title}]§r{assistant_name}({time_part}):"

    def _build_gui_chat_text(self, player_name: str) -> str:
        history = self.player_histories.get(player_name) or []
        if not history:
            greet = str(
                self.chat_config.get("gui_greet_message")
                or "你好，我是弧光天星服务器小助理，请问有什么可以帮助您的？"
            )
            return greet

        lines: List[str] = []
        for item in history:
            role = item.get("role")
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                lines.append(f"§7你:§r {content}")
            elif role == "assistant":
                lines.append(f"{self._format_assistant_header()}\n{content}")
            else:
                lines.append(content)
        return "\n\n".join(lines) if lines else ""

    def _open_ai_chat_panel(self, player) -> None:
        player_name = player.name
        chat_history_text = self._build_gui_chat_text(player_name)

        history_label = Label(text=chat_history_text)
        input_box = TextInput(
            label="输入要发送给AI助手的内容：",
            placeholder="在这里输入你的问题或想说的话",
            default_value="",
        )

        def handle_submit(sender, data):
            if not isinstance(data, (list, tuple)) or len(data) < 2:
                sender.send_message("表单数据异常，请重试。")
                return

            user_text = str(data[1] or "").strip()
            if not user_text:
                self._open_ai_chat_panel(sender)
                return

            assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
            assistant_tag = f"[{assistant_name}]"

            max_queue_size = int(self.chat_config.get("max_queue_size", 10))
            if max_queue_size < 1:
                max_queue_size = 1

            with self.queue_lock:
                queue_size = self.request_queue.qsize()
                if queue_size >= max_queue_size:
                    sender.send_message(f"§c{assistant_tag} 当前排队人数过多，请稍后再试。")
                    return

                current_owner = self.current_request_owner
                position = queue_size + 1

            is_op = bool(getattr(sender, "is_op", False))
            self.request_queue.put(
                {
                    "type": "gui",
                    "owner_name": sender.name,
                    "player": sender,
                    "player_name": sender.name,
                    "user_content": user_text,
                    "is_op": is_op,
                    "channel": "gui",
                }
            )

            if current_owner:
                sender.send_message(
                    f"§e{assistant_tag} 服务正忙，正在处理 {current_owner} 的请求。"
                    f"你的请求已加入队列（第 {position} 位）。"
                )
            else:
                sender.send_message(f"§7{assistant_tag} 已收到请求，正在排队处理中（第 {position} 位）。")

        form = ModalForm(
            title="与AI助手聊天",
            controls=[history_label, input_box],
            on_submit=handle_submit,
            on_close=lambda s: None,
        )

        player.send_form(form)

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
        self.request_queue.put(
            {
                "type": "public",
                "owner_name": player_name,
                "player": player,
                "player_name": player_name,
                    "user_content": user_content,
                    "is_op": is_op,
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
            or "欢迎来到弧光大陆服务器，我是人工智能助手弧光天星，需要找我的话喊我的名字天星就可以啦"
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
            or "遇到困难了吗？有问题可以问我哦~喊我的名字天星我就来帮助你啦！"
        ).strip()
        if not tip_message:
            return

        header = self._format_assistant_header()
        player.send_message(f"{header}\n{tip_message}")

