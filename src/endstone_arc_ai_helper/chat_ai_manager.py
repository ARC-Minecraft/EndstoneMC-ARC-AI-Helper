import json
import os
from typing import Any, Dict, List, Optional, Tuple

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

    def chat(self, messages: List[Dict[str, str]]) -> Tuple[bool, str]:
        max_rounds = self._max_timeout_retry_rounds()
        last_error = ""

        for round_index in range(max_rounds):
            target = self._get_next_target()
            if target is None:
                return False, "未配置可用的AI Provider，请检查providers.json。"

            provider, api_key, model = target
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

                if self._should_retry_after_http_error(
                    response.status_code
                ) and round_index + 1 < max_rounds:
                    last_error = (
                        f"AI服务暂时不可用({response.status_code})，已切换模型重试。"
                        f" 详情: {error_message}"
                    )
                    continue

                return False, f"AI服务返回错误: {error_message}"

            try:
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    return False, "AI服务未返回结果。"

                message = choices[0].get("message") or {}
                content = message.get("content") or ""
                return True, str(content)
            except Exception as error:
                return False, f"解析AI响应失败: {error}"

        if last_error:
            return False, last_error
        return False, "AI服务多次超时或不可用，请稍后再试或检查providers.json中的模型列表。"

