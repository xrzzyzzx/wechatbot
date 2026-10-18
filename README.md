# WeChat ↔ AstrBot 桥接（OneBot v11）

把**微信小号**接进 AstrBot，让你自己的 AI 人格在微信群和私聊里说话。

```
微信  ←→  WeFlow  ──SSE──→  本桥接  ──WebSocket 客户端──→  AstrBot
                               ↑                              ↑
                        OneBot v11 事件               aiocqhttp 服务端
```

本桥接以 **WebSocket 客户端**身份连接 AstrBot 的 aiocqhttp **服务端**，
与 NapCatQQ 接入 AstrBot 的方式完全一致 —— 所以你在 AstrBot 里配过 QQ 机器人，这里就是同样的套路。

## 特性

- **收消息**：订阅 WeFlow 的 SSE 推送，实时收微信消息，不轮询
- **发消息**：Windows UI Automation 纯键盘模拟，直接操作微信输入框
- **消息合并**：同一个人连着发的几条短消息，在 N 秒内合成一条再送 AI，省 token
- **三种群聊模式**：只理 @ / 全量回复 / 按群单独配置唤醒词
- **群昵称白名单**：重启后 @ 人不会失忆，一个群一个 `nicks/<群名>.json`
- **图片转文字**：接 Ollama 本地模型或任意 OpenAI 兼容接口，把图描述成文字再喂给 AI
- **网页面板**：`http://127.0.0.1:8766`，启停、看状态、在线改配置
- **自回复防护**：多层去重，防止 AI 跟自己的消息无限循环

> 本仓库**只有代码**。不含任何 token、wxid、群成员名单、聊天记录或截图。
> 你的所有数据都写在本地的 `config.json` 和 `nicks/` 里，已被 `.gitignore` 排除。

---

## 你需要准备好的三样东西

| # | 东西 | 说明 |
|---|------|------|
| 1 | **桌面版微信** | 建议用小号，别拿主号跑自动化 |
| 2 | **WeFlow** | 本地运行的微信数据工具，要开启 HTTP API（默认端口 5031）。本项目**不包含也不分发**它，请自行获取 |
| 3 | **AstrBot** | 已跑起来的实例，并在里面加好了 aiocqhttp 平台 |

三样都齐了再往下走，缺一样都会卡在启动阶段。

---

## 三步跑起来

### 第 1 步：装 Python

装 **Python 3.10 或更高版本**，安装时**务必勾上 `Add python.exe to PATH`**。
没勾就重装一遍，这是新手最常踩的坑。

### 第 2 步：双击 `start.bat`

第一次运行会自动做两件事：

1. 装依赖（`pip install -r requirements.txt`，只有第一次慢）
2. 发现没有 `config.json`，自动拉起配置向导，问你几个问题

向导要你填的值，按下表去拿：

| 向导问的 | 从哪拿 |
|---|---|
| WeFlow API 地址 | 一般就是 `http://127.0.0.1:5031`，除非你改过 WeFlow 端口 |
| **WeFlow Access Token** | WeFlow 的设置/API 页面里生成的 token，一串十六进制 |
| **机器人微信的 wxid** | 浏览器打开 `http://127.0.0.1:5031/api/v1/messages?access_token=你的token&limit=5`，返回 JSON 里的 `sessionId` 字段，`wxid_` 开头那串；私聊自己的会话就是你的 wxid |
| 机器人微信的昵称 | 微信里这个号显示的名字，原文照抄（用于识别"这条是我自己发的"） |
| AstrBot aiocqhttp 地址 | AstrBot 后台 → 平台 → 新增 `aiocqhttp`，里面填的 WebSocket 端口，格式 `ws://127.0.0.1:端口/ws`，两边必须一致 |
| 回复模式 | `mention`（只有 @、句首斜杠、唤醒词才回，推荐）或 `all`（全部消息都送 AI） |

> AstrBot 那边要加的平台配置长这样，端口自己定，跟着填：
> ```json
> {
>   "id": "wechat_bridge",
>   "type": "aiocqhttp",
>   "enable": true,
>   "ws_reverse_host": "0.0.0.0",
>   "ws_reverse_port": 11229,
>   "ws_reverse_token": ""
> }
> ```
> AstrBot 跑在 Docker 里的话，记得把该端口映射出来。

### 第 3 步：看面板

浏览器打开 `http://127.0.0.1:8766`。
面板上点「启动」，日志里能看到下面这几行，就说明整条链路通了：

```
[Web] 已启动
Bridge | WeFlow: http://127.0.0.1:5031 | OB11: ws://127.0.0.1:11229/ws | 发送: uia
✅ WeFlow API 正常
[OB11] ✅ 已连接到 AstrBot
```

然后在群里 @ 一下机器人试试。

---

## 群里什么时候才会回

`group_reply_mode` 决定默认行为：

| 模式 | 行为 |
|---|---|
| `mention` | 只响应：@机器人、消息开头是 `/`、或命中本群唤醒词 |
| `all` | 群里所有消息都推给 AI（机器人会主动插话） |

更细的控制写在 `nicks/<群名>.json` 里，第一次收到某群消息时会自动生成：

```json
{
  "chatroom": "12345678901@chatroom",
  "group": "群名",
  "回复策略": {
    "全量回复": false,
    "提到就回": true,
    "唤醒词": ["小明", "小明同学"]
  },
  "成员": {
    "123456789": "群昵称"
  }
}
```

- **全量回复**：`true` 时这个群的闲聊也会推给 AI，不看 @
- **唤醒词**：不 @ 也算被点名，一个群可以有自己的叫法
- **成员**：QQ/微信 ID → 群昵称的映射，桥接重启后靠它 @ 到人

> 这些文件里有群成员信息，**已被 `.gitignore` 排除**，不会跟着代码进仓库。

---

## 配置项一览

| 字段 | 说明 |
|---|---|
| `weflow_base_url` | WeFlow API 地址，默认 `http://127.0.0.1:5031` |
| `access_token` | WeFlow 的 Access Token |
| `bot_wxid` | 机器人微信的 wxid，决定 OneBot 侧的 `self_id` |
| `bot_nicknames` | 机器人在微信里的昵称，可写多个 |
| `astrbot_ob_url` | AstrBot aiocqhttp 地址，`ws://主机:端口/ws` |
| `astrbot_attachments` | 可选，AstrBot 的 attachments 目录；留空则图片走 base64 |
| `web_port` | 网页面板端口，默认 8766 |
| `buffer_seconds` | 消息合并窗口，默认 5 秒 |
| `group_reply_mode` | `mention` / `all` |
| `all_mode_groups` | `all` 模式下只对这些群生效，留空=所有群 |
| `wake_words` | 默认唤醒词，新建的群白名单继承它 |
| `image_caption_provider` | `ollama`（本地）或 `openai`（任意 OpenAI 兼容接口） |
| `image_caption_model` | 模型名，如 `llava:7b`、`gpt-4o-mini` |
| `image_caption_api_key` | `openai` 模式下填 |
| `image_caption_api_base` | `openai` 模式下的 base_url |
| `ollama_base_url` | `ollama` 模式下 Ollama 地址 |

不想用向导，也可以 `复制 config.example.json → config.json` 手改，字段说明都写在里面。
想重新填一遍，在项目目录跑 `python init_config.py`，它发现已有 `config.json` 会先问你要不要覆盖。

---

## 常见问题

**`❌ 无法连接 WeFlow`**
WeFlow 没开，或者 API 服务没启。先确认 `http://127.0.0.1:5031` 能在浏览器打开。
另外：如果你开着 Clash / V2Ray，系统代理会把 `127.0.0.1` 的流量也劫走。本项目已在 `config.py` 里强制对本机地址绕行代理，但仍建议把代理软件设为「直连本机」。

**`❌ Access Token 无效`**
token 复制错了，或者带上了多余空格。重新从 WeFlow 复制一遍。

**`正在搜索微信窗口` 一直卡住**
微信没启动，或者微信版本太新导致窗口类名变了。确认桌面版微信已登录，窗口没最小化到托盘。

**报错 `No module named xxx`**
依赖没装全。在项目目录手动跑一次 `python -m pip install -r requirements.txt`。

**@ 不到人 / 只能发纯文字**
`nicks/<群名>.json` 里那个群的 `成员` 表还是空的。让对方先在群里发一句话，桥接会自动补进去；或者手动补 ID → 昵称。

**面板打不开**
端口被占了。改 `web_port`（比如 8767）重启。

**控制台中文乱码**
在 cmd 里执行 `chcp 65001` 后用 `python main.py` 启动。

---

## 项目结构

```
wechat-ob11-bridge/
├── main.py              # 入口：生命周期、PID 防双开
├── config.py            # 加载 config.json，强制本机流量直连
├── state.py             # 全局共享状态
├── bridge_core.py       # 核心：SSE 收消息、缓冲合并、构造 OneBot 事件
├── ob_client.py         # WebSocket 客户端，连 AstrBot
├── ob_protocol.py       # OneBot v11 协议解析与 API 应答
├── uia_sender.py        # UIA 纯键盘发送器
├── senders.py           # 发送器工厂
├── nicks.py             # 群昵称白名单读写
├── web_panel.py         # 本地网页面板
├── init_config.py       # 首次配置向导
├── start.bat            # 一键启动：装依赖 → 配置 → 运行
├── config.example.json  # 配置模板
├── requirements.txt
└── nicks/               # 运行时生成，一个群一个文件（已 gitignore）
```

---

## 注意事项

- 自动化操作微信违反微信用户协议，**有封号风险**。请务必用**小号**，不要用主号。
- 本项目不包含、也不分发 WeFlow 和 AstrBot，请从各自的官方渠道获取并遵守其许可。
- 桥接只在本机回环地址上通信，不要把 `5031` / `11229` / `8766` 暴露到公网。

## 许可

MIT，见 [LICENSE](LICENSE)。
代码基于上游 **Akasha-WeChat** 项目的 OneBot v11 桥接改写，在此致谢。
