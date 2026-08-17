## EndStone ARC AI Helper
[![Codacy Grade](https://app.codacy.com/project/badge/Grade/55ab81f1c00342de889d1d6376ea18f0)](https://app.codacy.com/gh/ARC-Minecraft/EndstoneMC-ARC-AI-Helper/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)


一个为 Endstone 服务器提供 **AI 聊天助手** 功能的插件。支持：

- 订阅 `PlayerChatEvent`，根据触发词自动和玩家对话
- `/ai` 指令打开 GUI 聊天面板，与 AI 进行多轮对话
- **优先走 AstrBot**：本机有[弧光 EndStone 消息中枢](https://github.com/ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub) 时，把玩家消息交给 AstrBot 正式对话管线（人格 / 记忆由 AstrBot 维护）
- 中枢不可用时，再降级到本机 `persona.txt` + `providers.json`
- 人格与系统提示严格分开：`persona.txt` 只用于降级人格，`system_prompt.txt` 只写能力（指令、颜色代码等）

### 推荐运行环境

- **Python**：推荐 **Python 3.13**
- Endstone 版本：支持使用 Python 插件的 Endstone 最新版本

### 安装方式

1. 确保服务器已正确安装并启用 Endstone 的 Python 插件支持。
2. 将本项目打包并放入 Endstone 对应的 Python 插件目录，或直接把源码按官方示例方式放置。
3. 启动 / 重启服务器，Endstone 会根据 `pyproject.toml` 中的 entrypoint 自动加载：
   - 入口：`arc_ai_helper = "endstone_arc_ai_helper.ai_helper_plugin:ARCAIHelperPlugin"`

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
  - 走 **AstrBot** 时：按 Minecraft **服务器名称**当成群、按玩家 **XUID** 当成用户 ID 交给 AstrBot（及记忆插件）。改名仍是同一个人；本插件只送当前这句话和 `system_prompt.txt` 能力说明。中枢还会给模型挂上查在线、查 TPS、执行指令等工具。
  - 走 **本机降级** 时：按玩家名维护多轮对话历史，并把 `persona.txt` + `system_prompt.txt` 一并作为 system 消息。
  - `/ai` GUI 面板仍会在本机保存展示用历史。

### 与 AstrBot 弧光消息中心对接

需要同时升级 **中枢 ≥ 1.4.0**。插件会用 `role=ai_helper` 单独连 `hub_host:hub_port`，不占用游戏子服编号，也不会在 QQ 里播报开停服。

1. 本机 AstrBot 启用「弧光EndStone消息中枢」，并已配置好模型与人格。
2. `chat_config.json` 里 `hub_host` / `hub_port` / `hub_token` 与中枢一致（默认同机 `127.0.0.1:19136`）。
3. 连接成功后日志会出现：`对话走 AstrBot 人格/记忆`。
4. 中枢连不上或版本过旧时，自动退回 `persona.txt` + `providers.json`。

### 配置文件说明

插件第一次成功加载后，会在插件数据目录下（`self.data_folder`）生成以下文件：

#### 1. `chat_config.json`

示例（首次启动自动生成）：

```json
{
  "prefix_triggers": ["天星"],
  "contain_triggers": ["请问", "吗", "?", "？"],
  "max_history_messages": 20,
  "max_queue_size": 10,
  "assistant_title": "AI助手",
  "assistant_name": "弧光天星",
  "gui_greet_message": "你好，我是弧光天星服务器小助理，请问有什么可以帮助您的？",
  "welcome_message": "欢迎来到弧光大陆服务器，我是人工智能助手弧光天星，需要找我的话喊我的名字天星就可以啦",
  "death_tip_message": "遇到困难了吗？有问题可以问我哦~喊我的名字天星我就来帮助你啦！",
  "hub_host": "127.0.0.1",
  "hub_port": 19136,
  "hub_token": "",
  "server_name": "",
  "astrbot_timeout": 180
}
```

- **prefix_triggers**：起始词列表。若玩家消息以任意一项开头，则触发 AI 回复。
- **contain_triggers**：包含词列表。若玩家消息中包含任意一项，也会触发 AI 回复。
- **max_history_messages**：为每个玩家保存的最大历史消息条数（user + assistant 混合），超出会从旧的开始丢弃。走 AstrBot 时这条只影响 GUI 展示，模型记忆由 AstrBot 维护。
- **assistant_title**：助手头衔，用于聊天输出前缀里的 `\[头衔]`。
- **assistant_name**：助手名称，用于聊天输出前缀里的名字部分。
- **gui_greet_message**：当玩家第一次打开 `/ai` 面板、还没有历史消息时，显示在面板顶部的初始问候语。
- **hub_host / hub_port / hub_token**：弧光消息中心地址，默认本机 `127.0.0.1:19136`，需与 QQ Sync / 中枢一致。
- **server_name**：在 AstrBot 里当作本服「群号」；留空则用 Endstone `server.name`。同一 XUID 在同一服务器名下会落到同一个人。
- **astrbot_timeout**：走 AstrBot 对话时的超时秒数。

#### 2. `persona.txt`（仅降级使用）

- **只有中枢不可用、走本机 `providers.json` 时才会发给模型。**
- 用来写人格、口吻、自称，不再把指令权限写在这里。
- 升级自旧版时：若原来的 `system_prompt.txt` 只是人格短文，会自动挪到本文件。

#### 3. `system_prompt.txt`（能力 / 策略，两条路径都会用）

- 无论走 AstrBot 还是本机模型，都会作为「额外系统说明」发送。
- 适合写：允许使用哪些游戏指令、颜色代码、非 OP 限制等。
- **不要**在这里写长篇人格；人格由 AstrBot WebUI 配置（有中枢时），或写在 `persona.txt`（降级时）。
- 默认内容包含 Minecraft 颜色代码和 `[execution_command:…]` 约定。

#### 4. `providers.json`

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
- 检查 `providers.json` 是否正确填写了 `base_url` 和 `api_keys`（仅降级路径需要）。
- 优先确认本机能连上弧光消息中心 `ws://127.0.0.1:19136`，且中枢版本 ≥ 1.4.0。
- 若 AI 请求失败，插件会通过玩家聊天或服务器日志输出错误信息，方便排查。

### 更新日志

- **1.2.0**：AstrBot 模式下用玩家 XUID 识别人、用服务器名称当群号，便于记忆插件对上同一个人（改名也能认）；并把查在线、查 TPS、服务器信息、执行指令封装成大模型工具。需搭配中枢 ≥ 1.4.0。
- **1.1.0**：本机弧光消息中心可用时走 AstrBot 对话（人格 / 记忆由 AstrBot 维护）；系统提示与人格拆成 `system_prompt.txt` / `persona.txt`，没有 AstrBot 时才用本机人格降级。

