# Hello Agent Android Shell

这是一个原生 Android 壳工程，用单个 `WebView` 聚合服务器上的站点入口。默认内置：

- `Agent`: `/agent/`
- `Todo`: `/agent/todo/`
- `Admin`: `/agent/admin/`
- `工坊`: `/workshop/`

启动后会拉取服务器配置覆盖上述地址，并缓存到本地。以后改链接只更新服务器 JSON，不必重发 APK。

## 目录

- `app/src/main/java/com/shishanling/helloagentshell/MainActivity.kt`
  - 抽屉菜单、单 `WebView`、返回键、异常页、外链跳浏览器、拉取入口配置
- `app/src/main/java/com/shishanling/helloagentshell/ModuleRegistry.kt`
  - 模块标题、URL、域名白名单，支持远程配置覆盖
- `config/modules.json`
  - 服务器入口配置源文件
- `app/src/main/res/layout/activity_main.xml`
  - `DrawerLayout + Toolbar + WebView + Loading/Error Overlay`

## 入口配置

源文件：`android-shell/config/modules.json`

生产读取顺序：

1. `https://shishanling.cn/hello-agent-app/modules.json`（需 Nginx 单独放行；当前仓库已写好 location）
2. `https://shishanling.cn/hello-agent-app/version.json` 里的 `modules` 字段（现有 Nginx 已能访问）

只允许 `https://shishanling.cn/...`。拉取失败时使用 APK 内置默认地址（工坊为 `/workshop/`）。

只改入口、不打 APK：

```bash
./android-shell/scripts/publish-modules.sh
```

兜底域名仍可在 `gradle.properties` 修改：

```properties
helloAgentBaseUrl=https://shishanling.cn
workshopUrl=https://shishanling.cn/workshop/
```

## 构建

```bash
cd android-shell
./gradlew :app:assembleDebug
```

APK 输出：

```text
android-shell/app/build/outputs/apk/debug/app-debug.apk
```

## 在线更新

- 更新清单：`https://shishanling.cn/hello-agent-app/version.json`
- APK 下载：`ShiShanLing/hello-agent-lab` 的 GitHub Releases
- 启动后自动静默检查，也可从右上角菜单选择“检查更新”
- 有新版本时显示版本号和更新说明，下载完成后打开 Android 系统安装界面
- 首次在线安装需要在 Android 设置中允许本应用安装未知来源应用

发布前先提交 `android-shell/` 的改动，然后运行：

```bash
./android-shell/scripts/publish-release.sh
```

脚本会运行单元测试、构建 APK、创建或更新 GitHub Release，并把 `version.json`
与 `modules.json` 上传到服务器 `/var/www/hello-agent-app/`。

## 登录态说明

壳层默认用一个共享 `WebView` 容器加载所有模块，并启用：

- `CookieManager.setAcceptCookie(true)`
- `CookieManager.setAcceptThirdPartyCookies(...)`
- `domStorageEnabled = true`

当前后端 Cookie 策略：

- 普通用户 Cookie: `path=/`, `SameSite=Lax`
- 后台管理员 Cookie: `path=/`, `SameSite=Strict`

因此在同一 HTTPS 域名下：

- `Agent` 与 `Todo` 应尽量复用登录态
- `Admin` 仍使用独立后台认证，但登录后可在壳内持续保持
- `工坊` 若同域可保留登录；若路径或站点策略变更，改服务器配置即可

## 当前已处理

- 抽屉菜单切换四个模块
- 页面加载中遮罩
- 主框架错误页与重试
- 外链跳系统浏览器
- 下载转浏览器继续
- 文件上传先给提示，后续二期补原生文件选择器
- 入口地址远程配置

## 建议真机验证

1. `Agent -> Todo` 是否无需重复登录
2. `Admin` 登录一次后再次进入是否保持
3. `工坊` 是否能打开 `https://shishanling.cn/workshop/`
4. 文件上传、下载、剪贴板、支付/第三方登录等是否有额外限制
