# 管理后台

独立的管理员控制台：系统概览、费用、用户权限、审计和安全设置。源码在本目录。

仓库总览见根目录 [README.md](../README.md)。Agent 用户登录说明见 [frontend/README.md](../frontend/README.md)。

## 访问地址

| 环境 | 地址 |
| --- | --- |
| 生产（HTTPS） | https://106.13.175.227/agent/admin/ |
| 本地开发 | http://127.0.0.1:5175/ |
| 局域网开发 | 终端里 Vite 打印的 `Network` 地址，端口 **5175** |
| 后端 API（生产） | https://106.13.175.227/agent/api/ |
| 后端健康检查 | https://106.13.175.227/agent/api/health |
| 本地 API | 开发时前端把 `/api` 代理到 `http://127.0.0.1:8000` |

生产站点由 Nginx 提供静态文件，资源基础路径是 `/agent/admin/`。HTTP 会跳转到 HTTPS。

相关站点：

- Agent 主站：https://106.13.175.227/agent/
- Todo：https://106.13.175.227/agent/todo/

## 框架

| 项目 | 版本 / 说明 |
| --- | --- |
| UI 框架 | **React 19.2**（`react` / `react-dom`） |
| 语言 | **TypeScript ~6.0**，JSX 为 `react-jsx`，编译目标 **ES2023** |
| 构建工具 | **Vite 8** + `@vitejs/plugin-react` |
| 路由 | 单页应用，无独立前端路由库；侧栏页面状态在 `src/App.tsx` |
| HTTP | 浏览器 `fetch` + 独立管理员 Cookie；开发走 Vite `/api` 代理，生产请求 `/agent/api` |
| 其它库 | `qrcode`（首次绑定 Authenticator 时生成二维码） |
| 检查 | Oxlint |

后端不是本目录的一部分：API 是仓库 `src/hello_agent/` 里的 **FastAPI + Uvicorn**。后台接口与 Agent 用户 Cookie 隔离。

## 编译环境

| 项目 | 说明 |
| --- | --- |
| 运行时 | **Node.js**：`package.json` 要求 `>=20.19.0`；仓库 `.nvmrc` / `.node-version` 指定 **26.5.0**，发布脚本按该版本构建 |
| 包管理器 | **npm**（以本目录 `package-lock.json` 为准） |
| 开发命令 | `npm run dev` → Vite 开发服务器，端口 **5175**，`host: true` |
| 生产构建 | `npm run build` → 先 `tsc -b` 做类型检查，再 `vite build`，产物在 `dist/` |
| 资源前缀 | 生产 `base` 为 `/agent/admin/`，本地开发为 `/` |
| 浏览器 | 需支持 ES2023 的现代浏览器 |
| 依赖后端 | 本地需同时运行 FastAPI：`uvicorn hello_agent.api:app --reload`（默认 `127.0.0.1:8000`） |

## 功能

- **系统概览**：注册用户数、调用量、成功率、Token、费用估算、评测和知识库容量等聚合指标。
- **总费用**：按日/周/月估算 Token 费用（不是账单实扣）。
- **行情概览**：指数、板块、ETF 快照（运维观察用）。
- **用户与权限**：调整 Agent 用户角色（`knowledge_manager` / `member`），启用或停用账号；变更后该用户现有 Agent 会话立即失效。
- **后台管理员**：管理独立后台账号及知识库管理权限。
- **发布记录**：查看近期发布快照。
- **审计日志**：记录操作者、目标账号、角色和状态变化，不记录聊天或 Todo 内容。
- **安全设置**：Authenticator、恢复码、邮箱恢复认证、后台会话。
- **公共知识库**：有权限的后台账号可维护系统级资料；个人用户上传的私有知识不会出现在这里。

## 登录说明

后台账号与 Agent 用户完全分开。Agent Cookie `hello_agent_login` **不能**访问任何后台接口。

登录流程：

1. 使用独立后台邮箱和密码。
2. 再完成 MFA：Authenticator TOTP、一次性恢复码，或已启用的邮箱验证码。
3. 首次登录必须设置至少 12 位新后台密码、绑定 Authenticator，并保存 8 个只显示一次的恢复码。

会话无操作 30 分钟失效，最长 8 小时。正式环境需 `ADMIN_COOKIE_SECURE=true`。创建管理员：

```bash
hello-agent-admin --email admin@example.com --name 系统管理员
```

服务器需先配置 `ADMIN_MFA_ENCRYPTION_KEY`。邮箱恢复依赖 SMTP；Authenticator 丢失时仍须先验证后台密码。

隐私边界：后台不展示用户聊天、Todo、旅行计划、提示词、回答，以及私有知识库文件名或正文。

## 本地开发

先启动 FastAPI，再启动本前端。开发时 `/api` 代理到 `127.0.0.1:8000`。

Node 版本与仓库 `.nvmrc` / `.node-version` 一致。

```bash
cd admin-frontend
npm install
npm run dev
```

生产构建的资源前缀必须是 `/agent/admin/`。不要把本应用的 `dist/` 发布到主站根目录。

## 发布

只发布管理后台：

```bash
./deploy/publish.sh publish --targets admin
```

同步目标只能是服务器叶子目录 `/var/www/projects/agent/admin/`，不要操作父目录 `/var/www/projects/agent/`。
