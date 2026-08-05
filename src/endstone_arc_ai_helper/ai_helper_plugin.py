import json
import os
import queue
import re
import threading
from typing import Any, Dict, List
from datetime import datetime

from endstone.event import PlayerChatEvent, PlayerJoinEvent, PlayerDeathEvent, event_handler
from endstone.plugin import Plugin
from endstone.form import ModalForm, Label, TextInput, ActionForm

from .chat_ai_manager import ChatAIManager


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

        self._ensure_config_folder()
        self._ensure_default_files()

        self.chat_config: Dict[str, Any] = self._load_chat_config()
        self.system_prompt = self._load_system_prompt()
        self.ai_manager = ChatAIManager(self.providers_config_path)

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

        self._start_worker_if_needed()

        if not self.ai_manager.has_provider():
            self.logger.warning(
                "[ARC AI Helper] 未找到有效的Provider配置，请编辑 providers.json 后重载插件。"
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
            }
            with open(self.chat_config_path, "w", encoding="utf-8") as file:
                json.dump(default_chat_config, file, ensure_ascii=False, indent=2)

        if not os.path.exists(self.system_prompt_path):
            default_prompt = (
                "你是Minecraft服务器中的AI助手“天星”，需要用友好、简洁的中文回答玩家的问题，"
                "并尽量结合游戏内的背景来解释。"
            )
            with open(self.system_prompt_path, "w", encoding="utf-8") as file:
                file.write(default_prompt)

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
        data.setdefault(
            "death_tip_message",
            "遇到困难了吗？有问题可以问我哦~喊我的名字天星我就来帮助你啦！",
        )

        try:
            max_history = int(data.get("max_history_messages", 20))
        except Exception:
            max_history = 20
        data["max_history_messages"] = max(1, max_history)

        return data

    def _load_system_prompt(self) -> str:
        try:
            with open(self.system_prompt_path, "r", encoding="utf-8") as file:
                return file.read().strip()
        except Exception as error:
            self.logger.error(f"[ARC AI Helper] 加载 system_prompt.txt 失败: {error}")
            return (
                "你是Minecraft服务器中的AI助手“天星”，需要用友好、简洁的中文回答玩家的问题。"
            )

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

                    if job_type == "gui":
                        self._process_gui_job(player, player_name, user_content, is_op)
                    elif job_type == "public":
                        self._process_public_job(player, player_name, user_content, is_op)
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

    def _process_gui_job(self, player, player_name: str, user_text: str, is_op: bool) -> None:
        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        assistant_tag = f"[{assistant_name}]"

        if player is None:
            return

        with self.history_lock:
            self._append_history_unlocked(player_name, "user", user_text)
            messages = self._build_messages_for_player(player_name, user_text, is_op)

        success, reply = self.ai_manager.chat(messages)
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

    def _process_public_job(self, player, player_name: str, user_content: str, is_op: bool) -> None:
        assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
        assistant_tag = f"[{assistant_name}]"

        if player is None:
            return

        with self.history_lock:
            self._append_public_history_unlocked(player_name, "user", user_content)
            messages = self._build_messages_for_player(player_name, user_content, is_op)

        success, reply = self.ai_manager.chat(messages)
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

    def _build_system_prompt(self) -> str:
        base_prompt = self.system_prompt or ""
        newbie_guide_text = self._get_arc_core_newbie_guide_text()
        command_prompt = (
            "你是运行在 Minecraft 服务器中的聊天助手“天星”，你的所有回复都会直接显示在游戏聊天栏中。"
            "请使用 Minecraft 的颜色代码和格式代码来美化消息，而不要使用 Markdown 或其他标记语言。"
            "常用颜色代码示例: §0黑色, §1深蓝, §2深绿, §3深青, §4深红, §5深紫, §6金色, §7灰色, "
            "§8深灰, §9蓝色, §a绿色, §b青色, §c红色, §d淡紫, §e黄色, §f白色。"
            "常用格式代码示例: §l粗体, §n下划线, §o斜体, §k随机字符, §m删除线, §r重置格式。"
            "你可以在需要时通过在回复中加入形如 [execution_command:实际游戏指令] 的标记，让服务器帮你执行指令，"
            "例如: [execution_command:effect DEVILENMO night_vision 1 10]。"
            "你被允许使用 /effect <player> <effect> [seconds] [amplifier] [hideParticles] 来为玩家增加效果。"
            "对非 OP 玩家：仅当玩家遇到困难或有正当理由时，才考虑给予短时间的增益（例如 10~60 秒、较低等级）。"
            "不要滥用负面效果或高强度效果，也不要用效果来破坏游戏平衡。"
            "对 OP 玩家：如果 OP 明确要求你执行某个合理的 /effect 命令，你可以按 OP 的要求执行。"
            "常见效果（effect 名称 -> 含义）示例："
            "absorption(额外生命), bad_omen(触发袭击), blindness(致盲), breath_of_the_nautilus(暂停耗氧), conduit_power(潮涌能量), "
            "darkness(黑暗), fatal_poison(致命中毒), fire_resistance(抗火), haste(急迫), health_boost(生命提升), hunger(饥饿), "
            "infested(虫蚀), instant_damage(瞬间伤害), instant_health(瞬间治疗), invisibility(隐身), jump_boost(跳跃提升), "
            "levitation(漂浮), mining_fatigue(挖掘疲劳), nausea(反胃), night_vision(夜视), oozing(渗浆), poison(中毒), "
            "raid_omen(袭击预兆), regeneration(生命恢复), resistance(抗性), saturation(饱和), slow_falling(缓降), slowness(缓慢), "
            "speed(速度), strength(力量), trial_omen(试炼预兆), village_hero(村庄英雄), water_breathing(水下呼吸), weakness(虚弱), "
            "weaving(织网), wind_charged(风爆), wither(凋零)。"
            "只有当确实需要执行游戏内命令时才这样做，并且要结合玩家是否为 OP 以及请求是否合理进行判断："
            "如果玩家不是 OP，或者请求的行为明显不合理/具有破坏性，你应该拒绝执行命令并给予解释。"
            "以下指令或包含这些片段的指令一律禁止执行，例如: kill @e 等具有破坏性的指令。"
            "坚决不允许执行的指令包括但不限于：stop（关服）、kill（击杀实体相关命令全部禁止）。"
            "gamemode 仅允许在 OP 玩家明确要求且合理时执行，非 OP 一律禁止。"
            "如果你无法安全判断，就不要生成 execution_command 标记。"
            "在你收到的用户消息中，会包含玩家名称以及是否为 OP 的信息，你需要据此谨慎决策。"
        )

        parts: List[str] = []
        if base_prompt:
            parts.append(base_prompt)
        if newbie_guide_text:
            parts.append(
                "【新手引导（来自 arc_core 的 newbie_welcome.txt）】\n" + newbie_guide_text
            )
        parts.append(command_prompt)
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

        if not self.ai_manager.has_provider():
            assistant_name = str(self.chat_config.get("assistant_name") or "弧光天星")
            assistant_tag = f"[{assistant_name}]"
            player.send_message(f"§c{assistant_tag} 尚未配置AI服务，请联系管理员。")
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

