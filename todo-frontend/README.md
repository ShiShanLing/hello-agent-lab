# Todo 应用

独立的计划与任务网页。和 Agent 主站共用同一套账号 Cookie 与 FastAPI 后端，按用户隔离任务。源码在本目录。

仓库总览见根目录 [README.md](../README.md)。主站说明见 [frontend/README.md](../frontend/README.md)。

## 访问地址

| 环境 | 地址 |
| --- | --- |
| 生产（HTTPS） | https://106.13.175.227/agent/todo/ |
| 本地开发 | http://127.0.0.1:5174/ |
| 局域网开发 | 终端里 Vite 打印的 `Network` 地址，端口 **5174** |
| 后端 API（生产） | https://106.13.175.227/agent/api/ |
| 后端健康检查 | https://106.13.175.227/agent/api/health |
| 本地 API | 开发时前端把 `/api` 代理到 `http://127.0.0.1:8000` |

生产站点由 Nginx 提供静态文件，资源基础路径是 `/agent/todo/`。HTTP 会跳转到 HTTPS。

相关站点：

- Agent 主站：https://106.13.175.227/agent/
- 管理后台：https://106.13.175.227/agent/admin/

## 框架

| 项目 | 版本 / 说明 |
| --- | --- |
| UI 框架 | **React 19.2**（`react` / `react-dom`） |
| 语言 | **TypeScript ~6.0**，JSX 为 `react-jsx`，编译目标 **ES2023** |
| 构建工具 | **Vite 8** + `@vitejs/plugin-react` |
| 路由 | 单页应用，无独立前端路由库；逻辑在 `src/App.tsx` |
| HTTP | 浏览器 `fetch` + `credentials: 'include'`；开发走 Vite `/api` 代理，生产请求 `/agent/api` |
| UI 库 | 无第三方组件库，样式在 `src/App.css` |
| 检查 | Oxlint |

后端不是本目录的一部分：API 是仓库 `src/hello_agent/` 里的 **FastAPI + Uvicorn**。

## 编译环境

| 项目 | 说明 |
| --- | --- |
| 运行时 | **Node.js**：`package.json` 要求 `>=20.19.0`；仓库 `.nvmrc` / `.node-version` 指定 **26.5.0**，发布脚本按该版本构建 |
| 包管理器 | **npm**（以本目录 `package-lock.json` 为准） |
| 开发命令 | `npm run dev` → Vite 开发服务器，端口 **5174**，`host: true` |
| 生产构建 | `npm run build` → 先 `tsc -b` 做类型检查，再 `vite build`，产物在 `dist/` |
| 资源前缀 | 生产 `base` 为 `/agent/todo/`，本地开发为 `/` |
| 浏览器 | 需支持 ES2023 的现代浏览器（含手机 Safari / Chrome） |
| 依赖后端 | 本地需同时运行 FastAPI：`uvicorn hello_agent.api:app --reload`（默认 `127.0.0.1:8000`） |

## 功能

- **两层结构**：计划 → 步骤。Agent 同步过来的结构化计划会带上标题、说明、优先级和选中步骤。
- **收集箱**：没有归属计划的任务显示在「未分类任务」，升级前的旧任务不会丢。
- **操作**：新建计划、向指定计划添加任务、全部/未完成/已完成筛选、完成、恢复、删除、手动刷新同步。
- **布局**：针对手机浏览器做了响应式适配。

页面顶栏可跳回 Agent 主站。Agent 完成任务仍需要用户确认；本页可直接勾选完成或删除。

## 登录说明

使用 **Hello Agent 统一账号**，不是另一套 Todo 用户。

- 已在主站登录时，打开本页会通过 `GET /auth/me` 自动进入工作区。
- 未登录时可在本页用同一套邮箱/密码注册或登录，写入的也是 Agent Cookie `hello_agent_login`。
- 退出会调用 `POST /auth/logout`，主站和工坊的登录态一并失效。

## 本地开发

先启动 FastAPI，再启动本前端。开发时 `/api` 代理到 `127.0.0.1:8000`。

Node 版本与仓库 `.nvmrc` / `.node-version` 一致。

```bash
cd todo-frontend
npm install
npm run dev
```

生产构建的资源前缀必须是 `/agent/todo/`。不要把本应用的 `dist/` 发布到主站根目录。

## 发布

只发布 Todo：

```bash
./deploy/publish.sh publish --targets todo
```

同步目标只能是服务器叶子目录 `/var/www/projects/agent/todo/`，不要操作父目录 `/var/www/projects/agent/`。
