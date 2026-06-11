package uk.iostream.enclave

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/** Encrypted persistence for the server URL and bearer token. */
object Prefs {
    private const val FILE = "enclave_secure_prefs"
    private const val KEY_URL = "server_url"
    private const val KEY_TOKEN = "token"
    private const val KEY_USER = "username"

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

    fun isConfigured(ctx: Context): Boolean =
        !serverUrl(ctx).isNullOrBlank() && !token(ctx).isNullOrBlank()

    fun save(ctx: Context, url: String, token: String, username: String) {
        prefs(ctx).edit()
            .putString(KEY_URL, url.trimEnd('/'))
            .putString(KEY_TOKEN, token)
            .putString(KEY_USER, username)
            .apply()
    }

    fun clearToken(ctx: Context) {
        prefs(ctx).edit().remove(KEY_TOKEN).apply()
    }
}
