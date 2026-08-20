# Hello Agent Android Shell

这是一个原生 Android 壳工程，用单个 `WebView` 聚合以下模块：

- `Agent`: `/agent/`
- `Todo`: `/agent/todo/`
- `Admin`: `/agent/admin/`
- `Angular20`: 外部 URL

## 目录

- `app/src/main/java/com/shishanling/helloagentshell/MainActivity.kt`
  - 抽屉菜单、单 `WebView`、返回键、异常页、外链跳浏览器
- `app/src/main/java/com/shishanling/helloagentshell/ModuleRegistry.kt`
  - 四个模块的标题、URL、域名白名单
- `app/src/main/res/layout/activity_main.xml`
  - `DrawerLayout + Toolbar + WebView + Loading/Error Overlay`

## URL 配置

在 `gradle.properties` 中修改：

```properties
helloAgentBaseUrl=https://106.13.175.227
angular20Url=https://106.13.175.227/angular20/
```

构建时会生成：

- `BuildConfig.HELLO_AGENT_BASE_URL`
- `BuildConfig.ANGULAR20_URL`

## 构建

```bash
cd android-shell
./gradlew :app:assembleDebug
```

APK 输出：

```text
android-shell/app/build/outputs/apk/debug/app-debug.apk
```

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
- `Angular20` 若不是同域，需要按其自身站点策略单独验证

## 当前已处理

- 抽屉菜单切换四个模块
- 页面加载中遮罩
- 主框架错误页与重试
- 外链跳系统浏览器
- 下载转浏览器继续
- 文件上传先给提示，后续二期补原生文件选择器

## 建议真机验证

1. `Agent -> Todo` 是否无需重复登录
2. `Admin` 登录一次后再次进入是否保持
3. `Angular20` 是否同域、是否能保留登录
4. 文件上传、下载、剪贴板、支付/第三方登录等是否有额外限制
