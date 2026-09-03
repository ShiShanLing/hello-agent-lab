package com.shishanling.helloagentshell

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ShellConfigSecurityTest {
    @Test
    fun acceptsHttpsPagesOnThisSite() {
        assertTrue(ShellConfig.isAllowedModuleUrl("https://shishanling.cn/workshop/"))
        assertTrue(ShellConfig.isAllowedModuleUrl("https://shishanling.cn/agent/todo/"))
    }

    @Test
    fun rejectsOtherHostsAndSchemes() {
        assertFalse(ShellConfig.isAllowedModuleUrl("http://shishanling.cn/workshop/"))
        assertFalse(ShellConfig.isAllowedModuleUrl("https://example.com/workshop/"))
        assertFalse(ShellConfig.isAllowedModuleUrl("https://evil.shishanling.cn/workshop/"))
        assertFalse(ShellConfig.isAllowedModuleUrl("javascript:alert(1)"))
    }

    @Test
    fun onlyTrustsKnownConfigEndpoints() {
        assertTrue(ShellConfig.isAllowedConfigUrl(ShellConfig.MODULES_URL))
        assertTrue(ShellConfig.isAllowedConfigUrl(AppUpdateConfig.VERSION_URL))
        assertFalse(ShellConfig.isAllowedConfigUrl("https://shishanling.cn/hello-agent-app/other.json"))
    }

    @Test
    fun parsesWorkshopEntryFromServerDocument() {
        val json = """
            {
              "modules": [
                {"id":"agent","title":"Agent","url":"https://shishanling.cn/agent"},
                {"id":"todo","title":"Todo","url":"https://shishanling.cn/agent/todo/"},
                {"id":"admin","title":"Admin","url":"https://shishanling.cn/agent/admin/"},
                {"id":"workshop","title":"工坊","url":"https://shishanling.cn/workshop/"}
              ]
            }
        """.trimIndent()
        val modules = ShellConfig.parseModulesDocument(json)
        assertEquals(4, modules.size)
        assertEquals("workshop", modules.last().id)
        assertEquals("https://shishanling.cn/workshop/", modules.last().url)
        assertEquals("https://shishanling.cn/agent/", modules.first().url)
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsUnknownModuleId() {
        ShellConfig.parseModulesDocument(
            """{"modules":[{"id":"evil","title":"x","url":"https://shishanling.cn/agent/"}]}"""
        )
    }
}
