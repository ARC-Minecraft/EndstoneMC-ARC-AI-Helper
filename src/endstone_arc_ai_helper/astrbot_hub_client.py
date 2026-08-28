"""Lightweight Hub client for routing MC AI chat through AstrBot."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from typing import Any, Optional, Tuple


def _import_websockets():
    try:
        import websockets  # type: ignore

        return websockets
    except ImportError:
        pass
    try:
        from endstone_arc_qq_sync_astrbot.utils.imports import import_websockets

        return import_websockets()
    except Exception as error:
        raise ImportError(
            "无法导入 websockets。请安装依赖 websockets，或确保已安装 ARC QQ Sync 插件。"
        ) from error


class AstrBotHubChatClient:
    """Connects to 弧光消息中心 as role=ai_helper and runs ai_chat RPC."""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.logger = plugin.logger
        self.ws = None
        self._running = False
        self._ai_chat_enabled = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = threading.Lock()

    def is_ready(self) -> bool:
        return bool(self.ws) and self._ai_chat_enabled

    def is_connecting(self) -> bool:
        return bool(self._running)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._connect_forever(), self._loop)

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        if loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._close_ws(), loop).result(timeout=3)
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        self._fail_pending(ConnectionError("AI Hub 客户端已停止"))
        self.ws = None
        self._ai_chat_enabled = False

    def chat(
        self,
        *,
        player_name: str,
        player_xuid: str,
        content: str,
        is_op: bool,
        permission_level=None,
        extra_system_prompt: str,
        channel: str,
        server_name: str,
        timeout: float,
    ) -> Tuple[bool, str]:
        if not self.is_ready() or self._loop is None:
            return False, "未连接弧光消息中心或不支持 AstrBot 对话"

        future = asyncio.run_coroutine_threadsafe(
            self._chat(
                player_name=player_name,
                player_xuid=player_xuid,
                content=content,
                is_op=is_op,
                permission_level=permission_level,
                extra_system_prompt=extra_system_prompt,
                channel=channel,
                server_name=server_name,
                timeout=timeout,
            ),
            self._loop,
        )
        try:
            return future.result(timeout=max(5.0, float(timeout) + 5.0))
        except Exception as error:
            return False, f"AstrBot 对话失败: {error}"

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _close_ws(self) -> None:
        ws = self.ws
        self.ws = None
        self._ai_chat_enabled = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def _resolve_hub_config(self) -> tuple[str, int, str, str]:
        cfg = self.plugin.chat_config or {}
        host = str(cfg.get("hub_host") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(cfg.get("hub_port", 19136))
        except Exception:
            port = 19136
        token = str(cfg.get("hub_token") or "")
        server_name = self.plugin.get_game_server_name()
        return host, port, token, f"{server_name}#ai-helper"

    async def _connect_forever(self) -> None:
        websockets = _import_websockets()
        delay = 2.0
        while self._running:
            host, port, token, client_name = self._resolve_hub_config()
            uri = f"ws://{host}:{port}"
            try:
                self.logger.info(
                    f"[ARC AI Helper] 正在连接弧光消息中心 {uri}（身份 {client_name}）..."
                )
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10,
                ) as ws:
                    self.ws = ws
                    register_msg: dict[str, Any] = {
                        "type": "register",
                        "server_name": client_name,
                        "role": "ai_helper",
                    }
                    if token:
                        register_msg["token"] = token
                    await ws.send(json.dumps(register_msg, ensure_ascii=False))

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        welcome = json.loads(raw)
                    except Exception as error:
                        self.logger.warning(
                            f"[ARC AI Helper] 等待 Hub 欢迎消息失败: {error}"
                        )
                        await ws.close()
                        raise ConnectionError("未收到 Hub 欢迎消息") from error

                    if welcome.get("type") != "hub_welcome":
                        raise ConnectionError(f"非预期欢迎消息: {welcome.get('type')}")

                    features = welcome.get("features") or []
                    self._ai_chat_enabled = bool(
                        welcome.get("ai_chat") or ("ai_chat" in features)
                    )
                    if not self._ai_chat_enabled:
                        self.logger.warning(
                            "[ARC AI Helper] 已连上中枢，但当前版本不支持 ai_chat；"
                            "请升级 AstrBot 弧光消息中枢后重试。暂时回退本机人格。"
                        )
                        await ws.close()
                        await asyncio.sleep(30)
                        continue

                    delay = 2.0
                    self.logger.info(
                        "[ARC AI Helper] 已连接弧光消息中心，对话走 AstrBot 人格/记忆"
                    )
                    await self._message_loop(ws)
                    self._fail_pending(ConnectionError("Hub 连接已断开"))
                    if not self._running:
                        break
                    self.logger.warning(
                        f"[ARC AI Helper] 弧光消息中心连接结束，{delay:.0f}s 后重连"
                    )
                    await asyncio.sleep(delay)
                    delay = min(30.0, delay * 1.5)
            except Exception as error:
                self.ws = None
                self._ai_chat_enabled = False
                self._fail_pending(ConnectionError("Hub 连接已断开"))
                if not self._running:
                    break
                self.logger.warning(
                    f"[ARC AI Helper] 弧光消息中心不可用: {error}，{delay:.0f}s 后重试；"
                    "此期间使用本机人格与 providers.json"
                )
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 1.5)
            finally:
                self.ws = None
                self._ai_chat_enabled = False

    async def _message_loop(self, ws) -> None:
        websockets = _import_websockets()
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                msg_type = data.get("type")
                if msg_type == "ai_chat_response":
                    request_id = str(data.get("request_id") or "")
                    fut = self._pending.get(request_id)
                    if fut and not fut.done():
                        fut.set_result(data)
                elif msg_type == "ai_tool":
                    asyncio.create_task(self._handle_ai_tool(data))
                elif msg_type == "pong":
                    pass
                elif msg_type == "hub_welcome":
                    features = data.get("features") or []
                    self._ai_chat_enabled = bool(
                        data.get("ai_chat") or ("ai_chat" in features)
                    )
        except websockets.exceptions.ConnectionClosed:
            self.logger.warning("[ARC AI Helper] 弧光消息中心连接已断开")

    async def _handle_ai_tool(self, data: dict[str, Any]) -> None:
        """Run a Hub-requested MC tool on the plugin and reply.

        Args:
            data: Incoming ``ai_tool`` payload.
        """
        request_id = data.get("request_id")
        action = str(data.get("action") or "")
        raw_args = data.get("args") if isinstance(data.get("args"), dict) else {}
        args = dict(raw_args)
        # Hub 侧认证的玩家名优先；禁止模型在 args 里伪造 caller / 权限。
        hub_player = (
            str(data.get("player_name") or "").strip()
            or str(data.get("caller_player_name") or "").strip()
            or str(args.get("caller_player_name") or "").strip()
        )
        if hub_player:
            args["caller_player_name"] = hub_player
        args.pop("permission_level", None)
        args.pop("is_op", None)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None, lambda: self.plugin.run_ai_tool(action, args)
            )
            if not isinstance(result, dict):
                result = {"ok": False, "error": "工具返回格式异常"}
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        ws = self.ws
        if ws is None:
            return
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "ai_tool_response",
                        "request_id": request_id,
                        "ok": bool(result.get("ok")),
                        "text": result.get("text") or "",
                        "error": result.get("error") or "",
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as error:
            self.logger.warning(f"[ARC AI Helper] 回传 AI 工具结果失败: {error}")

    async def _chat(
        self,
        *,
        player_name: str,
        player_xuid: str,
        content: str,
        is_op: bool,
        permission_level=None,
        extra_system_prompt: str,
        channel: str,
        server_name: str,
        timeout: float,
    ) -> Tuple[bool, str]:
        if not self.ws or not self._ai_chat_enabled:
            return False, "未连接弧光消息中心或不支持 AstrBot 对话"

        request_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        level_value = permission_level
        try:
            from .ai_permission import AIPermissionLevel, level_display

            if isinstance(level_value, AIPermissionLevel):
                level_name = level_display(level_value)
            elif level_value is not None:
                level_name = level_display(AIPermissionLevel(int(level_value)))
            else:
                level_name = "assistant"
        except Exception:
            level_name = str(level_value or "assistant")
        payload = {
            "type": "ai_chat",
            "request_id": request_id,
            "player_name": player_name,
            "player_xuid": player_xuid,
            "content": content,
            "is_op": bool(is_op),
            "permission_level": level_name,
            "extra_system_prompt": extra_system_prompt,
            "channel": channel,
            "server_name": server_name,
        }
        try:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))
            data = await asyncio.wait_for(fut, timeout=max(5.0, float(timeout)))
        except asyncio.TimeoutError:
            return False, "AstrBot 对话超时"
        except Exception as error:
            return False, f"AstrBot 对话失败: {error}"
        finally:
            self._pending.pop(request_id, None)

        if not data.get("ok"):
            return False, str(data.get("error") or "AstrBot 对话失败")
        reply = str(data.get("reply") or "").strip()
        if not reply:
            return False, "AstrBot 未返回文本"
        return True, reply
