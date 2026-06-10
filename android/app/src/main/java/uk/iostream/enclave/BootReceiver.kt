package uk.iostream.enclave

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restart the notification service after a device reboot. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action == Intent.ACTION_BOOT_COMPLETED && Prefs.isConfigured(context)) {
            NotificationService.start(context)
        }
    }
}
