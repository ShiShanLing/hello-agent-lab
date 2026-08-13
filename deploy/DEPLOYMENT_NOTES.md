# 线上部署注意事项

本文记录 Hello Agent 项目的线上目录边界、前端子路径配置和发布验收要求，避免主站发布误删子项目或生成错误的静态资源地址。

## 1. 线上目录边界

线上站点根目录为：

```text
/var/www/projects/agent/
```

该目录同时包含三个独立前端：

```text
/var/www/projects/agent/          Agent 主站
/var/www/projects/agent/admin/   管理后台
/var/www/projects/agent/todo/    Todo 项目
```

三个目录属于三个独立的部署目标。更新其中一个项目时，不得删除或覆盖另外两个项目。

## 2. 禁止无保护规则地对站点根目录执行删除同步

发布 Agent 主站时，禁止对 `/var/www/projects/agent/` 使用无排除规则的删除同步，例如：

```bash
# 禁止：这会删除 admin/ 和 todo/ 等不在主站构建产物中的目录
rsync -az --delete frontend/dist/ SERVER:/var/www/projects/agent/
```

主站在不需要清理旧资源时，可以使用不删除额外文件的同步：

```bash
rsync -az frontend/dist/ SERVER:/var/www/projects/agent/
```

如果确实需要清理旧主站资源，必须明确排除 `admin/` 和 `todo/`，安全边界至少应等价于：

```bash
rsync -az --delete --exclude='admin/' --exclude='todo/' frontend/dist/ SERVER:/var/www/projects/agent/
```

Admin 和 Todo 也必须分别发布到自己的精确目录：

```bash
rsync -az admin-frontend/dist/ SERVER:/var/www/projects/agent/admin/
rsync -az todo-frontend/dist/ SERVER:/var/www/projects/agent/todo/
```

即使使用上述带排除规则的主站同步，也必须再次核对最终目标路径确实是 `/var/www/projects/agent/`，而不是 `/var/www/projects/`。如果只需要清理某个子项目的旧构建文件，也可以仅在确认过的单个子项目 `assets/` 目录中进行，不能把站点根目录或更上层目录作为删除目标。

## 3. Vite 子路径配置

部署在子路径下的 Vite 项目必须设置正确的生产环境 `base`，否则 `index.html` 会引用错误的 `/assets/...` 根路径并产生 404。

Todo 项目的生产配置应为：

```ts
export default defineConfig(({ mode }) => ({
  base: mode === 'production' ? '/agent/todo/' : '/',
  // ...
}))
```

Todo 项目的生产地址必须分别为：

```text
静态资源：/agent/todo/assets/...
后端接口：/agent/api/...
Agent 主站：/agent/
```

Admin 项目同理，生产静态资源必须使用 `/agent/admin/` 作为基础路径。Agent 主站使用 `/agent/`。

## 4. 构建产物检查

构建完成后不能只检查文件是否存在，还必须检查 `dist/index.html` 实际引用的 URL：

```bash
sed -n '1,20p' todo-frontend/dist/index.html
```

Todo 的正确结果示例：

```html
<script type="module" src="/agent/todo/assets/index-xxxx.js"></script>
<link rel="stylesheet" href="/agent/todo/assets/index-xxxx.css">
```

如果看到以下地址，则构建配置错误，禁止发布：

```html
<script type="module" src="/assets/index-xxxx.js"></script>
```

## 5. 发布前保护检查

发布任意一个前端前，先确认另外两个项目入口存在：

```bash
test -f /var/www/projects/agent/admin/index.html
test -f /var/www/projects/agent/todo/index.html
test -f /var/www/projects/agent/index.html
```

发布后必须再次执行同样的检查，不能只依赖同步命令的退出状态。

## 6. 发布后 HTTP 验收

每次发布必须直接请求页面及其当前 `index.html` 引用的 JS、CSS 文件，确认返回 200。下面的 `106.13.175.227` 仅为当前部署示例；如果服务器 IP、域名或反向代理入口变更，必须按实际生产入口替换后再验收。

```bash
curl -fsS -o /dev/null -w 'main=%{http_code}\n' \
  http://106.13.175.227/agent/
curl -fsS -o /dev/null -w 'admin=%{http_code}\n' \
  http://106.13.175.227/agent/admin/
curl -fsS -o /dev/null -w 'todo=%{http_code}\n' \
  http://106.13.175.227/agent/todo/
curl -fsS -o /dev/null -w 'api=%{http_code}\n' \
  http://106.13.175.227/agent/api/health
```

还需要从线上 `index.html` 读取带哈希的真实文件名，再验证对应资源，不能使用上一次构建的旧文件名。

验收标准：

- 主站、Admin、Todo 页面均返回 200。
- 当前构建引用的 JS 和 CSS 均返回 200。
- `/agent/api/health` 返回 200。
- API、Worker、Redis 服务均为 `active`。
- 浏览器控制台没有静态资源 404。

## 7. 后端重启注意事项

API 存在 SSE 长连接。正常重启时，旧进程可能停留在“等待连接关闭”状态。部署时应检查服务最终状态和健康接口，不能只执行 `systemctl restart` 后立即认为成功。

```bash
systemctl is-active hello-agent hello-agent-worker redis-server
curl -fsS http://127.0.0.1:8000/health
```

任务数据和通知保存在 SQLite/Redis 中，重启 Worker 前应避免清空 Redis 或任务数据库。

## 8. 发布完成清单

- [ ] 构建命令成功完成。
- [ ] `dist/index.html` 的资源基础路径正确。
- [ ] 同步目标是精确的项目目录。
- [ ] 没有对站点根目录使用 `--delete`。
- [ ] 主站、Admin、Todo 的 `index.html` 均仍存在。
- [ ] 三个页面和 API 健康接口均返回 200。
- [ ] 当前 JS、CSS 哈希资源均返回 200。
- [ ] API、Worker、Redis 均为 `active`。
- [ ] 浏览器实际打开页面后没有 404 或控制台错误。

## 9. 自动发布脚本

日常发布不要再手写 `rsync`。使用仓库脚本，它会按顺序执行：测试、构建、资源路径检查、SQLite 备份、Redis 持久化检查、快照、同步、验收；失败则回滚并写入发布记录。

```bash
./deploy/publish.sh publish --targets frontend
./deploy/publish.sh publish --targets backend,admin
./deploy/publish.sh publish --targets frontend --dry-run
./deploy/publish.sh history
./deploy/publish.sh check-dist
```

`--targets` 必须显式指定，不会默认发布全部应用。主站同步始终排除 `admin/` 和 `todo/`。发布记录写在服务器 `/var/lib/hello-agent/releases/`，可在管理后台「发布记录」页查看。

