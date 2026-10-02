package com.example.karlon.data.remote

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "karlon_settings")

/**
 * Persists the two things needed to talk to a Karlon server: its base
 * URL (LAN IP, or a remote tunnel URL — see server/README.md for both
 * paths) and the display name shown as message sender. This is what
 * lets the same APK be pointed at any PC running Karlon, on any network,
 * without a rebuild.
 */
class ServerConfig(private val context: Context) {

    private val serverUrlKey = stringPreferencesKey("server_url")
    private val displayNameKey = stringPreferencesKey("display_name")

    val serverUrlFlow: Flow<String?> =
        context.dataStore.data.map { it[serverUrlKey] }

    val displayNameFlow: Flow<String?> =
        context.dataStore.data.map { it[displayNameKey] }

    suspend fun currentServerUrl(): String? = serverUrlFlow.first()
    suspend fun currentDisplayName(): String? = displayNameFlow.first()

    suspend fun save(serverUrl: String, displayName: String) {
        context.dataStore.edit { prefs ->
            prefs[serverUrlKey] = normalize(serverUrl)
            prefs[displayNameKey] = displayName.trim()
        }
    }

    suspend fun clear() {
        context.dataStore.edit { it.clear() }
    }

    companion object {
        /** Ensures a trailing slash (Retrofit's baseUrl requires one) and
         * that the scheme is present, so a user typing "192.168.1.42:8000"
         * still works, not just "http://192.168.1.42:8000". */
        fun normalize(raw: String): String {
            var url = raw.trim()
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                url = "http://$url"
            }
            if (!url.endsWith("/")) url = "$url/"
            return url
        }
    }
}
