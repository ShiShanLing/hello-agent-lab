package com.shishanling.helloagentshell

import org.json.JSONObject
import java.net.URI

internal object AppUpdateConfig {
    const val VERSION_URL = "https://shishanling.cn/hello-agent-app/version.json"
    const val RELEASE_REPOSITORY = "ShiShanLing/hello-agent-lab"
}

internal data class AppUpdateInfo(
    val versionCode: Int,
    val versionName: String,
    val apkUrl: String,
    val releaseNotes: String,
    val forceUpdate: Boolean,
)

internal object AppUpdateManifestParser {
    fun parse(json: String): AppUpdateInfo {
        val value = JSONObject(json)
        val info = AppUpdateInfo(
            versionCode = value.getInt("versionCode"),
            versionName = value.getString("versionName").trim(),
            apkUrl = value.getString("apkUrl").trim(),
            releaseNotes = value.optString("releaseNotes").trim(),
            forceUpdate = value.optBoolean("forceUpdate", false),
        )
        require(info.versionCode > 0) { "versionCode 无效" }
        require(info.versionName.isNotEmpty()) { "缺少 versionName" }
        require(AppUpdateSecurity.isAllowedReleaseUrl(info.apkUrl)) {
            "APK 下载地址不是本项目的 GitHub Releases HTTPS 链接"
        }
        return info
    }
}

internal object AppUpdateSecurity {
    private const val GITHUB_HOST = "github.com"
    private const val RELEASE_PATH_PREFIX = "/${AppUpdateConfig.RELEASE_REPOSITORY}/releases/download/"
    private val allowedRedirectHosts = setOf(
        GITHUB_HOST,
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    )

    fun isAllowedReleaseUrl(value: String): Boolean = runCatching {
        val uri = URI(value)
        uri.scheme.equals("https", ignoreCase = true) &&
            uri.host.equals(GITHUB_HOST, ignoreCase = true) &&
            uri.path.startsWith(RELEASE_PATH_PREFIX) &&
            uri.path.endsWith(".apk", ignoreCase = true)
    }.getOrDefault(false)

    fun requireAllowedRedirect(value: String) {
        val uri = URI(value)
        require(uri.scheme.equals("https", ignoreCase = true)) { "下载链接必须使用 HTTPS" }
        require(uri.host?.lowercase() in allowedRedirectHosts) { "下载跳转到了不受信任的域名" }
    }
}
