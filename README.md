# Hello Agent Lab：第一个 AI 练习

这是一个带计算器、代码沙箱、Todo、MCP 天气/知识库、OpenAPI 工具导入、长期记忆、多模型路由和工具结果 Offload 的 AI 学习助手。DeepSeek 负责判断该调用哪个工具，Python 负责执行具体操作。Todo、对话历史、记忆与卸载结果都保存在 SQLite 数据库中。

## 这个练习要理解什么

先关注 `src/hello_agent/app.py` 和 `src/hello_agent/tools.py` 中的执行流程：

1. `input()` 读取用户问题。
2. 程序把计算器的说明随问题一起发给模型。
3. 模型判断是否需要调用 `calculate`。
4. Python 执行计算，并把结果返回给模型。
5. 模型根据工具结果生成自然语言回答。

这就是最小的 Agent 循环：模型负责判断，代码负责执行工具。`AgentSession.messages` 保存本次运行中的历史消息，让模型能够理解连续追问；Todo 默认保存在 `data/todos.json`，重启程序后仍然存在。

当前常用工具：

- `calculate`：精确计算数学表达式。
- `run_python`：在隔离沙箱中执行短 Python 代码（无网络/文件/系统命令）。
- `add_todo` / `list_todos` / `complete_todo`：待办；完成前必须用户确认。
- `get_weather`：MCP 天气服务，查询中国省市区县实时天气与预报。
- `search_knowledge` / `list_knowledge_files`：本地知识库检索与文件列表。
- `web_search`：本地知识不足时的联网摘要兜底（需开启相关配置）。
- `remember_fact` / `forget_fact` / `list_memories`：跨会话长期记忆。
- `load_skill`：按需加载 `skills/` 中的工作说明书。
- `list_chat_attachments` / `read_chat_attachment`：读取当前会话上传的文本附件。
- `fetch_tool_result`：读取被 Offload 的完整工具结果（见下文）。
- 用户导入的 OpenAPI 操作：会以动态工具名出现在可用工具列表中。

## 近期能力速览

| 能力 | 入口 / 关键文件 | 说明 |
| --- | --- | --- |
| 多模型路由 | `model_routing.py`，环境变量 `DEEPSEEK_MODEL_CHEAP` / `STRONG` / `FALLBACK` | 简单任务走 cheap，复杂任务走 strong，失败可降级 |
| 长期记忆 | 功能中心「记忆」；`memory.py` | 偏好/事实/目标/约束跨会话保留，回答前自动注入相关条目 |
| 每日 Todo 简报 | 任务中心开关；`automations.py` + `scheduler.py` | **默认关闭**；开启后按小时生成简报，需 `AUTOMATION_SCHEDULER_ENABLED=true` |
| OpenAPI → 工具 | 功能中心「OpenAPI 工具」；`openapi_tools.py` | 粘贴 OpenAPI JSON，勾选接口后 Agent 可调用；内网地址拒绝；写操作需确认 |
| 工具结果 Offload | `tool_offload.py`，表 `tool_result_blobs` | 超过约 4000 字符的工具结果只把摘要+`ref` 放进上下文，全文可按需取回 |
| 多 Agent 协作 | `/多智能体` 或自动路由；`collaboration.py` | 规划→**人确认计划**→执行→审核；普通复杂目标可自动进入 |
| 协作 HITL | 规划后审批卡片；`collaboration_runs` | 确认后继续执行，取消则中止 |
| 协作自动路由 | `POST /routing/collaboration`；`collaboration_routing.py` | 判断是否走多 Agent；计算+记 Todo 等短组合保持普通对话 |
| 聊天附件文本分析 | 输入框附件按钮；`chat_attachments.py` | 会话级上传 md/txt/pdf/docx/图片；文字版直接抽文本，扫描件与图片走本地 Tesseract OCR |
| 可信问答 / 回归 | 评测与自动收录 | 知识优先，不足时网页兜底；失败可进入回归集 |

## 第一次运行

在当前项目目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

然后配置自己的 DeepSeek API Key：

```bash
export DEEPSEEK_API_KEY="你的 API Key"
```

API Key 不要写进源代码，也不要提交到 Git。

启动程序：

```bash
hello-agent
```

你可以尝试输入：

```text
请精确计算 (135.5 + 64.5) * 17 / 4
刚才的结果再乘以 2
```

也可以测试普通对话记忆：

```text
我叫小明，正在学习 AI Agent。
我刚才说我叫什么，正在学什么？
```

测试 Todo 工具：

```text
帮我添加一个任务：学习 Python asyncio
再添加一个任务：完成 Agent 练习
查看我的任务
```

终端会显示 `[Agent] 请求调用工具` 和 `[工具结果]`，帮助你观察模型决策与 Python 执行之间的分工。

完成任务时，Agent 会暂停并确认：

```text
完成任务 1
确认将任务 #1 标记为完成吗？输入 y 确认：y
```

输入 `y` 或 `yes` 才会修改 JSON，直接回车或输入其他内容都会取消。这展示了 Agent 的操作授权边界：模型可以建议执行，但修改数据的最终决定由用户作出。

输入 `exit` 后重新运行 `hello-agent`，再输入“查看我的任务”，可以验证 JSON 持久化。首次添加任务时程序会自动创建 `data/todos.json`。

输入 `exit` 退出。

## 运行测试

测试使用模拟客户端，不会调用真实 API，也不会产生模型费用：

```bash
python -m unittest discover -s tests -v
```

## AgentOps 自动评测

登录网页后打开“自动评测中心”，可以完成以下操作：

- 在“Prompt 版本”中新建草稿、发布新版本，或把历史版本重新发布完成回滚。
- 在“测试集”中添加问题和期望关键词，并临时启用或停用某条用例。
- 选择 Prompt、测试集和模型后后台运行评测；进度、失败重试和完成通知统一进入任务中心。
- 在“结果与对比”中查看成功率、耗时、Token、费用估算和相对上次完成版本的变化。

每次运行都会保存 Prompt 与测试集名称快照。费用估算默认不启用；如需显示，可在服务环境中配置每百万 Token 的价格：

```bash
DEEPSEEK_EVALUATION_PRICE_PER_MILLION=1
```

## 可以修改的小实验

打开 `src/hello_agent/app.py`：

- 修改 `SYSTEM_INSTRUCTIONS`，观察助手回答风格的变化。
- 修改 `DEEPSEEK_MODEL` 环境变量，尝试账户可用的其他模型。
- 在 `CALCULATOR_TOOL` 中修改工具描述，观察模型是否仍能正确调用。
- 查看 `AgentSession.messages`，理解每一轮对话如何被保存。
- 查看 `TodoStore`，理解多个工具如何共享同一份会话状态。
- 打开 `data/todos.json`，观察 Python 实际保存的数据。
- 思考：为什么 API Key 只能放在后端或本地环境变量中？

当前 JSON 方案适合单机、单用户练习，不适合多个进程同时写入。下一阶段可以升级为 FastAPI，让 Angular 或移动端通过 HTTP 使用这个 Agent。

## FastAPI 服务

启动 HTTP 服务：

```bash
uvicorn hello_agent.api:app --reload
```

浏览器打开：

```text
http://127.0.0.1:8000/docs
```

FastAPI 会显示可交互的接口页面。先调用 `POST /chat`：

```json
{
  "message": "我叫小明"
}
```

响应会包含一个 `session_id`。后续请求必须带上它才能保持对话：

```json
{
  "session_id": "第一次响应返回的 ID",
  "message": "我叫什么？"
}
```

完成任务默认不会执行，并返回 `approval_required: true`。确认后重新发送请求：

```json
{
  "session_id": "同一个 ID",
  "message": "完成任务 1",
  "approve": true
}
```

API 使用 SQLAlchemy 将数据保存到 `data/agent.db`：Todo 位于 `todos` 表，对话位于 `chat_messages` 表，并用 `session_id` 隔离不同会话。服务器重启后，使用同一个 `session_id` 就能恢复模型的对话记忆。为控制发送给模型的上下文长度，每次恢复最近 40 条消息，数据库中的完整记录不会因此删除。

## React 聊天界面

前端由最新版 React、TypeScript 和 Vite 构建，位于 `frontend`。后端保持运行，再打开第二个终端：

```bash
cd /Users/SSL/Documents/hello-agent-lab/frontend
n exec 26.5.0 npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。前端会调用 FastAPI 的 `/chat/stream`，并把 `session_id` 保存在浏览器 Local Storage 中。再次打开同一浏览器窗口时，前端会调用 `GET /sessions/{session_id}/messages`，从 SQLite 读取并显示最近 200 条历史消息。

## 账号登录

网页现在会先检查 HttpOnly 登录 Cookie。新用户可以使用邮箱、昵称和至少 8 位密码注册，注册后自动登录；已有用户使用邮箱和密码登录。密码通过带随机盐的 PBKDF2-SHA256 保存，数据库不会保存明文密码；登录 Cookie 对 JavaScript 不可见。聊天、历史记录、计划、Todo 同步和 MCP 工具接口都需要登录，并且后端会校验 Agent 会话属于当前用户。

登录相关接口：

- `POST /auth/register`
- `POST /auth/login`
- `GET /auth/me`
- `POST /auth/logout`

开发环境中，两个 Vite 前端都通过同源 `/api` 代理访问 `127.0.0.1:8000`，浏览器和手机不再直接跨域请求 8000 端口。因此后端按普通方式启动即可，前端会自动监听局域网地址。正式部署必须使用 HTTPS，并设置 `AUTH_COOKIE_SECURE=true` 和准确的 `FRONTEND_ORIGINS`，同时增加登录限流和密码找回能力。

聊天发送使用 `POST /chat/stream`。FastAPI 把 DeepSeek 返回的内容包装成 SSE 事件，React 通过 `ReadableStream` 持续读取并更新同一个回答气泡，因此不需要等待完整回答生成后再显示。原来的非流式 `POST /chat` 仍然保留，方便对比和调试。

## 聊天附件（文本分析）

登录后可在输入框旁上传本会话附件（Markdown、TXT、PDF、Word `.docx`、PNG/JPG/WEBP，单文件不超过 8 MB，每会话最多 5 个）。文字版 PDF 直接抽文本；扫描件 PDF 与图片使用**本地 Tesseract OCR**（默认 `chi_sim+eng`）。较长文件只注入目录与预览，Agent 可用 `read_chat_attachment` 分段读取。

这与知识库不同：附件只属于当前会话，不入向量索引，也不跨会话共享。OCR 可能有错字，回答时应注明来源文件。关闭 OCR：`OCR_ENABLED=false`。

相关接口：`GET/POST /chat/attachments`、`DELETE /chat/attachments/{id}`。健康检查 `/health` 会返回 `ocr_available`。

Agent 回答使用 `react-markdown` 和 `remark-gfm` 渲染标题、列表、表格、链接及任务列表，代码块由 `prism-react-renderer` 提供语法高亮和复制按钮。用户消息仍按纯文本显示；模型输出中的原始 HTML 和图片不会渲染，降低不受信内容直接注入页面的风险。

在聊天输入框输入 `/` 会打开操作菜单。可以继续输入文字筛选，也可以使用方向键选择、Enter 确认、Esc 关闭。选择命令后，输入框只显示功能标签和自然的操作提示，给模型使用的内部指令不会展示给用户，也不会在选择时自动执行。

选择 `/制定计划` 会进入结构化计划模式。后端通过 DeepSeek JSON Output 获取固定格式的数据，再由 Pydantic 校验标题、优先级、步骤数量和预计分钟数；验证成功后，React 将其显示为计划卡片。用户可以勾选需要执行的步骤，点击“确认同步到 Todo”后，后端才会把所选步骤写入当前会话的 Todo。这是一个包含“规划 → 人工确认 → 执行”的 Human-in-the-loop 工作流。计划同时以 Markdown 形式写入聊天历史，因此刷新后仍能恢复其内容。

选择 `/天气` 可以填入天气查询指令。天气功能目前只支持中国省、市、区县。工具优先使用 GeoToolCN 内置的中国行政区划数据离线匹配地址并取得 WGS-84 经纬度，再调用 Open-Meteo Forecast API 获取真实天气；天气服务不需要单独的 API Key。

本地数据覆盖省、市和区县，因此支持“驻马店市”“河南驻马店”“河南省驻马店市驿城区”等写法。同名地区无法通过上下文消除歧义时，工具会要求用户补充省或城市，不会擅自选择。离线数据没有匹配时，程序会把中文转换成拼音，使用 `countryCode=CN` 做最后一次受限查询，并过滤所有非中国结果。

天气能力现在使用 MCP（Model Context Protocol）连接。`weather_mcp_server.py` 是独立工具服务，`mcp_client.py` 通过 STDIO 启动它、完成协议握手并动态读取 `get_weather` 的名称、说明和参数结构。Agent 收到模型的工具调用后，不再直接执行天气函数，而是通过 MCP Client 请求 MCP Server。FastAPI 启动时不需要再单独打开一个天气服务终端，客户端会自动管理本地子进程。

## 用户知识库与 MCP

登录后从“功能中心 → 知识库”可以上传 Markdown、TXT、PDF 和 Word（`.docx`）文件，也可以一次导入项目自带的 10 份虚构演示资料。文档会解析为可检索片段，按账号隔离保存；其他用户不能查看、删除或检索你的资料。网页支持多文件选择、拖拽上传、分类筛选、片段统计和删除。

用户上传目录由 `KNOWLEDGE_UPLOAD_DIR` 配置，生产环境建议放在持久化数据目录并纳入备份。上传文件单个不超过 8 MB；加密 PDF 和纯图片扫描件暂时无法提取文字。

把 UTF-8 编码的 `.md` 或 `.txt` 文件放进项目的 `knowledge` 目录，Agent 就可以搜索它们。文件可以放在子目录中，每次调用工具都会读取最新内容，添加或修改资料后不需要重新建立索引。

聊天窗口输入 `/知识库`，或者直接提问：

```text
请根据本地知识库解释 MCP 和普通函数调用的区别，并标注来源。
```

`knowledge_mcp_server.py` 会把资料按段落切成小块，根据中英文关键词计算相关度，并返回文件名、片段编号和内容。DeepSeek 负责根据检索片段组织回答。这是一个不需要 Embedding 服务的 RAG 入门版本，后续可以升级为向量检索。

普通问题也会自动优先检索知识库，不需要添加“请根据本地知识库”这样的前缀。例如直接输入“什么是 RAG？”，后端会先调用 `search_knowledge`：命中时把最多 3 个相关片段作为本轮临时上下文交给模型并要求标注来源；未命中时再使用模型自身知识。临时检索片段不会写入聊天历史，避免上下文不断膨胀。

当前有两个独立 MCP Server，`mcp_client.py` 中的注册器负责合并它们的工具并将调用路由到正确服务。点击聊天窗口功能菜单中的「MCP 工具」，可以看到天气服务、本地知识库服务，以及你添加的远程 MCP。

可以单独启动 MCP Server 观察它进入等待连接状态（退出按 `Ctrl+C`）：

```bash
hello-agent-weather-mcp
```

知识库 MCP Server 也可以单独启动：

```bash
hello-agent-knowledge-mcp
```

## OpenAPI → 工具

登录后打开「功能中心 → OpenAPI 工具」：

1. 粘贴 OpenAPI **JSON**（暂不支持直接粘贴 YAML）。
2. 可选填写 API 根地址与 Bearer Token；不填根地址时使用文档里的 `servers.url`。
3. 先「解析并预览」，勾选要启用的接口（默认偏向非 DELETE），再「确认导入」。
4. 导入后，这些接口会出现在 Agent 可用工具中；对话里可直接让 Agent 调用。

安全边界：

- 拒绝 `localhost`、内网 IP、云元数据等地址（SSRF 防护）。
- Token 只保存在当前账号下，接口列表只返回 `has_auth`，不回显 Token。
- `POST` / `PUT` / `PATCH` / `DELETE` 等写操作调用前需要用户确认（与 `complete_todo` 同类）。
- 每账号最多 8 个来源，每来源最多启用 20 个工具。

相关代码：`openapi_tools.py`（解析与调用）、`openapi_registry.py`（注册到 Agent）、`SqliteOpenApiSourceStore`（持久化）。

## 工具结果 Offload

多轮工具调用时，MCP / OpenAPI / 长 `run_python` 输出容易把上下文窗口撑满。系统在工具结果写回模型前做卸载：

- 结果长度 **≥ 约 4000 字符** 时：全文写入 SQLite 表 `tool_result_blobs`，回灌模型的是精简 JSON（`offloaded`、`ref`、`preview`、`hint`）。
- 短结果不处理，行为与以前一致。
- 模型需要完整字段时，调用 `fetch_tool_result`，传入 stub 中的 `ref`。
- 按用户隔离；每个会话最多保留约 40 份卸载结果，超出淘汰旧记录。
- 覆盖对话 `ask` / `ask_stream` 与多 Agent 协作中的临时工具循环。

相关代码：`tool_offload.py`、`SqliteToolResultStore`；测试见 `tests/test_tool_offload.py`。

本地快速验收示例（不产生模型费用）：

```bash
python -m pytest tests/test_tool_offload.py -q
```

## 多模型路由

不同角色可走不同模型档位，降低成本和延迟：

```bash
# 可选，写入服务环境变量
DEEPSEEK_MODEL_CHEAP=deepseek-v4-flash
DEEPSEEK_MODEL_STRONG=deepseek-v4-pro
DEEPSEEK_MODEL_FALLBACK=deepseek-v4-flash,deepseek-v4-pro
```

对话与复杂规划默认偏 strong；部分辅助任务（如简报）可用 cheap。调用失败时按 fallback 链降级。监控看板会展示当前路由配置。

## 长期记忆与每日简报

长期记忆与聊天历史、知识库是三类不同信息：

- **聊天历史**：本会话近期问答。
- **长期记忆**：跨会话的偏好、事实、目标、约束（工具写入 + 规则抽取）。
- **知识库**：文档资料，不是用户画像。

每日 Todo 简报在「任务中心」中默认关闭。打开开关并设置小时后，由 `hello-agent-scheduler` 定时投递；也可点「立即生成」。服务器需：

```bash
AUTOMATION_SCHEDULER_ENABLED=true
```

## 多 Agent 协作（会用工具）

在输入框选择 `/多智能体`，或从功能菜单进入「多 Agent 协作」，描述一个目标后会启动流水线：

1. **规划 Agent**：根据当前可用工具清单拆步骤（cheap 模型），不直接调工具。
2. **人机确认**：规划完成后暂停，你确认「按此计划执行」或取消。
3. **执行 Agent**：按计划真实调用工具（计算器、`run_python`、天气、Todo 添加、知识库、已导入的 OpenAPI 读操作、`fetch_tool_result` 等）。
4. **审核 Agent**：检查是否缺工具证据；不合格可打回执行一轮；合格后交付最终中文答案。

**自动路由**：普通输入（未选 `/多智能体`）时，前端会先请求 `POST /routing/collaboration`。复杂目标（分阶段方案、先规划再执行、明确多 Agent 等）自动进入协作，用户消息会标「多 Agent 协作（自动）」；像「计算并记一条 Todo」、查天气、闲聊等仍走单 Agent 对话。手动 `/多智能体` 始终强制协作。

边界：

- `complete_todo`、旅行确认、OpenAPI 写操作等仍不能由协作代用户确认。
- 执行过程会显示在「执行过程」轨迹中（含 MCP / OpenAPI / 本地工具来源）。
- 规划会出现在回答区；确认后才继续执行，最终由审核交付覆盖为用户可读答案。

相关代码：`collaboration.py`、`SqliteCollaborationStore`、`collaboration_routing.py`；接口 `POST /collaborations/stream`、`POST /collaborations/{id}/approve`、`POST /collaborations/{id}/reject`、`POST /routing/collaboration`。

如需修改后端地址，复制 `frontend/.env.example` 为 `frontend/.env`，修改 `VITE_API_BASE_URL` 后重启前端开发服务器。

## SQLite 数据库

Todo 已从 JSON 文件升级到 SQLite，ORM 使用 SQLAlchemy 2。第一次启动时，程序会把旧的 `data/todos.json` 和 `data/sessions/<session_id>/todos.json` 数据导入 `data/agent.db`；旧 JSON 文件会保留，不会删除。

模型看到的工具没有变化：`add_todo`、`list_todos` 和 `complete_todo` 仍使用相同参数。变化只发生在工具内部：现在通过 SQLAlchemy `Session` 执行数据库事务。

## SQLite 对话记忆

每次模型成功回答后，程序会把用户问题和 Agent 最终回答作为一组写入 `chat_messages`。工具调用参数和工具结果仍只参与当前 Agent 循环，不作为聊天气泡长期展示。大体积工具结果会额外写入 `tool_result_blobs`（Offload），便于同会话按 `ref` 取回，同时避免把完整工具日志反复塞进模型上下文。

## 独立 Todo 应用

`todo-frontend` 是使用 React、TypeScript 和 Vite 创建的独立 Todo 网页，默认运行在 `http://localhost:5174`。它与 Agent 网页共用同一套账号 Cookie 和 FastAPI 后端。Todo 已按 `user_id` 隔离，同一用户的多个 Agent 会话共享任务。

Todo 使用“计划 → 步骤”的两层结构。Agent 结构化计划同步时，会同时保存计划标题、说明、优先级和用户选中的步骤；每个步骤只属于对应计划。Todo 页面按计划显示步骤数量、预计总时间和完成进度。用户也可以手动新建其他计划，再把任务添加到指定计划。升级前已经存在的独立任务不会丢失，会显示在“未分类任务”中。

启动 Todo 网页：

```bash
cd /Users/SSL/Documents/hello-agent-lab/todo-frontend
n exec 26.5.0 npm run dev
```

Todo 应用支持新建计划、向指定计划添加任务、筛选、完成、恢复、删除和手动刷新同步。布局针对手机浏览器进行了响应式适配。手机与电脑连接同一个 Wi-Fi 后，使用终端输出的 `Network` 地址并将端口改为 `5174` 即可访问。

## 管理后台

`admin-frontend` 是独立的 React、TypeScript 和 Vite 管理端，开发环境默认运行在 `http://localhost:5175`，生产路径为 `/agent/admin/`。后台使用独立的 `admin_users`、后台密码、MFA 会话和 HttpOnly Cookie；Agent 用户登录 Cookie 无法访问任何后台接口。

管理后台提供系统汇总、用户权限管理、操作审计和安全设置。后台管理员可以在 `knowledge_manager` 与 `member` 之间调整 Agent 用户角色，或启用、停用 Agent 账号；变更后目标账号的现有 Agent 登录会话会立即失效。

知识文档管理使用角色权限控制：`knowledge_manager` 可以上传、导入演示资料及删除自己的知识文档，`member` 只能查看和使用已有资料进行问答。前端会按角色隐藏管理操作，后端同时执行强制校验，不能通过直接请求接口绕过。

后台遵循隐私边界：只展示人数、调用量、成功率、Token、费用估算、评测和知识库容量等聚合指标，不提供用户聊天内容、Todo、旅行计划、提示词、回答、私有知识库文件名或正文。审计日志也只记录操作者、目标账号、角色和状态变化。

后台登录采用“独立密码 + Authenticator TOTP”两步验证。首次登录会显示二维码，要求设置至少 12 位的新后台密码，并生成 8 个只显示一次的恢复码。安全设置页展示 Authenticator 状态、恢复码余量、后台有效会话和超时规则，并可退出其他设备。

配置 SMTP 后，管理员可以在安全设置中向自己的后台邮箱发送验证码，验证后启用“邮箱恢复认证”。Authenticator 丢失时，仍需先验证正确的后台密码，才能把一次性验证码发送至已提前验证的邮箱。邮箱恢复登录后不能直接操作后台，必须立即重新绑定 Authenticator；旧恢复码会全部失效并生成 8 个新恢复码。邮箱验证码 5 分钟失效、60 秒内不能重复发送、最多尝试 5 次，只保存带服务端密钥的 HMAC，不记录验证码明文。

服务器先配置 `ADMIN_MFA_ENCRYPTION_KEY`，再创建管理员：

```bash
hello-agent-admin --email admin@example.com --name 系统管理员
```

后台会话无操作 30 分钟失效，最长 8 小时。正式启用 HTTPS 后，应同时设置 `ADMIN_COOKIE_SECURE=true`。

启动管理端：

```bash
cd /Users/SSL/Documents/hello-agent-lab/admin-frontend
n exec 20.19.0 npm run dev
```
