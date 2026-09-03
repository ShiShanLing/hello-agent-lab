package com.shishanling.helloagentshell

import android.content.Context
import android.net.Uri

object ModuleRegistry {
    private const val PREFS_NAME = "shell_config"
    private const val PREFS_MODULES = "modules_json"

    @Volatile
    private var modules: List<ShellModule> = defaultModules()

    val agent: ShellModule get() = byId("agent")
    val all: List<ShellModule> get() = modules

    fun restore(context: Context) {
        val cached = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .getString(PREFS_MODULES, null)
            ?: return
        applyRemoteJson(cached)
    }

    fun persist(context: Context, json: String) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(PREFS_MODULES, json)
            .apply()
    }

    fun applyRemoteJson(json: String): Boolean {
        val specs = runCatching { ShellConfig.parseModulesDocument(json) }.getOrNull() ?: return false
        val next = specs.mapNotNull { spec ->
            val menuId = menuIdFor(spec.id) ?: return@mapNotNull null
            shellModule(menuId, spec.title, spec.url, spec.id)
        }
        if (next.none { it.id == "agent" }) return false
        modules = mergeWithDefaults(next)
        return true
    }

    fun byMenuId(menuId: Int): ShellModule =
        modules.firstOrNull { it.menuId == menuId } ?: agent

    fun isInternalUrl(url: String): Boolean {
        val currentHost = Uri.parse(url).host ?: return false
        return modules.mapNotNull { it.host }.contains(currentHost)
    }

    private fun byId(id: String): ShellModule =
        modules.firstOrNull { it.id == id } ?: defaultModules().first { it.id == "agent" }

    private fun mergeWithDefaults(remote: List<ShellModule>): List<ShellModule> {
        val byMenu = remote.associateBy { it.menuId }
        return defaultModules().map { fallback ->
            byMenu[fallback.menuId] ?: fallback
        }
    }

    private fun menuIdFor(id: String): Int? = when (id) {
        "agent" -> R.id.nav_agent
        "todo" -> R.id.nav_todo
        "admin" -> R.id.nav_admin
        "workshop", "angular20" -> R.id.nav_workshop
        else -> null
    }

    private fun defaultModules(): List<ShellModule> {
        val baseUrl = BuildConfig.HELLO_AGENT_BASE_URL.trimEnd('/')
        return listOf(
            shellModule(R.id.nav_agent, "Agent", "$baseUrl/agent/", "agent"),
            shellModule(R.id.nav_todo, "Todo", "$baseUrl/agent/todo/", "todo"),
            shellModule(R.id.nav_admin, "Admin", "$baseUrl/agent/admin/", "admin"),
            shellModule(R.id.nav_workshop, "工坊", BuildConfig.WORKSHOP_URL, "workshop"),
        )
    }

    private fun shellModule(menuId: Int, title: String, url: String, id: String): ShellModule {
        val normalized = ShellConfig.normalizeHttpsUrl(url)
        return ShellModule(
            menuId = menuId,
            title = title,
            url = normalized,
            host = Uri.parse(normalized).host,
            id = id,
        )
    }
}
