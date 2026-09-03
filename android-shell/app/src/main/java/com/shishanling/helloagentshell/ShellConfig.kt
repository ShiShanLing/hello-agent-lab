package com.shishanling.helloagentshell

import org.json.JSONArray
import org.json.JSONObject
import java.net.URI

internal data class ShellModuleSpec(
    val id: String,
    val title: String,
    val url: String,
)

internal object ShellConfig {
    const val MODULES_URL = "https://shishanling.cn/hello-agent-app/modules.json"
    const val ALLOWED_HOST = "shishanling.cn"
    private val knownIds = setOf("agent", "todo", "admin", "workshop", "angular20")

    fun parseModulesDocument(json: String): List<ShellModuleSpec> {
        val root = JSONObject(json)
        val array = root.optJSONArray("modules") ?: JSONArray()
        require(array.length() > 0) { "配置文件缺少 modules" }
        val seen = linkedSetOf<String>()
        val specs = ArrayList<ShellModuleSpec>()
        for (index in 0 until array.length()) {
            val item = array.getJSONObject(index)
            val id = item.getString("id").trim().lowercase()
            val title = item.getString("title").trim()
            val url = normalizeHttpsUrl(item.getString("url"))
            require(id in knownIds) { "未知模块 $id" }
            require(title.isNotEmpty()) { "模块标题为空" }
            require(isAllowedModuleUrl(url)) { "模块地址不受信任: $url" }
            require(id !in seen) { "模块 $id 重复" }
            seen += id
            specs += ShellModuleSpec(id = id, title = title, url = url)
        }
        require(specs.any { it.id == "agent" }) { "配置必须包含 agent" }
        return specs
    }

    fun isAllowedConfigUrl(value: String): Boolean =
        value == MODULES_URL || value == AppUpdateConfig.VERSION_URL

    fun isAllowedModuleUrl(value: String): Boolean = runCatching {
        val uri = URI(value)
        uri.scheme.equals("https", ignoreCase = true) &&
            uri.userInfo == null &&
            uri.host.equals(ALLOWED_HOST, ignoreCase = true) &&
            uri.path.orEmpty().startsWith("/") &&
            !uri.path.contains("..")
    }.getOrDefault(false)

    fun normalizeHttpsUrl(value: String): String {
        val trimmed = value.trim()
        require(trimmed.isNotEmpty()) { "模块地址为空" }
        return if (trimmed.endsWith('/')) trimmed else "$trimmed/"
    }
}
