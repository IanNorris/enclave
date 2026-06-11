package uk.iostream.enclave

import okhttp3.FormBody
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Thin HTTP helper for login. TLS trust is handled by the app's
 *  network_security_config (bundled server CA), so the default client works. */
object Api {
    val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .pingInterval(25, TimeUnit.SECONDS)
        .build()

    data class LoginResult(val token: String?, val error: String?)

    /** POST /api/auth/login (OAuth2 password form). Returns access_token. */
    fun login(baseUrl: String, username: String, password: String): LoginResult {
        val url = baseUrl.trimEnd('/') + "/api/auth/login"
        val body = FormBody.Builder()
            .add("username", username)
            .add("password", password)
            .build()
        val req = Request.Builder().url(url).post(body).build()
        return try {
            client.newCall(req).execute().use { resp ->
                val text = resp.body?.string().orEmpty()
                if (!resp.isSuccessful) {
                    return LoginResult(null, "HTTP ${resp.code}: ${text.take(200)}")
                }
                val token = JSONObject(text).optString("access_token", "")
                if (token.isBlank()) LoginResult(null, "No token in response")
                else LoginResult(token, null)
            }
        } catch (e: Exception) {
            LoginResult(null, e.message ?: e.toString())
        }
    }
}
