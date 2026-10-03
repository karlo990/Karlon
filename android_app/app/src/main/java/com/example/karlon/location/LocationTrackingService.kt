package com.example.karlon.location

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import androidx.core.app.NotificationCompat
import com.example.karlon.MainActivity
import com.example.karlon.R
import com.example.karlon.data.model.LocationRequest
import com.example.karlon.data.remote.RetrofitProvider
import com.example.karlon.data.remote.ServerConfig
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * Foreground service that shares this device's live GPS position with the
 * Karlon server, so a web/app dashboard can show where every team member
 * currently is (see server/app/routers/team_location.py and the "Live team
 * locations" panel in static/index.html).
 *
 * Android requires this to run as a foreground service with a persistent
 * notification whenever it reports location in the background — it can't
 * run invisibly, by design. Start/stop it via [start]/[stop] from a screen
 * that has already obtained ACCESS_FINE_LOCATION (+ ACCESS_BACKGROUND_LOCATION
 * on API 30+) from the user.
 */
class LocationTrackingService : Service() {

    private val jobs = SupervisorJob()
    private val scope = CoroutineScope(Dispatchers.IO + jobs)

    private lateinit var fusedClient: FusedLocationProviderClient
    private var callback: LocationCallback? = null

    override fun onCreate() {
        super.onCreate()
        fusedClient = LocationServices.getFusedLocationProviderClient(this)
        startForeground(NOTIFICATION_ID, buildNotification())
        beginReporting()
    }

    private fun beginReporting() {
        scope.launch {
            val serverConfig = ServerConfig(applicationContext)
            val baseUrl = serverConfig.currentServerUrl() ?: return@launch // not connected yet
            val displayName = serverConfig.currentDisplayName() ?: "Guest"
            val api = RetrofitProvider.buildApiService(baseUrl)

            @SuppressWarnings("HardwareIds")
            val memberId = Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID)
                ?: "unknown-device"

            val request = com.google.android.gms.location.LocationRequest.Builder(
                Priority.PRIORITY_BALANCED_POWER_ACCURACY,
                UPDATE_INTERVAL_MS,
            ).build()

            val cb = object : LocationCallback() {
                override fun onLocationResult(result: LocationResult) {
                    val loc = result.lastLocation ?: return
                    scope.launch {
                        try {
                            api.sendLocation(
                                LocationRequest(
                                    member_id = memberId,
                                    member_name = displayName,
                                    latitude = loc.latitude,
                                    longitude = loc.longitude,
                                    accuracy_m = loc.accuracy,
                                )
                            )
                        } catch (_: Exception) {
                            // Best-effort — next fix will retry. A transient network
                            // blip here shouldn't crash the foreground service.
                        }
                    }
                }
            }
            callback = cb

            try {
                fusedClient.requestLocationUpdates(request, cb, mainLooper)
            } catch (_: SecurityException) {
                // Permission wasn't actually granted — stop cleanly rather than
                // spin with a foreground notification doing nothing useful.
                stopSelf()
            }
        }
    }

    override fun onDestroy() {
        callback?.let { fusedClient.removeLocationUpdates(it) }
        jobs.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        val channelId = "team_location_channel"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NotificationManager::class.java)
            val channel = NotificationChannel(
                channelId,
                "Live location sharing",
                NotificationManager.IMPORTANCE_LOW,
            ).apply { description = "Shows when Karlon is sharing your location with the team" }
            manager.createNotificationChannel(channel)
        }

        val openApp = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )

        return NotificationCompat.Builder(this, channelId)
            .setContentTitle("Sharing your location")
            .setContentText("Karlon is sharing your live location with the team")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(openApp)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val NOTIFICATION_ID = 4201
        private const val UPDATE_INTERVAL_MS = 30_000L // 30s balance of "live" vs battery

        fun start(context: Context) {
            val intent = Intent(context, LocationTrackingService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, LocationTrackingService::class.java))
        }
    }
}
