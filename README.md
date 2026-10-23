# Ollama Agent Loop 例子

单文件页面，在浏览器里观察本地 [Ollama](https://ollama.com) 如何通过工具调用完成多步任务。模型只负责决定「下一步调用哪个函数、传什么参数」；真正的计算在浏览器里执行，结果再送回模型，直到它给出最终答案。

同目录的 `agent_wf.py` 是同一思路的命令行版本。

页面文件：`deepseek_html_20261023_097c08.html`

## 页面在做什么

打开页面后，它会请求 `http://localhost:11434/api/tags`，确认 Ollama 是否在运行，并填入本机已有模型。

点「运行 Agent」后，流程是：

1. 组装对话：一条系统提示 + 你的问题。
2. 把对话和工具清单发给 `POST /api/chat`（`stream: false`）。
3. 模型若返回 `tool_calls`，页面在本地执行对应函数，把结果以 `role: "tool"` 追加进对话。
4. 带着新对话再请求模型。没有工具调用时，把文本当作最终答案并结束。
5. 每一轮都画成时间线卡片；右侧同步显示当前上下文窗口。

系统提示要求：涉及计算或当前时间时必须调用工具，不要心算。

```
用户问题
   │
   ▼
┌─────────────┐
│  Ollama     │  messages + tools
│  /api/chat  │
└──────┬──────┘
       │
       ├─ 无 tool_calls ──► 展示最终答案，结束
       │
       └─ 有 tool_calls ──► 浏览器执行 add / multiply / get_current_time
                              │
                              ▼
                         结果写回 messages，进入下一轮
```

轮次有上限（默认 10，界面可调到 30）。同一工具、同一参数连续出现 4 次会主动打断，避免死循环。点「停止」会通过 `AbortController` 取消正在进行的请求。

## 内置工具

工具分两层：交给模型看的 JSON Schema（`TOOLS`），以及浏览器里真正执行的函数（`TOOL_IMPL`）。

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `add` | `a`, `b`（数字） | 返回两数之和 |
| `multiply` | `a`, `b`（数字） | 返回两数之积 |
| `get_current_time` | 无 | 返回当前时间的 ISO 字符串、小时、分钟 |

示例问题：

- **两步计算**：`(11434 + 12341) * 412`。模型应先 `add`，再 `multiply`。
- **时间 + 计算**：先 `get_current_time`，再用返回的小时去 `multiply`。
- **另一道题**：`37 + 85`，再把和乘以 `3`。

模型返回的 `arguments` 可能是对象，也可能是 JSON 字符串，页面两种都接受。未知工具名会记为执行失败，并把错误文本回给模型。

## 界面

左侧是问题和轮次时间线。每张卡片包含：

- 轮次、状态（思考中 / 工具调用 / 完成 / 失败）、本轮耗时
- 模型正文；若响应里有 `thinking` 字段，会单独显示
- 工具名、参数 JSON、本地执行结果
- 结束时的最终答案

右侧：

- **推理后端**：模型名（可下拉已安装模型）、`num_ctx`（默认 8192）、最大轮次
- **上下文窗口**：当前 `messages` 的角色和内容预览（system / user / assistant / tool）
- **使用前提**：启动 Ollama 和打开本页的命令

顶栏圆点：绿色表示已连上 Ollama，灰色表示未连接，运行中为琥珀色闪烁。请求体里温度固定为 `0.2`。

## 怎么跑

需要本机已安装并启动 Ollama，且模型支持 tools（页面提示里写的是 `qwen3:8b`、`qwen2.5:7b`）。

浏览器直接请求 `localhost:11434` 会跨域。启动 Ollama 时要放开来源，并用本地 HTTP 服务打开页面（不要用 `file://`）。

```bash
# 终端 1：允许任意来源访问 Ollama
OLLAMA_ORIGINS="*" ollama serve

# 若模型还没拉下来
ollama pull qwen3:8b

# 终端 2：在本目录起一个静态服务
cd /path/to/agentTest
python3 -m http.server 8000
```

浏览器打开：

```text
http://localhost:8000/deepseek_html_20261023_097c08.html
```

确认右上角变成「已连接 · N 个模型」，选好模型后点「运行 Agent」。

Ollama 已经在跑、但没带 `OLLAMA_ORIGINS` 时，先停掉再按上面的方式重启。macOS 上若是用菜单栏应用启动的，需要改成命令行启动，否则页面会显示「连接失败（检查 Ollama 与跨域设置）」。

## 和 agent_wf.py 的差别

`agent_wf.py` 用官方 Python SDK（`ollama.chat`）跑同一个循环，适合在终端里看打印结果。

| | HTML 页面 | `agent_wf.py` |
| --- | --- | --- |
| 入口 | 浏览器，问题可改 | 终端，问题写死在代码里 |
| 模型 | 页面上选择，默认 `qwen3:8b` | 固定 `qwen3:8b` |
| 工具 | `add`、`multiply`、`get_current_time` | 只有 `add`、`multiply` |
| 安全措施 | 最大轮次、连续相同调用 4 次中断、可手动停止 | 最多 10 轮 |
| 依赖 | 无，只要浏览器和 Ollama | `pip install ollama` |

Python 版示例：

```bash
pip install ollama
python3 agent_wf.py
```

它会计算 `(11434 + 12341) * 412`，并在终端打印每一轮的工具名、参数和本地结果。

## 想加一个新工具

在 `deepseek_html_20261023_097c08.html` 的 `<script>` 里做两处对应修改：

1. 在 `TOOLS` 里增加一条 function schema（`name`、`description`、`parameters`）。描述要写清楚何时该用，模型靠这段文字做选择。
2. 在 `TOOL_IMPL` 里用同名函数实现它，返回字符串。计算、时间、只读查询都可以；需要副作用的操作请自己控制范围。

改完刷新页面即可，没有构建步骤。

## 请求失败时看什么

页面上的错误会提示这三项，按顺序查：

1. Ollama 是否在 `11434` 端口运行（`curl http://localhost:11434/api/tags`）。
2. 当前模型是否支持 tools。不支持时，模型可能直接心算，或接口返回错误。
3. 是否用 `OLLAMA_ORIGINS="*"` 启动，并且页面是通过 `http://localhost:8000/...` 打开的。
