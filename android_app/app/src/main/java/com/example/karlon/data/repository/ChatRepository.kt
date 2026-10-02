package com.example.karlon.data.repository

import android.content.Context
import android.net.Uri
import com.example.karlon.data.model.ChatDto
import com.example.karlon.data.model.HouseListingDto
import com.example.karlon.data.model.HouseSendRequest
import com.example.karlon.data.model.HouseSendResponse
import com.example.karlon.data.model.InvoiceCreateRequest
import com.example.karlon.data.model.InvoiceDto
import com.example.karlon.data.model.LocationOption
import com.example.karlon.data.model.MessageDto
import com.example.karlon.data.model.ReservationDto
import com.example.karlon.data.model.ReservationRequest
import com.example.karlon.data.model.SendMessageRequest
import com.example.karlon.data.remote.ApiService
import com.example.karlon.data.remote.ChatWebSocketClient
import com.example.karlon.data.remote.RetrofitProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.io.FileOutputStream

/**
 * Single source of truth for chat data. [observeChat] now uses the
 * server's actual pub/sub channel (WS /ws/{chatId} — see
 * app/routers/websocket.py + ws_manager.py on the server) as the
 * primary real-time path: this phone, the web UI, another phone, and
 * wa_bridge.py on the PC all publish into the same HF Space hub, and
 * every message insert broadcasts to every open socket on that chat
 * within milliseconds — genuine push, not a polling illusion.
 *
 * A slow REST poll runs alongside it purely as a safety net: if a
 * network path doesn't support the WebSocket Upgrade handshake (some
 * corporate proxies, rare carrier NAT setups), messages still arrive,
 * just capped at [FALLBACK_POLL_INTERVAL_MS] latency instead of
 * near-instant. Both paths de-dupe against the same [seenIds] set, so
 * whichever gets a message first "wins" and the other silently no-ops.
 */
class ChatRepository(
    private val baseUrl: String,
    private val context: Context,
) {
    private val api: ApiService = RetrofitProvider.buildApiService(baseUrl)

    suspend fun checkServerReachable(): Boolean = withContext(Dispatchers.IO) {
        try {
            api.health().isSuccessful
        } catch (_: Exception) {
            false
        }
    }

    suspend fun getChats(): List<ChatDto> = withContext(Dispatchers.IO) { api.getChats() }

    suspend fun getMessages(chatId: String): List<MessageDto> =
        withContext(Dispatchers.IO) { api.getMessages(chatId) }

    suspend fun sendText(chatId: String, sender: String, text: String): MessageDto =
        withContext(Dispatchers.IO) {
            api.sendMessage(chatId, SendMessageRequest(sender = sender, text = text))
        }

    /** Copies the picked gallery image into a temp file (content:// Uris
     * aren't directly readable by OkHttp's MultipartBody) and uploads it
     * — the outbound half of the text<->image parser on the client side. */
    suspend fun sendImage(chatId: String, sender: String, caption: String, imageUri: Uri): MessageDto =
        withContext(Dispatchers.IO) {
            val tempFile = File.createTempFile("karlon_upload", ".jpg", context.cacheDir)
            context.contentResolver.openInputStream(imageUri).use { input ->
                FileOutputStream(tempFile).use { output ->
                    input?.copyTo(output)
                }
            }
            val requestFile = tempFile.asRequestBody("image/*".toMediaTypeOrNull())
            val filePart = MultipartBody.Part.createFormData("file", tempFile.name, requestFile)
            val senderPart = sender.toRequestBody("text/plain".toMediaTypeOrNull())
            val captionPart = caption.toRequestBody("text/plain".toMediaTypeOrNull())

            val result = api.sendImage(chatId, senderPart, captionPart, filePart)
            tempFile.delete()
            result
        }

    /** Sends the KARLCON Elite Retreats Terms & Conditions PDF to this chat.
     * No file upload involved — the server already holds the static PDF and
     * just queues a 'document' message pointing at it (see ApiService). */
    suspend fun sendTerms(chatId: String): MessageDto =
        withContext(Dispatchers.IO) { api.sendTerms(chatId) }

    /** "Live" stream of new messages for one chat thread: WebSocket
     * pub/sub as the primary path, REST polling as a fallback safety
     * net. Cancels both cleanly when the collecting coroutine is
     * cancelled (screen closed). */
    fun observeChat(chatId: String): Flow<MessageDto> = callbackFlow {
        val seenIds = HashSet<String>()

        // 1. Load existing history once via REST so the screen has
        //    something to show immediately, before either live path
        //    delivers anything new.
        try {
            val initial = withContext(Dispatchers.IO) { api.getMessages(chatId) }
            initial.forEach { seenIds.add(it.id) }
        } catch (_: Exception) {
            // No history yet, or server briefly unreachable — the
            // fallback poll loop below will retry.
        }

        // 2. Primary path: subscribe to the server's pub/sub channel.
        //    wa_bridge.py, the web UI, or another phone publishing a
        //    message shows up here in real time.
        val wsClient = ChatWebSocketClient(RetrofitProvider.buildOkHttpClient(), baseUrl)
        val wsJob = launch {
            wsClient.observe(chatId)
                .catch { /* socket dropped — fallback poll loop covers us */ }
                .collect { message ->
                    if (seenIds.add(message.id)) trySend(message)
                }
        }

        // 3. Safety net: slow REST poll in case the WebSocket Upgrade
        //    never completes on some network path. De-dupes against the
        //    same seenIds set, so this is a no-op whenever the socket
        //    is doing its job.
        val pollJob = launch {
            while (isActive) {
                delay(FALLBACK_POLL_INTERVAL_MS)
                try {
                    val messages = withContext(Dispatchers.IO) { api.getMessages(chatId) }
                    for (message in messages) {
                        if (seenIds.add(message.id)) trySend(message)
                    }
                } catch (_: Exception) {
                    // Try again next tick.
                }
            }
        }

        awaitClose {
            wsClient.close()
            wsJob.cancel()
            pollJob.cancel()
        }
    }

    suspend fun getInvoiceLocations(): List<LocationOption> =
        withContext(Dispatchers.IO) { api.getInvoiceLocations() }

    suspend fun getInvoices(): List<InvoiceDto> =
        withContext(Dispatchers.IO) { api.getInvoices() }

    suspend fun createInvoice(body: InvoiceCreateRequest): InvoiceDto =
        withContext(Dispatchers.IO) { api.createInvoice(body) }

    /** Fetches up to 6 freshly-scraped listings for the house-icon popup.
     * checkIn/checkOut (YYYY-MM-DD) narrow the search to listings priced for
     * those exact dates; leave null to see the freshest scrape regardless. */
    suspend fun getAvailableHouses(
        location: String,
        checkIn: String? = null,
        checkOut: String? = null,
        limit: Int = 6,
    ): List<HouseListingDto> =
        withContext(Dispatchers.IO) { api.getAvailableHouses(location, checkIn, checkOut, limit) }

    suspend fun getHouseLocations(): List<String> =
        withContext(Dispatchers.IO) { api.getHouseLocations() }

    /** Sends the chosen listings' photos + price/title/link to this chat over
     * WhatsApp — same outbox/wa_bridge delivery path as sendTerms(). */
    suspend fun sendHouses(chatId: String, listingIds: List<String>, sender: String): HouseSendResponse =
        withContext(Dispatchers.IO) { api.sendHouses(HouseSendRequest(chatId, listingIds, sender)) }

    /** Reserve button: queues the booking for the PC's Airbnb session. */
    suspend fun createReservation(body: ReservationRequest): ReservationDto =
        withContext(Dispatchers.IO) { api.createReservation(body) }

    suspend fun getReservation(id: Long): ReservationDto =
        withContext(Dispatchers.IO) { api.getReservation(id) }

    suspend fun cancelReservation(id: Long): ReservationDto =
        withContext(Dispatchers.IO) { api.cancelReservation(id) }

    fun mediaUrl(path: String): String =
        if (path.startsWith("http")) path else baseUrl.trimEnd('/') + path

    fun close() {
        // Per-chat sockets are opened and torn down inside observeChat's
        // awaitClose now; nothing global to close here. Kept so
        // ChatDetailViewModel's onCleared() call site doesn't need to change.
    }

    companion object {
        /** Safety-net cadence only — the WebSocket is the real-time
         * path. Kept well above the old 3s polling interval since it's
         * now a fallback, not the primary mechanism. */
        private const val FALLBACK_POLL_INTERVAL_MS = 15_000L
    }
}
