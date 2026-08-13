# Agent 生产发布检查表（虚构测试数据）

> 本检查表只用于演示 RAG 检索和来源引用。

## 发布前

确认自动化测试全部通过，备份 SQLite 数据库，检查 DeepSeek API Key 已通过环境变量注入。前端部署到 `/agent/` 子路径时，静态资源基础路径必须设置为 `/agent/`，API 地址必须设置为 `/agent/api`。

## 发布中

发布 Agent 前端时必须排除 `/agent/todo/` 目录。后端更新完成后重启服务，并检查健康接口、API 文档和最新 50 行日志。

## 发布后

验证 Agent、Todo 和 Angular20 三个入口均返回 200。登录接口未携带 Cookie 时返回 401 JSON 属于正常现象，不应返回 HTML。

