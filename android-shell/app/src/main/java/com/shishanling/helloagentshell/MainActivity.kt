package com.shishanling.helloagentshell

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.webkit.CookieManager
import android.webkit.DownloadListener
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.enableEdgeToEdge
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.content.res.AppCompatResources
import androidx.core.view.GravityCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewFeature
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.navigation.NavigationView
import com.shishanling.helloagentshell.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private var currentModule = ModuleRegistry.agent

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        configureWindowInsets()
        configureToolbar()
        configureDrawer()
        configureWebView()
        configureBackNavigation()

        if (savedInstanceState == null) {
            openModule(currentModule, forceReload = true)
        } else {
            binding.webView.restoreState(savedInstanceState)
            setToolbarTitle(currentModule.title)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        binding.webView.saveState(outState)
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add(0, MENU_REFRESH, 0, getString(R.string.refresh_current_page))
        menu.findItem(MENU_REFRESH)?.apply {
            icon = AppCompatResources.getDrawable(
                this@MainActivity,
                R.drawable.ic_refresh_toolbar
            )
            setShowAsAction(MenuItem.SHOW_AS_ACTION_ALWAYS)
        }
        menu.add(0, MENU_OPEN_BROWSER, 0, getString(R.string.open_in_browser))
        return true
    }

    override fun onOptionsItemSelected(item: android.view.MenuItem): Boolean {
        return when (item.itemId) {
            android.R.id.home -> {
                binding.drawerLayout.openDrawer(GravityCompat.START)
                true
            }

            MENU_REFRESH -> {
                hideError()
                if (binding.webView.url.isNullOrBlank()) {
                    binding.webView.loadUrl(currentModule.url)
                } else {
                    binding.webView.reload()
                }
                true
            }

            MENU_OPEN_BROWSER -> {
                openInBrowser(binding.webView.url ?: currentModule.url)
                true
            }

            else -> super.onOptionsItemSelected(item)
        }
    }

    private fun configureWindowInsets() {
        val toolbar = binding.toolbar
        val content = binding.contentContainer
        val navigationView = binding.navigationView
        val header = navigationView.getHeaderView(0)
        val toolbarMinHeight = toolbar.minimumHeight
        val toolbarPaddingLeft = toolbar.paddingLeft
        val toolbarPaddingRight = toolbar.paddingRight
        val contentPaddingLeft = content.paddingLeft
        val contentPaddingRight = content.paddingRight
        val contentPaddingBottom = content.paddingBottom
        val navigationPaddingLeft = navigationView.paddingLeft
        val navigationPaddingBottom = navigationView.paddingBottom
        val headerPaddingTop = header.paddingTop

        ViewCompat.setOnApplyWindowInsetsListener(binding.drawerLayout) { _, windowInsets ->
            val topInsets = windowInsets.getInsets(
                WindowInsetsCompat.Type.statusBars() or WindowInsetsCompat.Type.displayCutout()
            )
            val navigationBars = windowInsets.getInsets(WindowInsetsCompat.Type.navigationBars())
            val reducedTop = (
                topInsets.top - (TOOLBAR_TOP_INSET_REDUCTION_DP * resources.displayMetrics.density)
            ).toInt().coerceAtLeast(0)
            toolbar.updatePadding(
                left = toolbarPaddingLeft + topInsets.left,
                top = reducedTop,
                right = toolbarPaddingRight + topInsets.right,
            )
            toolbar.minimumHeight = toolbarMinHeight + reducedTop
            content.updatePadding(
                left = contentPaddingLeft + topInsets.left,
                right = contentPaddingRight + topInsets.right,
                bottom = contentPaddingBottom + navigationBars.bottom,
            )
            header.updatePadding(top = headerPaddingTop + reducedTop)
            navigationView.updatePadding(
                left = navigationPaddingLeft + topInsets.left,
                bottom = navigationPaddingBottom + navigationBars.bottom,
            )
            WindowInsetsCompat.CONSUMED
        }
    }

    private fun configureToolbar() {
        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.setHomeAsUpIndicator(android.R.drawable.ic_menu_sort_by_size)
        setToolbarTitle(currentModule.title)
    }

    private fun configureDrawer() {
        binding.navigationView.setCheckedItem(currentModule.menuId)
        binding.navigationView.setNavigationItemSelectedListener(
            NavigationView.OnNavigationItemSelectedListener { item ->
                val module = ModuleRegistry.byMenuId(item.itemId)
                openModule(module, forceReload = module != currentModule)
                binding.drawerLayout.closeDrawer(GravityCompat.START)
                true
            }
        )
        binding.retryButton.setOnClickListener {
            hideError()
            binding.webView.reload()
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(binding.webView, true)
        }

        binding.webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            cacheMode = WebSettings.LOAD_DEFAULT
            loadsImagesAutomatically = true
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            useWideViewPort = true
            loadWithOverviewMode = true
            builtInZoomControls = false
            displayZoomControls = false
            allowFileAccess = false
            allowContentAccess = true
            mediaPlaybackRequiresUserGesture = false
            userAgentString = "$userAgentString HelloAgentShell/0.1"
        }

        if (WebViewFeature.isFeatureSupported(WebViewFeature.ALGORITHMIC_DARKENING)) {
            WebSettingsCompat.setAlgorithmicDarkeningAllowed(binding.webView.settings, true)
        }

        binding.webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>?,
                fileChooserParams: WebChromeClient.FileChooserParams?,
            ): Boolean {
                Toast.makeText(
                    this@MainActivity,
                    "文件选择将在二期补齐，当前请先在浏览器中上传。",
                    Toast.LENGTH_LONG
                ).show()
                filePathCallback?.onReceiveValue(null)
                return true
            }
        }

        binding.webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                super.onPageStarted(view, url, favicon)
                showLoading("正在打开 ${currentModule.title}…")
                hideError()
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                hideLoading()
                CookieManager.getInstance().flush()
            }

            override fun shouldOverrideUrlLoading(
                view: WebView?,
                request: WebResourceRequest?,
            ): Boolean {
                val target = request?.url?.toString() ?: return false
                return if (ModuleRegistry.isInternalUrl(target)) {
                    false
                } else {
                    openInBrowser(target)
                    true
                }
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?,
            ) {
                super.onReceivedError(view, request, error)
                if (request?.isForMainFrame == true) {
                    showError(error?.description?.toString() ?: getString(R.string.error_message))
                }
            }
        }

        binding.webView.setDownloadListener(
            DownloadListener { url, _, _, _, _ ->
                MaterialAlertDialogBuilder(this)
                    .setMessage("当前下载会转到系统浏览器中继续。")
                    .setPositiveButton("继续") { _, _ -> openInBrowser(url) }
                    .setNegativeButton("取消", null)
                    .show()
            }
        )
    }

    private fun configureBackNavigation() {
        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    when {
                        binding.drawerLayout.isDrawerOpen(GravityCompat.START) ->
                            binding.drawerLayout.closeDrawer(GravityCompat.START)

                        binding.webView.canGoBack() -> binding.webView.goBack()
                        else -> finish()
                    }
                }
            }
        )
    }

    private fun openModule(module: ShellModule, forceReload: Boolean) {
        currentModule = module
        setToolbarTitle(module.title)
        binding.navigationView.setCheckedItem(module.menuId)
        hideError()
        if (forceReload || binding.webView.url == null) {
            binding.webView.loadUrl(module.url)
        } else if (binding.webView.url?.startsWith(module.url) == false) {
            binding.webView.loadUrl(module.url)
        }
    }

    private fun setToolbarTitle(title: String) {
        supportActionBar?.title = title
        binding.toolbar.title = title
    }

    private fun showLoading(message: String) {
        binding.loadingText.text = message
        binding.loadingOverlay.visibility = View.VISIBLE
    }

    private fun hideLoading() {
        binding.loadingOverlay.visibility = View.GONE
    }

    private fun showError(message: String) {
        hideLoading()
        binding.errorMessage.text = message
        binding.errorState.visibility = View.VISIBLE
    }

    private fun hideError() {
        binding.errorState.visibility = View.GONE
    }

    private fun openInBrowser(url: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (_: ActivityNotFoundException) {
            Toast.makeText(this, "当前设备没有可用浏览器。", Toast.LENGTH_SHORT).show()
        }
    }

    companion object {
        private const val MENU_REFRESH = 1000
        private const val MENU_OPEN_BROWSER = 1001
        private const val TOOLBAR_TOP_INSET_REDUCTION_DP = 15
    }
}
