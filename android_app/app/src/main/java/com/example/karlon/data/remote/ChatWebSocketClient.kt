package com.example.karlon.data.remote

import com.example.karlon.data.model.MessageDto
import com.google.gson.Gson
import com.google.gson.JsonObject
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

/**
 * One WebSocket per open chat thread — this WAS the realtime sync
 * mechanism. NOT CURRENTLY USED: [com.example.karlon.data.repository.
 * ChatRepository.observeChat] now polls REST instead (simpler to host
 * on platforms like Railway/Render). Left in place in case you want to
 * switch back to push-based sync later — just re-wire ChatRepository
 * to construct and call this again.
 *
 * Connects to ws(s)://<server>/ws/{chatId}; every message the server
 * broadcasts (see app/ws_manager.py) arrives here within milliseconds
 * of being sent by ANY client (this phone, the web UI, another phone,
 * or the wa_bridge importer), whether that client is on the same WiFi
 * or a remote network reaching the server through a tunnel.
 *
 * Exposes a cold [Flow] of decoded [MessageDto]s; poll_update events are
 * ignored here since the Compose UI in this build doesn't render polls
 * (text + image is the in-scope path) — extend the `when` below if you
 * add poll UI later.
 */
class ChatWebSocketClient(
    private val client: OkHttpClient,
    private val baseUrl: String,
    private val gson: Gson = Gson(),
) {
    private var socket: WebSocket? = null

    fun observe(chatId: String): Flow<MessageDto> = callbackFlow {
        val wsUrl = baseUrl
            .replace("https://", "wss://")
            .replace("http://", "ws://")
            .trimEnd('/') + "/ws/$chatId"

        val request = Request.Builder().url(wsUrl).build()

        val listener = object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val envelope = gson.fromJson(text, JsonObject::class.java)
                    if (envelope.get("type")?.asString == "message") {
                        val msg = gson.fromJson(envelope.get("data"), MessageDto::class.java)
                        trySend(msg)
                    }
                    // poll_update events are currently dropped — see class doc.
                } catch (_: Exception) {
                    // malformed/irrelevant frame — ignore rather than crash the stream
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                close(t)
            }
        }

        socket = client.newWebSocket(request, listener)

        awaitClose {
            socket?.close(1000, "screen closed")
            socket = null
        }
    }

    fun close() {
        socket?.close(1000, "client closing")
        socket = null
    }
}
