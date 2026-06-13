package uk.iostream.enclave

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Foreground service holding a WebSocket to the orchestrator's global
 * notification stream. It posts:
 *   - one notification PER SESSION for "major_reply" events (tag = session id,
 *     so a new reply replaces the previous — only the latest is kept), and
 *   - a single ONGOING/pinned notification summarising sessions that need a
 *     reply (its own foreground notification).
 */
class NotificationService : Service() {

    private var ws: WebSocket? = null
    private val running = AtomicBoolean(false)
    private var reconnectDelayMs = 2000L
    private val awaiting = LinkedHashMap<String, String>() // session_id -> session_name

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannels()
        startForeground(PINNED_ID, buildPinnedNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (running.compareAndSet(false, true)) {
            connect()
            refreshAwaiting()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running.set(false)
        ws?.cancel()
        ws = null
        super.onDestroy()
    }

    // ─── WebSocket ───

    private fun connect() {
        val base = Prefs.serverUrl(this) ?: return
        val token = Prefs.token(this) ?: return
        val wsUrl = base.replaceFirst("http", "ws").trimEnd('/') +
            "/api/notifications/stream?token=$token"
        val req = Request.Builder().url(wsUrl).build()
        ws = Api.client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                reconnectDelayMs = 2000L
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleEvent(text)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                // 4001 = unauthorized (token expired). Try a silent re-auth
                // before reconnecting so the service self-heals in the background.
                if (response?.code == 401 || response?.code == 4001) reauth()
                scheduleReconnect()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (code == 4001) reauth()
                scheduleReconnect()
            }
        })
    }

    /** Silent re-auth with stored credentials, updating Prefs so the next
     *  reconnect uses a fresh token. Best-effort, runs off the main thread. */
    private fun reauth() {
        val base = Prefs.serverUrl(this) ?: return
        val user = Prefs.username(this) ?: return
        val pass = Prefs.password(this) ?: return
        try {
            val res = Api.login(base, user, pass)
            if (res.token != null) Prefs.updateToken(this, res.token)
        } catch (_: Exception) { }
    }

    private fun scheduleReconnect() {
        if (!running.get()) return
        ws = null
        android.os.Handler(mainLooper).postDelayed({
            if (running.get()) connect()
        }, reconnectDelayMs)
        reconnectDelayMs = (reconnectDelayMs * 2).coerceAtMost(30000L)
    }

    private fun handleEvent(text: String) {
        val obj = try { JSONObject(text) } catch (e: Exception) { return }
        when (obj.optString("type")) {
            "major_reply" -> postMajorReply(obj)
            "session_activity" -> {} // not used by the app
            "notification" -> refreshAwaiting() // awaiting/deferred state changed
            else -> {
                // Some events (deferred_ask, awaiting) may carry session info too.
                if (obj.has("awaiting_input") || obj.has("ask_count")) refreshAwaiting()
            }
        }
    }

    // ─── Major replies (one per session, replace-in-place) ───

    private fun postMajorReply(obj: JSONObject) {
        val sessionId = obj.optString("session_id").ifBlank { return }
        val name = obj.optString("session_name", sessionId)
        val body = obj.optString("text", "")
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val n = Notification.Builder(this, CHANNEL_REPLIES)
            .setSmallIcon(android.R.drawable.ic_dialog_email)
            .setContentTitle(name)
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setContentIntent(sessionIntent(sessionId))
            .build()
        // Tag by session id so a newer reply replaces the previous one.
        nm.notify(sessionId, REPLY_ID, n)
    }

    // ─── Pinned "needs reply" summary ───

    private fun refreshAwaiting() {
        val base = Prefs.serverUrl(this) ?: return
        val token = Prefs.token(this) ?: return
        Thread {
            try {
                val req = Request.Builder()
                    .url(base.trimEnd('/') + "/api/notifications")
                    .header("Authorization", "Bearer $token")
                    .build()
                Api.client.newCall(req).execute().use { resp ->
                    val txt = resp.body?.string().orEmpty()
                    if (!resp.isSuccessful) return@Thread
                    val list = JSONObject(txt).optJSONArray("notifications") ?: JSONArray()
                    awaiting.clear()
                    for (i in 0 until list.length()) {
                        val n = list.getJSONObject(i)
                        awaiting[n.optString("session_id")] = n.optString("session_name")
                    }
                    android.os.Handler(mainLooper).post { updatePinned() }
                }
            } catch (_: Exception) { }
        }.start()
    }

    private fun updatePinned() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(PINNED_ID, buildPinnedNotification())
    }

    private fun buildPinnedNotification(): Notification {
        val count = awaiting.size
        val title = if (count == 0) "Enclave" else "$count session${if (count == 1) "" else "s"} need a reply"
        val text = if (count == 0) "Connected" else awaiting.values.joinToString(", ")
        val builder = Notification.Builder(this, CHANNEL_STATUS)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
        // Tapping opens the first awaiting session (or just the app).
        val firstSession = awaiting.keys.firstOrNull()
        builder.setContentIntent(
            if (firstSession != null) sessionIntent(firstSession) else appIntent()
        )
        return builder.build()
    }

    // ─── Intents ───

    private fun sessionIntent(sessionId: String): PendingIntent {
        val intent = Intent(this, MainActivity::class.java).apply {
            putExtra(MainActivity.EXTRA_SESSION, sessionId)
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_NEW_TASK
        }
        return PendingIntent.getActivity(
            this, sessionId.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private fun appIntent(): PendingIntent {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_NEW_TASK
        }
        return PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private fun createChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_REPLIES, "Replies", NotificationManager.IMPORTANCE_HIGH,
                ).apply { description = "Agent replies, one per session" }
            )
            nm.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_STATUS, "Status", NotificationManager.IMPORTANCE_LOW,
                ).apply { description = "Sessions awaiting a reply (pinned)" }
            )
        }
    }

    companion object {
        private const val CHANNEL_REPLIES = "replies"
        private const val CHANNEL_STATUS = "status"
        private const val PINNED_ID = 1
        private const val REPLY_ID = 2

        fun start(ctx: Context) {
            val intent = Intent(ctx, NotificationService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(intent)
            } else {
                ctx.startService(intent)
            }
        }
    }
}
