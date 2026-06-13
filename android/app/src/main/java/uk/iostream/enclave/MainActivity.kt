package uk.iostream.enclave

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONObject
import kotlin.concurrent.thread

/** Full-screen WebView hosting the existing Enclave web UI. The bearer token
 *  is injected into the page's localStorage so the web app is already
 *  authenticated. When the token expires, the app silently re-authenticates
 *  with the stored credentials so the user is never bounced to a login screen. */
class MainActivity : Activity() {

    private lateinit var webView: WebView
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val fileChooserCode = 1001
    // Skip the first onResume (it fires right after onCreate's initial load).
    private var didInitialLoad = false
    // A session to select on the next page (re)load, e.g. from a notification tap.
    private var pendingSession: String? = null
    // Set when onNewIntent already navigated, so the following onResume doesn't reload over it.
    private var skipNextResumeReload = false
    // Guard against concurrent/looping silent re-auth attempts.
    @Volatile private var reauthInFlight = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        if (!Prefs.isConfigured(this)) {
            startActivity(Intent(this, ConnectionActivity::class.java))
            finish()
            return
        }

        requestNotificationPermissionIfNeeded()
        NotificationService.start(this)

        webView = WebView(this)
        setContentView(webView)

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = true
            cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
        }

        val baseUrl = Prefs.serverUrl(this)!!

        webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                // Always seed the current (always-fresh, since every login goes
                // through ConnectionActivity which updates Prefs) token into
                // localStorage before the SPA reads it.
                injectToken()
                injectPendingSession()
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                // If the web UI bounced us to its login screen, the token has
                // expired. Re-authenticate with stored credentials if we have
                // them (invisible); otherwise hand off to the app's own
                // connection screen (which captures the password for future
                // silent re-auth) and finish, so no /login WebView lingers to
                // bounce us in a loop.
                if (url != null && url.contains("/login")) {
                    if (Prefs.canReauth(this@MainActivity)) {
                        attemptSilentReauth(baseUrl)
                    } else {
                        startActivity(Intent(this@MainActivity, ConnectionActivity::class.java))
                        finish()
                    }
                }
            }

            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val u = request?.url?.toString() ?: return false
                // Keep same-origin navigation in the WebView; open external links in the browser.
                return if (u.startsWith(baseUrl)) {
                    false
                } else {
                    try { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(u))) } catch (_: Exception) {}
                    true
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?,
            ): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = filePathCallback
                val intent = fileChooserParams?.createIntent()
                return try {
                    startActivityForResult(intent, fileChooserCode)
                    true
                } catch (e: Exception) {
                    fileCallback = null
                    false
                }
            }
        }

        // Handle a notification tap that targets a specific session.
        val targetSession = intent?.getStringExtra(EXTRA_SESSION)
        if (targetSession != null) {
            pendingSession = targetSession
            webView.loadUrl("$baseUrl/chat")
        } else {
            webView.loadUrl("$baseUrl/")
        }
        didInitialLoad = true
    }

    override fun onResume() {
        super.onResume()
        // Returning from background: the in-page WebSocket may have dropped and
        // the active session can be stale, so force a reload to reconnect and
        // re-sync. Skip the first resume (onCreate already did the initial load)
        // and the resume that immediately follows a notification-tap onNewIntent
        // (which already navigated).
        if (skipNextResumeReload) {
            skipNextResumeReload = false
            return
        }
        if (didInitialLoad && this::webView.isInitialized) {
            // Make sure the (possibly restarted) notification service is up.
            if (Prefs.isConfigured(this)) NotificationService.start(this)
            webView.reload()
        }
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        val session = intent?.getStringExtra(EXTRA_SESSION) ?: return
        val baseUrl = Prefs.serverUrl(this) ?: return
        // Select the session on the next load (injected in onPageStarted before
        // the web UI reads localStorage), then navigate to chat. The onResume
        // that follows this intent must not reload over it.
        pendingSession = session
        skipNextResumeReload = true
        webView.loadUrl("$baseUrl/chat")
    }

    /** Seed the stored token into the page's localStorage before the SPA reads
     *  it. Prefs is the single source of truth (all logins update it), so we
     *  always inject the latest. */
    private fun injectToken() {
        val token = Prefs.token(this) ?: return
        val js = "try{localStorage.setItem('enclave_token', ${JSONObject.quote(token)});}catch(e){}"
        webView.evaluateJavascript(js, null)
    }

    /** Token expired (we're on /login) and we have stored credentials:
     *  re-authenticate off the UI thread, then reload the app authenticated. */
    private fun attemptSilentReauth(baseUrl: String) {
        if (reauthInFlight) return
        if (!Prefs.canReauth(this)) return
        reauthInFlight = true
        val user = Prefs.username(this)!!
        val pass = Prefs.password(this)!!
        thread {
            val res = Api.login(baseUrl, user, pass)
            runOnUiThread {
                reauthInFlight = false
                if (res.token != null) {
                    Prefs.updateToken(this, res.token)
                    NotificationService.start(this)
                    // Re-enter the app authenticated.
                    val js = "try{localStorage.setItem('enclave_token', ${JSONObject.quote(res.token)});}catch(e){}"
                    webView.evaluateJavascript(js) {
                        webView.loadUrl("$baseUrl/")
                    }
                } else {
                    // Stored creds no longer valid (e.g. password changed) — let
                    // the user re-enter them via the app's connection screen.
                    startActivity(Intent(this, ConnectionActivity::class.java))
                    finish()
                }
            }
        }
    }

    /** If a session is pending (from a notification tap), write it to localStorage
     *  before the web UI store reads it, then clear the pending marker. */
    private fun injectPendingSession() {
        val s = pendingSession ?: return
        pendingSession = null
        val js = "try{localStorage.setItem('enclave_selected_session', ${JSONObject.quote(s)});}catch(e){}"
        webView.evaluateJavascript(js, null)
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2002)
            }
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == fileChooserCode) {
            val result = WebChromeClient.FileChooserParams.parseResult(resultCode, data)
            fileCallback?.onReceiveValue(result)
            fileCallback = null
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (this::webView.isInitialized && webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    companion object {
        const val EXTRA_SESSION = "session_id"
    }
}
