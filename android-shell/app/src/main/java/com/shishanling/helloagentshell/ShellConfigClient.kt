package com.shishanling.helloagentshell

import java.net.HttpURLConnection
import java.net.URL

internal class ShellConfigClient(
    private val modulesUrl: String = ShellConfig.MODULES_URL,
    private val versionUrl: String = AppUpdateConfig.VERSION_URL,
    private val userAgent: String = "HelloAgentShell/${BuildConfig.VERSION_NAME}",
) {
    fun fetch(): String {
        val documents = listOf(modulesUrl, versionUrl)
        var lastError: Exception? = null
        for (url in documents) {
            try {
                val body = get(url)
                val modules = ShellConfig.parseModulesDocument(body)
                require(modules.isNotEmpty())
                return body
            } catch (error: Exception) {
                lastError = error
            }
        }
        throw lastError ?: IllegalStateException("无法读取入口配置")
    }

    private fun get(url: String): String {
        require(ShellConfig.isAllowedConfigUrl(url)) { "配置地址不受信任" }
        val connection = (URL(url).openConnection() as HttpURLConnection).apply {
            connectTimeout = TIMEOUT_MS
            readTimeout = TIMEOUT_MS
            instanceFollowRedirects = false
            requestMethod = "GET"
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Cache-Control", "no-cache")
            setRequestProperty("User-Agent", userAgent)
        }
        try {
            val code = connection.responseCode
            require(code in 200..299) { "配置接口返回 $code" }
            return connection.inputStream.bufferedReader().use { it.readText() }
        } finally {
            connection.disconnect()
        }
    }

    private companion object {
        const val TIMEOUT_MS = 8_000
    }
}
