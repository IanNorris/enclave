package uk.iostream.enclave

import android.Manifest
import android.app.Activity
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import kotlin.concurrent.thread

/** Full-screen WebView hosting the existing Enclave web UI. The bearer token
 *  is injected into the page's localStorage so the web app is already
 *  authenticated. When the token expires, the app silently re-authenticates
 *  with the stored credentials so the user is never bounced to a login screen. */
class MainActivity : Activity() {

    private lateinit var webView: WebView
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val fileChooserCode = 1001
    private val micPermissionCode = 1003
    // A WebView mic-permission request awaiting the OS RECORD_AUDIO grant.
    private var pendingWebPermission: PermissionRequest? = null
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
        requestMicPermissionIfNeeded()
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

        // The web UI serves agent-sent files (send_file / structured_response
        // downloads) as authenticated URLs. A plain WebView ignores <a download>
        // clicks, so route them through the app's HTTP client (which trusts the
        // server's self-signed CA via network_security_config) and save to the
        // device Downloads collection.
        webView.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
            downloadFile(url, contentDisposition, mimeType)
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
                    // Launching the system file picker will trigger onResume when
                    // we return — which normally reloads the WebView (for the
                    // background→foreground reconnect case). That reload would wipe
                    // the just-picked file (and the composer draft) before the web
                    // UI can use it, so suppress it for this round-trip.
                    skipNextResumeReload = true
                    startActivityForResult(intent, fileChooserCode)
                    true
                } catch (e: Exception) {
                    skipNextResumeReload = false
                    fileCallback = null
                    false
                }
            }

            // getUserMedia() inside the WebView (voice dictation) asks the host
            // app to grant the mic. Grant it only if we hold the OS RECORD_AUDIO
            // permission; otherwise request that first and grant on the result.
            override fun onPermissionRequest(request: PermissionRequest?) {
                request ?: return
                val wantsMic = request.resources.any {
                    it == PermissionRequest.RESOURCE_AUDIO_CAPTURE
                }
                if (!wantsMic) {
                    runOnUiThread { request.deny() }
                    return
                }
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    == PackageManager.PERMISSION_GRANTED) {
                    runOnUiThread {
                        request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                    }
                } else {
                    pendingWebPermission = request
                    // The permission dialog fires onResume on return, which would
                    // otherwise reload the WebView and abort the in-page recording
                    // flow — suppress that one reload (as the file chooser does).
                    skipNextResumeReload = true
                    requestPermissions(
                        arrayOf(Manifest.permission.RECORD_AUDIO), micPermissionCode,
                    )
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

    /** Ask for RECORD_AUDIO up front so that when the in-page voice dictation
     *  calls getUserMedia(), our onPermissionRequest handler can grant the mic
     *  synchronously. Deferring the grant across an async OS permission dialog
     *  causes the WebView to reject the in-flight getUserMedia as "denied", so
     *  holding the OS permission ahead of time is the reliable path. */
    private fun requestMicPermissionIfNeeded() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 2003)
        }
    }

    /** Fetch an authenticated file URL through the app's HTTP client (which
     *  trusts the server CA) and write it into the device Downloads collection.
     *  Runs off the UI thread; toasts the outcome. */
    private fun downloadFile(url: String, contentDisposition: String?, mimeType: String?) {
        val name = URLUtil.guessFileName(url, contentDisposition, mimeType)
        val mime = mimeType?.takeIf { it.isNotBlank() } ?: "application/octet-stream"
        Toast.makeText(this, "Downloading $name…", Toast.LENGTH_SHORT).show()
        thread {
            val ok = try {
                Api.client.newCall(Request.Builder().url(url).build()).execute().use { resp ->
                    val body = resp.body
                    if (!resp.isSuccessful || body == null) {
                        false
                    } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                        saveToMediaStore(name, mime, body.byteStream())
                    } else {
                        @Suppress("DEPRECATION")
                        val dir = Environment.getExternalStoragePublicDirectory(
                            Environment.DIRECTORY_DOWNLOADS,
                        )
                        dir.mkdirs()
                        File(dir, name).outputStream().use { out ->
                            body.byteStream().copyTo(out)
                        }
                        true
                    }
                }
            } catch (e: Exception) {
                false
            }
            runOnUiThread {
                Toast.makeText(
                    this,
                    if (ok) "Saved $name to Downloads" else "Download failed: $name",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }
    }

    private fun saveToMediaStore(
        name: String, mime: String, input: java.io.InputStream,
    ): Boolean {
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, name)
            put(MediaStore.Downloads.MIME_TYPE, mime)
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val resolver = contentResolver
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: return false
        val wrote = resolver.openOutputStream(uri)?.use { out ->
            input.copyTo(out)
            true
        } ?: false
        values.clear()
        values.put(MediaStore.Downloads.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
        return wrote
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == fileChooserCode) {
            val result = WebChromeClient.FileChooserParams.parseResult(resultCode, data)
            fileCallback?.onReceiveValue(result)
            fileCallback = null
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == micPermissionCode) {
            val granted = grantResults.isNotEmpty() &&
                grantResults[0] == PackageManager.PERMISSION_GRANTED
            val req = pendingWebPermission
            pendingWebPermission = null
            runOnUiThread {
                if (granted && req != null) {
                    req.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                } else {
                    req?.deny()
                }
            }
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
