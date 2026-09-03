# Hello Agent Lab 开发与发布说明

本文件供 Codex、AI 编程助手、自动化发布工具和后续开发人员使用。开始修改、构建或发布本仓库前，必须先阅读本文件。

## 项目概览

这是一个由 Python 后端、三个 React 前端、后台任务 Worker、知识库和运维配置组成的多应用项目。三个前端虽然在同一个仓库中，但在服务器上是三个必须独立保留、独立发布的站点。

| 本地目录 | 功能 | 本地开发端口 | 生产 URL | 服务器发布路径 |
| --- | --- | ---: | --- | --- |
| `frontend/` | Agent 主站：登录、对话、工作流、旅行计划、知识库、评测和运行观测 | 5173 | `/agent/` | `/var/www/projects/agent/` |
| `todo-frontend/` | Todo/计划管理：计划、步骤、筛选、完成状态和 Agent 任务同步 | 5174 | `/agent/todo/` | `/var/www/projects/agent/todo/` |
| `admin-frontend/` | 独立管理后台：管理员登录、MFA、用户与权限、审计、知识文档和安全设置 | 5175 | `/agent/admin/` | `/var/www/projects/agent/admin/` |
| `src/hello_agent/` | FastAPI API、Agent、认证、数据库、知识库、任务队列、MCP、OpenAPI 工具与工具结果 Offload | 8000（仅监听服务器本机） | `/agent/api/` | `/opt/hello-agent/src/hello_agent/` |
| `knowledge/` | 正式知识库文件 | — | 由 API 使用 | `/opt/hello-agent/knowledge/` |
| `demo_knowledge/` | 演示知识库文件 | — | 由 API 使用 | `/opt/hello-agent/demo_knowledge/` |
| `skills/` | Agent Skills 工作说明书 | — | 由 API 使用 | `/opt/hello-agent/skills/` |
| `deploy/` | Nginx、systemd 和环境变量示例 | — | — | 见下方运维文件映射 |

运行时数据不属于代码发布内容：

- SQLite 数据库：`/var/lib/hello-agent/agent.db`（含对话、Todo、记忆、OpenAPI 来源、工具 Offload、聊天附件元数据等表）
- 用户上传的知识文件：`/var/lib/hello-agent/knowledge_uploads/`
- 会话聊天附件正文：默认 `/var/lib/hello-agent/chat_attachments/`（可用 `CHAT_ATTACHMENT_DIR` 覆盖）
- 服务器环境变量：`/etc/hello-agent.env`
- Redis：`redis://127.0.0.1:6379/0`

禁止用仓库文件覆盖或删除上述运行时数据。

## 近期后端能力索引

助手或开发者排查时，可按模块快速定位：

| 能力 | 主要模块 |
| --- | --- |
| 多模型路由 | `model_routing.py` |
| 长期记忆 | `memory.py`，`SqliteMemoryStore` |
| 每日 Todo 简报 | `automations.py`，`scheduler.py`，`hello-agent-scheduler.service` |
| OpenAPI → 工具 | `openapi_tools.py`，`openapi_registry.py`，`SqliteOpenApiSourceStore` |
| 工具结果 Offload | `tool_offload.py`，`SqliteToolResultStore`（表 `tool_result_blobs`） |
| 多 Agent 协作 | `collaboration.py`（规划/执行/审核，规划后人机确认） |
| 协作检查点 | `SqliteCollaborationStore`（表 `collaboration_runs`） |
| 协作自动路由 | `collaboration_routing.py`，`POST /routing/collaboration` |
| 聊天附件文本分析 | `chat_attachments.py`，`SqliteChatAttachmentStore`（表 `chat_attachments`） |
| 本地 OCR | `ocr.py`（Tesseract + Pillow + pypdfium2）；环境变量 `OCR_ENABLED` / `OCR_LANG` |
| MCP | `mcp_client.py`，`SqliteMcpServerStore` |

用户可见说明与操作步骤见仓库根目录 `README.md` 的「近期能力速览」及后续对应章节。

## 发布红线：不得删除兄弟应用

服务器静态目录结构如下：

```text
/var/www/projects/
├── agent/                 # Agent 主站，也是下面两个应用的父目录
│   ├── index.html
│   ├── assets/
│   ├── todo/              # Todo 独立应用，必须保留
│   │   ├── index.html
│   │   └── assets/
│   └── admin/             # 管理后台独立应用，必须保留
│       ├── index.html
│       └── assets/
└── angular20/             # 另一项目，不属于本仓库，严禁改动
```

必须遵守以下规则：

1. **禁止删除或整体重建 `/var/www/projects/agent/`。** 该目录是三个应用共享的父目录。
2. **禁止把 `frontend/dist/` 用无排除规则的 `rsync --delete` 同步到 `/var/www/projects/agent/`。** 这会删除 `todo/` 和 `admin/`。
3. 发布主站时，如需删除旧主站资源，必须明确排除 `todo/` 和 `admin/`。
4. 发布 Todo 或管理后台时，只能操作各自的叶子目录，不能操作父目录 `/var/www/projects/agent/`。
5. 禁止改动 `/var/www/projects/angular20/`、`/var/www/uploads/` 或其他不属于本仓库的服务器目录。
6. 任何带 `--delete`、递归删除或目录替换的操作，执行前都必须再次核对最终目标的绝对路径；目标不得是 `/var/www/projects/agent/` 或 `/var/www/projects/`。

主站采用同步发布时，安全边界至少应等价于：

```bash
rsync -av --delete --exclude='admin/' --exclude='todo/' frontend/dist/ SERVER:/var/www/projects/agent/
```

其中 `SERVER` 只是服务器连接占位符，不要将本示例当作无需检查即可执行的发布命令。Todo 和管理后台应分别同步到以下叶子目录：

```text
todo-frontend/dist/  -> /var/www/projects/agent/todo/
admin-frontend/dist/ -> /var/www/projects/agent/admin/
```

## 前端构建要求

在各应用目录运行构建，输出均为各自的 `dist/`：

```bash
cd frontend && npm run build
cd todo-frontend && npm run build
cd admin-frontend && npm run build
```

生产构建的资源基础路径必须与 Nginx 路径一致：

- `frontend`: `/agent/`
- `todo-frontend`: `/agent/todo/`
- `admin-frontend`: `/agent/admin/`

构建后要检查每个 `dist/index.html` 中的 JavaScript、CSS 和图标 URL，确认使用对应的生产前缀。不要发布仍引用 `/assets/...` 的 Todo 构建产物，否则浏览器会从错误的站点根路径加载资源。

## 后端和运维文件

| 仓库文件 | 服务器位置或服务 |
| --- | --- |
| `deploy/hello-agent.nginx` | Nginx 站点配置；将 `/agent/api/` 代理到 `127.0.0.1:8000`，并提供三个前端的静态文件 |
| `deploy/hello-agent.service` | `hello-agent.service`，FastAPI/uvicorn 服务 |
| `deploy/hello-agent-worker.service` | `hello-agent-worker.service`，Redis/RQ 后台任务 Worker |
| `deploy/hello-agent.env.example` | `/etc/hello-agent.env` 的字段示例；真实密钥不得提交到仓库 |

后端代码发布到 `/opt/hello-agent/`。更新后端时不要删除 `.venv`、知识目录或服务器环境文件。数据库与上传目录位于 `/var/lib/hello-agent/`，不得包含在代码目录清理范围内。

## 自动发布

不要手写 `rsync` 发布。使用：

```bash
./deploy/publish.sh publish --targets frontend
./deploy/publish.sh publish --targets backend,admin
```

`--targets` 必须显式给出，可选 `frontend`、`todo`、`admin`、`backend`、`knowledge`、`skills`。脚本会运行测试、检查 `dist/index.html` 资源前缀、备份 SQLite、检查 Redis 持久化、快照当前版本，再同步并验收四个生产地址。失败会回滚并写入 `/var/lib/hello-agent/releases/`。主站 `--delete` 仍必须排除 `admin/` 和 `todo/`，脚本已内置该检查。

**发布到服务器时，必须同时把对应源码提交并推到 GitHub。** 禁止只更新生产、把改动留在本机工作区。GitHub 与生产落后，是上次行情页丢失的原因。不要提交 `.env`、密钥、数据库、上传文件、缓存或 `dist/`。

## 发布前检查

1. 确认本次发布的是主站、Todo、管理后台、后端中的哪一个，不要默认发布全部应用。
2. 确认本地构建成功，目标应用的 `dist/index.html` 和资源文件存在。
3. 确认远端绝对路径与上表完全一致。
4. 若命令包含删除行为，确认它只能影响目标应用的叶子目录；主站发布必须排除 `admin/` 和 `todo/`。
5. 更新 Nginx 前先校验配置；更新 systemd 服务后再按需重载并重启对应服务。

## 发布后检查

发布后至少验证当前生产环境对应的以下地址。下面的 `106.13.175.227` 仅为当前部署示例；如果服务器 IP、域名或反向代理入口变更，必须按实际生产入口替换后再检查：

```text
http://106.13.175.227/agent/
http://106.13.175.227/agent/todo/
http://106.13.175.227/agent/admin/
http://106.13.175.227/agent/api/health
```

四个地址都必须可访问。即使本次只发布主站，也必须检查 Todo 和管理后台，防止父目录同步误删子应用。

如果 `/agent/todo/` 或 `/agent/admin/` 返回 Nginx 500，而主站和 API 正常，优先检查对应服务器目录中的 `index.html` 是否被删除。当前 Nginx 的 SPA 回退会在回退文件不存在时产生内部重定向循环并返回 500。

## 大功能自动发布

完成一项新的大功能（新模块或新 Agent 能力，不是单纯改间距/文案）后，助手应在测试和必要构建通过后，用 `./deploy/publish.sh publish --targets ...` 发布受影响的部分，并把对应源码 `commit` + `push` 到 GitHub，不必再等用户说“发布吧”或“提交吧”。

仍须遵守本文全部发布红线：只发本次改动涉及的应用；主站删除同步必须排除 `admin/` 和 `todo/`；不得覆盖运行时数据；发布后检查四个生产地址。

## 修改范围原则

- 前端功能修改应尽量限制在对应的前端目录内。
- API、认证、数据库和任务队列修改位于 `src/hello_agent/`，并应运行相关测试。
- 不要把不同前端的构建产物相互复制或合并。
- 不要提交 `.env`、密钥、数据库、上传文件、缓存或服务器日志。
- 如果发布目标、服务器目录或删除范围不明确，停止发布并先向用户确认。
