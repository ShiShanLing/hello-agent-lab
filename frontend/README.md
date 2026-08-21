# Agent 主站

Hello Agent 的网页入口：登录、对话、工作流、旅行计划、知识库、评测和运行观测。源码在本目录。

仓库总览、后端能力和发布规则见根目录 [README.md](../README.md) 与 [AGENTS.md](../AGENTS.md)。

## 访问地址

| 环境 | 地址 |
| --- | --- |
| 生产（HTTPS） | https://106.13.175.227/agent/ |
| 本地开发 | http://127.0.0.1:5173/ |
| 局域网开发 | 终端里 Vite 打印的 `Network` 地址，端口 **5173** |
| 后端 API（生产） | https://106.13.175.227/agent/api/ |
| 后端健康检查 | https://106.13.175.227/agent/api/health |
| 本地 API | 开发时前端把 `/api` 代理到 `http://127.0.0.1:8000` |

生产站点由 Nginx 提供静态文件，资源基础路径是 `/agent/`。HTTP 会跳转到 HTTPS。

兄弟站点：

- Todo：https://106.13.175.227/agent/todo/
- 管理后台：https://106.13.175.227/agent/admin/

## 框架

| 项目 | 版本 / 说明 |
| --- | --- |
| UI 框架 | **React 19.2**（`react` / `react-dom`） |
| 语言 | **TypeScript ~6.0**，JSX 为 `react-jsx`，编译目标 **ES2023** |
| 构建工具 | **Vite 8** + `@vitejs/plugin-react` |
| 路由 | 单页应用，无独立前端路由库；页面状态在 `App.tsx` 内切换 |
| HTTP | 浏览器 `fetch`；开发走 Vite `/api` 代理，生产请求 `/agent/api` |
| 其它库 | `react-markdown` + `remark-gfm`、`prism-react-renderer`（代码高亮）、`@xyflow/react`（工作流画布） |
| 测试 / 检查 | Vitest、Oxlint |

后端不是本目录的一部分：API 是仓库 `src/hello_agent/` 里的 **FastAPI + Uvicorn**。

## 编译环境

| 项目 | 说明 |
| --- | --- |
| 运行时 | **Node.js**：`package.json` 要求 `>=20.19.0`；仓库 `.nvmrc` / `.node-version` 指定 **26.5.0**，发布脚本按该版本构建 |
| 包管理器 | **npm**（以本目录 `package-lock.json` 为准） |
| 开发命令 | `npm run dev` → Vite 开发服务器，端口 **5173**，`host: true` |
| 生产构建 | `npm run build` → 先 `tsc -b` 做类型检查，再 `vite build`，产物在 `dist/` |
| 资源前缀 | 生产 `base` 为 `/agent/`，本地开发为 `/` |
| 浏览器 | 需支持 ES2023 的现代浏览器（Chrome / Edge / Firefox / Safari 近期版本） |
| 依赖后端 | 本地需同时运行 FastAPI：`uvicorn hello_agent.api:app --reload`（默认 `127.0.0.1:8000`） |

## 功能

- **对话**：流式 SSE 回答、停止生成、Markdown 渲染、斜杠命令（计算、天气、Todo、计划、旅行、多智能体、知识库、记忆等）。
- **账号**：邮箱注册、密码登录、邮箱验证码登录、找回密码；登录态是 HttpOnly Cookie `hello_agent_login`（`path=/`）。
- **统一登录回跳**：工坊等站点可打开 `/agent/?next=/angular20/#...`。登录成功后，主站只允许跳回安全的 `/angular20` 路径。
- **计划与 Todo**：结构化计划需人工确认后再同步；与独立 Todo 应用共用同一套账号和任务数据。
- **功能中心**：知识库、OpenAPI 工具、MCP、长期记忆、可视化工作流、监控看板、自动评测。
- **任务中心**：后台任务进度；可选每日 Todo 简报。
- **附件**：当前会话上传 md/txt/pdf/docx/图片，扫描件走本地 OCR。

## 登录说明

聊天、知识库、计划、评测等接口都要求已登录。Cookie 对 JavaScript 不可见，请求需 `credentials: 'include'`。

工坊（Angular）不再自己管账号：点「使用统一账号登录」会进入本站；登录或注册成功后按 `next` 回到工坊。已在本站登录时，直接打开工坊即可。

退出登录会清除 Agent Cookie，工坊和 Todo 的登录态也会一起失效。管理后台使用另一套管理员 Cookie，互不影响。

## 本地开发

先在仓库根目录启动 FastAPI（默认 `127.0.0.1:8000`），再启动本前端。开发时 `/api` 会代理到后端，一般不用配置 `.env`。

Node 版本与仓库 `.nvmrc` / `.node-version` 一致（当前 26.5.0）。

```bash
cd frontend
npm install
npm run dev
```

常用脚本：`npm run build`、`npm test`、`npm run lint`。生产构建的资源前缀必须是 `/agent/`。

前后端不在同一域名时，复制 `.env.example` 为 `.env`，设置 `VITE_API_BASE_URL`（以及可选的 `VITE_TODO_APP_URL`）后重启开发服务器。

## 发布

只发布主站：

```bash
./deploy/publish.sh publish --targets frontend
```

主站 `rsync --delete` 必须排除 `todo/` 和 `admin/`，不要把兄弟应用删掉。
