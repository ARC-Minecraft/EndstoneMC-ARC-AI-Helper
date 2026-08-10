## EndStone ARC AI Helper
[![Codacy Grade](https://app.codacy.com/project/badge/Grade/55ab81f1c00342de889d1d6376ea18f0)](https://app.codacy.com/gh/ARC-Minecraft/EndstoneMC-ARC-AI-Helper/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)


一个为 Endstone 服务器提供 **AI 聊天助手** 功能的插件。支持：

- 订阅 `PlayerChatEvent`，根据触发词自动和玩家对话
- `/ai` 指令打开 GUI 聊天面板，与 AI 进行多轮对话
- 多 Provider / 多模型轮询配置（支持按 Provider 配置代理）
- 可配置的起始词、包含词、上下文长度、助手头衔 / 名称、GUI 初始问候语

### 推荐运行环境

- **Python**：推荐 **Python 3.13**
- Endstone 版本：支持使用 Python 插件的 Endstone 最新版本

### 安装方式

1. 确保服务器已正确安装并启用 Endstone 的 Python 插件支持。
2. 将本项目打包并放入 Endstone 对应的 Python 插件目录，或直接把源码按官方示例方式放置。
3. 启动 / 重启服务器，Endstone 会根据 `pyproject.toml` 中的 entrypoint 自动加载：
   - 入口：`arc_ai_helper = "endstone_arc_ai_builder:ARCAIHelperPlugin"`

### 基本功能

- **自动聊天触发**
  - 监听 [`PlayerChatEvent`](https://endstone.dev/latest/reference/python/event/#endstone.event.PlayerChatEvent)。
  - 玩家消息满足下列任一条件时触发 AI 回复：
    - 以任一“起始词”开头，例如 `天星 现在几点？`
    - 包含任一“包含词”，例如消息中有 `请问`、`吗`、`?`、`？` 等。
  - 回复格式（可通过配置中头衔 / 名称间接控制）：
    ```text
    §u[AI助手]§r弧光天星-2026.3.17-15:19:
    你好，我是弧光游戏俱乐部的天星，有什么能帮你的？
    ```

- **/ai GUI 聊天面板**
  - 命令：`/ai`
  - 仅玩家可用，会打开一个 `ModalForm`：
    - 上方：以 Label 显示当前玩家与 AI 的聊天记录。
    - 下方：TextInput 输入框，用于输入要发送给 AI 的内容。
    - 提交后：
      - 把玩家输入和 AI 回复一并加入该玩家的上下文历史。
      - 自动刷新面板，让聊天记录即时更新。
  - 当该玩家还没有聊天历史时，面板顶部会显示一条 **可配置的初始问候语**。

- **上下文管理**
  - 按玩家名维护多轮对话历史。
  - 历史同时用于：
    - 自动触发的聊天
    - `/ai` GUI 面板
  - 上下文长度（最大保存的消息条数）可在配置中设定。

### 配置文件说明

插件第一次成功加载后，会在插件数据目录下（`self.data_folder`）生成以下文件：

#### 1. `chat_config.json`

示例（首次启动自动生成）：

```json
{
  "prefix_triggers": ["天星"],
  "contain_triggers": ["请问", "吗", "?", "？"],
  "max_history_messages": 20,
  "assistant_title": "AI助手",
  "assistant_name": "弧光天星",
  "gui_greet_message": "你好，我是弧光天星服务器小助理，请问有什么可以帮助您的？"
}
```

- **prefix_triggers**：起始词列表。若玩家消息以任意一项开头，则触发 AI 回复。
- **contain_triggers**：包含词列表。若玩家消息中包含任意一项，也会触发 AI 回复。
- **max_history_messages**：为每个玩家保存的最大历史消息条数（user + assistant 混合），超出会从旧的开始丢弃。
- **assistant_title**：助手头衔，用于聊天输出前缀里的 `\[头衔]`。
- **assistant_name**：助手名称，用于聊天输出前缀里的名字部分。
- **gui_greet_message**：当玩家第一次打开 `/ai` 面板、还没有历史消息时，显示在面板顶部的初始问候语。

#### 2. `system_prompt.txt`

- 用于设定系统级提示词，每次构造对话 messages 时，都会作为第一条 `system` 消息发送给模型。
- 默认内容示例：

```text
你是Minecraft服务器中的AI助手“天星”，需要用友好、简洁的中文回答玩家的问题，并尽量结合游戏内的背景来解释。
```

你可以根据实际服务器风格自由修改，比如增加玩家称呼、规则说明等。

#### 3. `providers.json`

用于配置一个或多个 AI Provider，支持多 API base URL、多密钥、多模型。

示例：

```json
[
  {
    "name": "default",
    "base_url": "https://api.openai.com/v1",
    "api_keys": ["你的_API_Key_1", "你的_API_Key_2"],
    "models": ["gpt-4.1-mini", "gpt-4.1"],
    "timeout": 60,
    "proxy": "127.0.0.1:7890"
  },
  {
    "name": "other-provider",
    "base_url": "https://your-other-endpoint/v1",
    "api_keys": ["other_key"],
    "models": ["your-model-a", "your-model-b"],
    "timeout": 60,
    "proxy": false
  }
]
```

- **name**：标识该 Provider 的名字，仅用于区分和调试。
- **base_url**：该 Provider 的 API 基础地址，会自动拼接 `/chat/completions`。
- **api_keys**：该 Provider 可用的 API 密钥列表，内部会轮询使用。
- **models**：可用模型名称列表，内部也会轮询使用。
- **timeout**：请求超时时间（秒）。
- **proxy**：该 Provider 请求时使用的 HTTP/HTTPS 代理。未配置时默认 `127.0.0.1:7890`；可写 `host:port` 或完整地址（如 `http://127.0.0.1:7890`、`socks5://127.0.0.1:7890`）；设为 `false`、`""`、`null` 等可关闭代理。

`ChatAIManager` 会在这些 Provider 与 key / model 组合之间做简单的轮询选择，提高可靠性与灵活性。

### 权限与命令

- **命令**
  - `/ai`：打开 AI 聊天 GUI 面板。

- **权限**
  - `arc_ai_helper.command.ai`：允许使用 `/ai` 指令，默认 `True`。

### 开发与调试建议

- 推荐在本地使用 Python 3.13 搭配虚拟环境进行开发。
- 检查 `providers.json` 是否正确填写了 `base_url` 和 `api_keys`，并确认网络连通性；若模型需翻墙，确认本机代理已开启且 `proxy` 配置正确。
- 若 AI 请求失败，插件会通过玩家聊天或服务器日志输出错误信息，方便排查。

