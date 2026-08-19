import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from requests.exceptions import Timeout as RequestsTimeout


class ChatAIManager:
    def __init__(self, providers_config_path: str):
        self.providers_config_path = providers_config_path
        self.providers: List[Dict[str, Any]] = []
        self.current_provider_index = 0
        self.current_key_index = 0
        self.current_model_index = 0
        self.session = requests.Session()

        self._load_providers_config()

    def _load_providers_config(self) -> None:
        if not os.path.exists(self.providers_config_path):
            self.providers = []
            return

        try:
            with open(self.providers_config_path, "r", encoding="utf-8") as file:
                data = json.load(file)

            if isinstance(data, list):
                self.providers = data
            else:
                self.providers = []
        except Exception:
            self.providers = []

    def has_provider(self) -> bool:
        return len(self.providers) > 0

    def _get_next_target(self) -> Optional[Tuple[Dict[str, Any], str, str]]:
        if not self.providers:
            return None

        provider_count = len(self.providers)
        for _ in range(provider_count):
            provider = self.providers[self.current_provider_index]
            api_keys = provider.get("api_keys") or []
            models = provider.get("models") or []
            base_url = str(provider.get("base_url") or "").rstrip("/")

            if api_keys and models and base_url:
                api_key = api_keys[self.current_key_index % len(api_keys)]
                model = models[self.current_model_index % len(models)]

                self.current_key_index = (self.current_key_index + 1) % len(api_keys)
                self.current_model_index = (self.current_model_index + 1) % len(models)

                return provider, api_key, model

            self.current_provider_index = (self.current_provider_index + 1) % provider_count

        return None

    def _count_valid_targets(self) -> int:
        total = 0
        for provider in self.providers:
            api_keys = provider.get("api_keys") or []
            models = provider.get("models") or []
            base_url = str(provider.get("base_url") or "").strip()
            if api_keys and models and base_url:
                total += len(api_keys) * len(models)
        return total

    def _max_timeout_retry_rounds(self) -> int:
        count = self._count_valid_targets()
        if count <= 0:
            return 1
        return min(20, max(count, 3))

    def _should_retry_after_http_error(self, status_code: int) -> bool:
        return status_code in (408, 429, 502, 503, 504)

    def _build_proxies(self, provider: Dict[str, Any]) -> Optional[Dict[str, str]]:
        proxy = provider.get("proxy", "127.0.0.1:7890")
        if proxy is None or proxy is False:
            return None

        proxy_text = str(proxy).strip()
        if not proxy_text or proxy_text.lower() in ("false", "none", "null", "off", "0"):
            return None

        if "://" not in proxy_text:
            proxy_text = f"http://{proxy_text}"

        return {"http": proxy_text, "https": proxy_text}

    def _post_chat_completions(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] | None = None,
        stick_to: Optional[Tuple[Dict[str, Any], str, str]] = None,
    ) -> Tuple[bool, Any]:
        """POST /chat/completions. Returns (ok, message_dict_or_error_str)."""
        max_rounds = self._max_timeout_retry_rounds()
        last_error = ""
        fixed = stick_to

        for round_index in range(max_rounds):
            if fixed is not None:
                provider, api_key, model = fixed
            else:
                target = self._get_next_target()
                if target is None:
                    return False, "未配置可用的AI Provider，请检查providers.json。"
                provider, api_key, model = target
                fixed = target

            base_url = str(provider.get("base_url") or "").rstrip("/")
            timeout = int(provider.get("timeout", 60))
            proxies = self._build_proxies(provider)
            url = f"{base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            body: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"

            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=timeout,
                    proxies=proxies,
                )
            except RequestsTimeout as error:
                last_error = f"请求AI服务超时: {error}"
                fixed = None
                if round_index + 1 >= max_rounds:
                    return False, last_error
                continue
            except requests.RequestException as error:
                return False, f"请求AI服务失败: {error}"

            if response.status_code != 200:
                try:
                    error_data = response.json()
                    error_message = (
                        error_data.get("error", {}).get("message") or response.text
                    )
                except Exception:
                    error_message = response.text

                # Some providers reject tools; caller may retry without them.
                if tools and response.status_code in (400, 404, 422):
                    return False, f"TOOL_UNSUPPORTED:{error_message}"

                if self._should_retry_after_http_error(
                    response.status_code
                ) and round_index + 1 < max_rounds:
                    last_error = (
                        f"AI服务暂时不可用({response.status_code})，已切换模型重试。"
                        f" 详情: {error_message}"
                    )
                    fixed = None
                    continue

                return False, f"AI服务返回错误: {error_message}"

            try:
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    return False, "AI服务未返回结果。"
                message = choices[0].get("message") or {}
                return True, message
            except Exception as error:
                return False, f"解析AI响应失败: {error}"

        if last_error:
            return False, last_error
        return False, "AI服务多次超时或不可用，请稍后再试或检查providers.json中的模型列表。"

    def chat(self, messages: List[Dict[str, str]]) -> Tuple[bool, str]:
        ok, payload = self._post_chat_completions(messages=list(messages), tools=None)
        if not ok:
            return False, str(payload)
        content = (payload or {}).get("content") or ""
        return True, str(content)

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        execute_tool: Callable[[str, Dict[str, Any]], str],
        *,
        max_tool_rounds: int = 8,
    ) -> Tuple[bool, str]:
        """Agent loop: call model → run tool_calls → feed results → until text reply.

        Args:
            messages: OpenAI-style message list (may grow with tool turns).
            tools: OpenAI tools definitions.
            execute_tool: ``(tool_name, args) -> result_text``.
            max_tool_rounds: Cap on tool-call iterations.

        Returns:
            ``(ok, reply_text)``.
        """
        working = [dict(item) for item in messages]
        tool_list = list(tools or [])
        use_tools = bool(tool_list)

        for _ in range(max(1, int(max_tool_rounds))):
            ok, payload = self._post_chat_completions(
                messages=working,
                tools=tool_list if use_tools else None,
            )
            if not ok:
                err = str(payload)
                if use_tools and err.startswith("TOOL_UNSUPPORTED:"):
                    use_tools = False
                    ok, payload = self._post_chat_completions(
                        messages=working,
                        tools=None,
                    )
                    if not ok:
                        return False, str(payload)
                else:
                    return False, err

            message = payload if isinstance(payload, dict) else {}
            tool_calls = message.get("tool_calls") or []
            content = message.get("content")

            if tool_calls and use_tools:
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": content if content is not None else "",
                    "tool_calls": tool_calls,
                }
                working.append(assistant_msg)
                for call in tool_calls:
                    call_id = str(call.get("id") or "")
                    fn = call.get("function") or {}
                    tool_name = str(fn.get("name") or "").strip()
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        if isinstance(raw_args, dict):
                            args = raw_args
                        else:
                            args = json.loads(raw_args) if str(raw_args).strip() else {}
                        if not isinstance(args, dict):
                            args = {}
                    except Exception:
                        args = {}
                    try:
                        result_text = execute_tool(tool_name, args)
                    except Exception as error:
                        result_text = f"工具执行失败: {error}"
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": tool_name,
                            "content": str(result_text or "（无返回）"),
                        }
                    )
                continue

            text = str(content or "").strip()
            if text:
                return True, text
            return False, "AI 未返回文本内容。"

        return False, "工具调用轮次过多，已中止。请简化请求后重试。"
