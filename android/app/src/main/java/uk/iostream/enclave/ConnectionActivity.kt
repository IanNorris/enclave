package uk.iostream.enclave

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import kotlin.concurrent.thread

/** First-run / re-auth screen: collect server URL + credentials, log in,
 *  persist the token, then launch the WebView. Built programmatically to keep
 *  the app dependency-light. */
class ConnectionActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val pad = (24 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#0f1117"))
            setPadding(pad, pad, pad, pad)
            gravity = Gravity.CENTER_VERTICAL
        }

        val title = TextView(this).apply {
            text = getString(R.string.connect_title)
            setTextColor(Color.parseColor("#e4e6f0"))
            textSize = 22f
            setPadding(0, 0, 0, pad)
        }

        fun field(hint: String, inputType: Int, preset: String? = null) = EditText(this).apply {
            this.hint = hint
            setHintTextColor(Color.parseColor("#8b8fa7"))
            setTextColor(Color.parseColor("#e4e6f0"))
            this.inputType = inputType
            if (preset != null) setText(preset)
        }

        val urlField = field(
            getString(R.string.server_url_hint),
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI,
            Prefs.serverUrl(this) ?: "https://",
        )
        val userField = field(
            getString(R.string.username_hint),
            InputType.TYPE_CLASS_TEXT,
            Prefs.username(this) ?: "ian",
        )
        val passField = field(
            getString(R.string.password_hint),
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD,
        )

        val status = TextView(this).apply {
            setTextColor(Color.parseColor("#e05555"))
            setPadding(0, pad / 2, 0, 0)
        }

        val button = Button(this).apply {
            text = getString(R.string.connect_button)
            setBackgroundColor(Color.parseColor("#6c9fff"))
            setTextColor(Color.WHITE)
        }

        button.setOnClickListener {
            val url = urlField.text.toString().trim().trimEnd('/')
            val user = userField.text.toString().trim()
            val pass = passField.text.toString()
            if (url.isBlank() || user.isBlank() || pass.isBlank()) {
                status.text = "All fields are required."
                return@setOnClickListener
            }
            if (!url.startsWith("https://") && !url.startsWith("http://")) {
                status.text = "URL must start with https:// or http://"
                return@setOnClickListener
            }
            button.isEnabled = false
            status.setTextColor(Color.parseColor("#8b8fa7"))
            status.text = "Connecting…"
            thread {
                val res = Api.login(url, user, pass)
                runOnUiThread {
                    button.isEnabled = true
                    if (res.token != null) {
                        Prefs.save(this, url, res.token, user)
                        NotificationService.start(this)
                        startActivity(Intent(this, MainActivity::class.java))
                        finish()
                    } else {
                        status.setTextColor(Color.parseColor("#e05555"))
                        status.text = "Login failed: ${res.error}"
                    }
                }
            }
        }

        val lp = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )
        root.addView(title, lp)
        root.addView(urlField, lp)
        root.addView(userField, lp)
        root.addView(passField, lp)
        root.addView(button, lp)
        root.addView(status, lp)
        setContentView(root)
    }
}
