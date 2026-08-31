package com.shishanling.helloagentshell

import java.io.File
import java.io.FileInputStream
import java.net.HttpURLConnection
import java.net.URL

internal class AppUpdateClient(
    private val versionUrl: String = AppUpdateConfig.VERSION_URL,
    private val userAgent: String = "HelloAgentShell/${BuildConfig.VERSION_NAME}",
) {
    fun fetchLatest(): AppUpdateInfo {
        val body = request(
            initialUrl = versionUrl,
            accept = "application/json",
            timeoutMs = REQUEST_TIMEOUT_MS,
            validateUrl = ::validateManifestUrl,
        ) { connection ->
            connection.inputStream.bufferedReader().use { it.readText() }
        }
        return AppUpdateManifestParser.parse(body)
    }

    fun download(apkUrl: String, destination: File, onProgress: (Int) -> Unit) {
        require(AppUpdateSecurity.isAllowedReleaseUrl(apkUrl)) { "APK 下载地址不受信任" }
        request(
            initialUrl = apkUrl,
            accept = "application/vnd.android.package-archive, application/octet-stream",
            timeoutMs = DOWNLOAD_TIMEOUT_MS,
            validateUrl = AppUpdateSecurity::requireAllowedRedirect,
        ) { connection ->
            val parent = destination.parentFile ?: error("更新目录无效")
            check(parent.exists() || parent.mkdirs()) { "无法创建更新目录" }
            val temporary = File(parent, "${destination.name}.part")
            val total = connection.contentLengthLong.takeIf { it > 0L }
            temporary.outputStream().buffered().use { output ->
                connection.inputStream.buffered().use { input ->
                    val buffer = ByteArray(BUFFER_SIZE)
                    var copied = 0L
                    var lastProgress = -1
                    while (true) {
                        val read = input.read(buffer)
                        if (read < 0) break
                        output.write(buffer, 0, read)
                        copied += read
                        val progress = total?.let {
                            ((copied * 100L) / it).toInt().coerceIn(0, 100)
                        } ?: 0
                        if (progress != lastProgress) {
                            lastProgress = progress
                            onProgress(progress)
                        }
                    }
                    require(copied > 0L) { "下载的安装包为空" }
                    if (total != null) require(copied == total) { "安装包下载不完整" }
                }
            }
            require(isZipFile(temporary)) { "下载内容不是有效的 APK" }
            if (destination.exists()) check(destination.delete()) { "无法替换旧安装包" }
            if (!temporary.renameTo(destination)) {
                temporary.copyTo(destination, overwrite = true)
                check(temporary.delete()) { "无法清理临时安装包" }
            }
            onProgress(100)
        }
    }

    private fun <T> request(
        initialUrl: String,
        accept: String,
        timeoutMs: Int,
        validateUrl: (String) -> Unit,
        read: (HttpURLConnection) -> T,
    ): T {
        var currentUrl = initialUrl
        repeat(MAX_REDIRECTS) {
            validateUrl(currentUrl)
            val connection = (URL(currentUrl).openConnection() as HttpURLConnection).apply {
                connectTimeout = REQUEST_TIMEOUT_MS
                readTimeout = timeoutMs
                instanceFollowRedirects = false
                requestMethod = "GET"
                setRequestProperty("Accept", accept)
                setRequestProperty("Accept-Encoding", "identity")
                setRequestProperty("Connection", "close")
                setRequestProperty("User-Agent", userAgent)
            }
            try {
                val responseCode = connection.responseCode
                if (responseCode in REDIRECT_CODES) {
                    val location = connection.getHeaderField("Location")?.trim().orEmpty()
                    require(location.isNotEmpty()) { "服务器返回 $responseCode，但没有跳转地址" }
                    currentUrl = URL(URL(currentUrl), location).toString()
                    return@repeat
                }
                require(responseCode in 200..299) { "服务器返回 $responseCode" }
                return read(connection)
            } finally {
                connection.disconnect()
            }
        }
        error("下载跳转次数过多")
    }

    private fun validateManifestUrl(value: String) {
        require(value == AppUpdateConfig.VERSION_URL) { "更新清单地址不受信任" }
    }

    private fun isZipFile(file: File): Boolean {
        val signature = ByteArray(4)
        return FileInputStream(file).use { input ->
            input.read(signature) == signature.size &&
                signature.contentEquals(byteArrayOf(0x50, 0x4B, 0x03, 0x04))
        }
    }

    private companion object {
        const val REQUEST_TIMEOUT_MS = 15_000
        const val DOWNLOAD_TIMEOUT_MS = 300_000
        const val BUFFER_SIZE = 16 * 1024
        const val MAX_REDIRECTS = 8
        val REDIRECT_CODES = setOf(301, 302, 303, 307, 308)
    }
}
