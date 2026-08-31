package com.shishanling.helloagentshell

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AppUpdateSecurityTest {
    @Test
    fun acceptsProjectGitHubReleaseApk() {
        assertTrue(
            AppUpdateSecurity.isAllowedReleaseUrl(
                "https://github.com/ShiShanLing/hello-agent-lab/releases/download/" +
                    "android-shell-v0.1.1/hello-agent-shell-0.1.1.apk"
            )
        )
    }

    @Test
    fun rejectsHttpAndOtherRepositories() {
        assertFalse(
            AppUpdateSecurity.isAllowedReleaseUrl(
                "http://github.com/ShiShanLing/hello-agent-lab/releases/download/" +
                    "android-shell-v0.1.1/hello-agent-shell-0.1.1.apk"
            )
        )
        assertFalse(
            AppUpdateSecurity.isAllowedReleaseUrl(
                "https://github.com/example/other/releases/download/v1/app.apk"
            )
        )
    }
}
