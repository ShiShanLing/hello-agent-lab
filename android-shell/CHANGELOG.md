# 服务器项目 Android 客户端更新记录

## [0.1.3] - 2026-09-07（versionCode 4）

- 关闭 WebView 算法变暗，修复工坊切换到明亮主题后又被压暗的问题。
- 刷新按钮图标改为跟随工具栏主题色，确保深色模式下仍清晰可见。

## [0.1.2] - 2026-09-03（versionCode 3）

- 第四个入口改为工坊 `https://shishanling.cn/workshop/`。
- 入口地址改为读取服务器配置（`modules.json`，并写入 `version.json` 的 `modules`）。
- 之后只改服务器配置即可调整地址，不必再为改链接打 APK。

## [0.1.1] - 2026-08-31（versionCode 2）

- 所有内置入口统一改用 `https://shishanling.cn`。
- 新增启动自动检查和菜单手动检查更新。
- 新版本通过服务器 JSON 清单发现，APK 从项目 GitHub Releases 下载。
- 下载完成后可授权并打开 Android 系统安装界面完成覆盖更新。

## [0.1.0] - 2026-08-20（versionCode 1）

- 首个版本，提供 Agent、Todo、Admin 和 Angular20 的统一 WebView 入口。
