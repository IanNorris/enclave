package uk.iostream.enclave

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/** Encrypted persistence for the server URL, bearer token, and credentials
 *  (so the app can silently re-authenticate when the token expires). */
object Prefs {
    private const val FILE = "enclave_secure_prefs"
    private const val KEY_URL = "server_url"
    private const val KEY_TOKEN = "token"
    private const val KEY_USER = "username"
    private const val KEY_PASS = "password"

    private fun prefs(ctx: Context) =
        EncryptedSharedPreferences.create(
            ctx,
            FILE,
            MasterKey.Builder(ctx).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )

    fun serverUrl(ctx: Context): String? = prefs(ctx).getString(KEY_URL, null)
    fun token(ctx: Context): String? = prefs(ctx).getString(KEY_TOKEN, null)
    fun username(ctx: Context): String? = prefs(ctx).getString(KEY_USER, null)
    fun password(ctx: Context): String? = prefs(ctx).getString(KEY_PASS, null)

    fun isConfigured(ctx: Context): Boolean =
        !serverUrl(ctx).isNullOrBlank() && !token(ctx).isNullOrBlank()

    /** True if we have what we need to silently re-authenticate. */
    fun canReauth(ctx: Context): Boolean =
        !serverUrl(ctx).isNullOrBlank() &&
            !username(ctx).isNullOrBlank() &&
            !password(ctx).isNullOrBlank()

    fun save(ctx: Context, url: String, token: String, username: String, password: String? = null) {
        val e = prefs(ctx).edit()
            .putString(KEY_URL, url.trimEnd('/'))
            .putString(KEY_TOKEN, token)
            .putString(KEY_USER, username)
        if (password != null) e.putString(KEY_PASS, password)
        e.apply()
    }

    /** Update just the stored token (e.g. after a silent re-auth or a manual
     *  login captured from the WebView). */
    fun updateToken(ctx: Context, token: String) {
        prefs(ctx).edit().putString(KEY_TOKEN, token).apply()
    }

    fun clearToken(ctx: Context) {
        prefs(ctx).edit().remove(KEY_TOKEN).apply()
    }
}
