package com.shishanling.helloagentshell

import android.net.Uri

object ModuleRegistry {
    private val baseUrl = BuildConfig.HELLO_AGENT_BASE_URL.trimEnd('/')
    private val angular20Url = BuildConfig.ANGULAR20_URL

    val agent = shellModule(R.id.nav_agent, "Agent", "$baseUrl/agent/")
    val todo = shellModule(R.id.nav_todo, "Todo", "$baseUrl/agent/todo/")
    val admin = shellModule(R.id.nav_admin, "Admin", "$baseUrl/agent/admin/")
    val angular20 = shellModule(R.id.nav_angular20, "Angular20", angular20Url)

    val all = listOf(agent, todo, admin, angular20)

    fun byMenuId(menuId: Int): ShellModule = all.firstOrNull { it.menuId == menuId } ?: agent

    fun isInternalUrl(url: String): Boolean {
        val currentHost = Uri.parse(url).host ?: return false
        return all.mapNotNull { it.host }.contains(currentHost)
    }

    private fun shellModule(menuId: Int, title: String, url: String): ShellModule {
        val normalized = if (url.endsWith('/')) url else "$url/"
        return ShellModule(
            menuId = menuId,
            title = title,
            url = normalized,
            host = Uri.parse(normalized).host,
        )
    }
}
