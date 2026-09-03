package com.shishanling.helloagentshell

data class ShellModule(
    val menuId: Int,
    val title: String,
    val url: String,
    val host: String?,
    val id: String = "",
)
