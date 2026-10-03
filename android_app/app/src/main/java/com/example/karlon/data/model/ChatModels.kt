package com.example.karlon.data.model

import com.google.gson.annotations.SerializedName

/**
 * Wire-format DTOs — field names/types stay in sync with
 * server/app/models.py's serialize_message() / list_chats() output.
 */

data class ChatDto(
    val id: String,
    val name: String,
    @SerializedName("avatar_emoji") val avatarEmoji: String?,
    @SerializedName("profile_pic_url") val profilePicUrl: String?,
    @SerializedName("last_text") val lastText: String?,
    @SerializedName("last_at") val lastAt: String?,
    @SerializedName("wa_name") val waName: String? = null,
    /** Unread message count for this chat, if the server reports one.
     * Drives the glowing green ring around the avatar in the chat list —
     * ring is hidden whenever this is 0 or absent. */
    @SerializedName("unread_count") val unreadCount: Int? = 0,
    /** True when this chat's WhatsApp name is a raw, un-saved phone number
     * rather than a contact name. Drives a small green dot next to the name
     * in the chat list — replaces the old server-side "📱 " prefix glued
     * onto the display name. */
    @SerializedName("is_unsaved") val isUnsaved: Boolean = false,
)

data class PollOptionDto(
    val id: String,
    val text: String,
    val votes: Int,
)

data class PollDto(
    val id: String,
    val question: String,
    val options: List<PollOptionDto>,
)

data class MessageDto(
    val id: String,
    @SerializedName("chat_id") val chatId: String,
    val sender: String,
    val kind: String,               // "text" | "image" | "poll"
    val text: String?,
    @SerializedName("poll_id") val pollId: String?,
    @SerializedName("created_at") val createdAt: String,
    @SerializedName("external_key") val externalKey: String?,
    @SerializedName("media_url") val mediaUrl: String?,
    @SerializedName("media_type") val mediaType: String?,
    val poll: PollDto? = null,
    /**
     * "in"  = arrived from WhatsApp (scraped by wa_bridge)
     * "out" = sent from the Karlon app / web UI
     * null  = legacy rows before the direction column was added
     */
    val direction: String? = null,
    /**
     * WhatsApp delivery state for outbound (direction="out") messages.
     *   "pending" → queued, not yet sent to WhatsApp
     *   "sent"    → delivered via DOM automation  ✓✓
     *   "error"   → could not deliver              ⚠
     *   null      = incoming message (no WA delivery needed)
     */
    @SerializedName("wa_status") val waStatus: String? = null,
) {
    val isImage: Boolean  get() = kind == "image"
    val isText: Boolean   get() = kind == "text"
    val isPoll: Boolean   get() = kind == "poll"
    /** True when this message was authored here and needs to reach WA. */
    val isOutbound: Boolean get() = direction == "out"
}

data class SendMessageRequest(
    val sender: String,
    val text: String,
)

/** Wire body for POST /api/team/location — see server/app/models.py's
 * LocationIn. member_id should be stable per-device (e.g. ANDROID_ID),
 * not the display name, so the server can upsert by device rather than
 * accumulating duplicate rows if two people share a display name. */
data class LocationRequest(
    val member_id: String,
    val member_name: String,
    val latitude: Double,
    val longitude: Double,
    val accuracy_m: Float?,
)

/** Envelope pushed down the WebSocket — mirrors ws_manager.py's broadcast payload. */
data class WsEnvelope(
    val type: String,
    val data: com.google.gson.JsonObject,
)

data class HealthResponse(
    val status: String,
    val service: String,
)

// ── Invoices ─────────────────────────────────────────────────────────────
// Mirrors server/app/models.py's InvoiceCreateIn + routers/invoices.py's
// row serialization.

data class PropertyOption(
    val name: String,
    val rate: Double,
)

data class LocationOption(
    val location: String,
    val properties: List<PropertyOption>,
)

data class InvoiceCreateRequest(
    @SerializedName("guest_name") val guestName: String,
    @SerializedName("id_number") val idNumber: String,
    val location: String,
    @SerializedName("property_name") val propertyName: String,
    @SerializedName("check_in") val checkIn: String? = null,
    @SerializedName("check_out") val checkOut: String? = null,
    val nights: Int = 1,
    val rate: Double = 0.0,
    val currency: String = "USD",
    @SerializedName("created_by") val createdBy: String,
    @SerializedName("chat_id") val chatId: String? = null,
    /** Printed on the invoice ("2 Guests"). */
    val guests: Int = 1,
    /** The house picked from the property list: links the invoice to its
     * listing (photo, place, size) and to the offer that was sent. */
    @SerializedName("listing_url") val listingUrl: String? = null,
    @SerializedName("listing_offer_id") val listingOfferId: String? = null,
)

// ── Houses (Airbnb scraper -> server -> app "available now" popup) ────────
// Mirrors server/app/models.py's HouseListingIn/HouseSendIn + routers/houses.py.

data class HouseListingDto(
    val id: String,
    val url: String,
    val title: String?,
    val location: String,
    @SerializedName("price_raw") val priceRaw: String?,
    @SerializedName("price_usd_per_night") val priceUsdPerNight: Double?,
    @SerializedName("check_in") val checkIn: String?,
    @SerializedName("check_out") val checkOut: String?,
    val images: List<String> = emptyList(),
    val lat: Double?,
    val lng: Double?,
    @SerializedName("updated_at") val updatedAt: String,
    /** "KCER 101" … — the name guests see instead of the Airbnb title. */
    @SerializedName("ref_code") val refCode: String? = null,
    /** The scraped price for exactly checkIn→checkOut; sent with a
     * reservation so the PC can check Airbnb's total against it. */
    @SerializedName("offer_id") val offerId: String? = null,
    val nights: Int? = null,
    /** Suburb ("Hatfield, Harare"), guests/bedrooms line and rating, as
     * sent in the WhatsApp listing message. */
    val neighbourhood: String? = null,
    val capacity: String? = null,
    val rating: String? = null,
    /** The server now names listings "KCER 248 · Greendale, Harare" in [title];
     * the Airbnb title (staff only) is kept here. */
    @SerializedName("airbnb_title") val airbnbTitle: String? = null,
    /** true = this house was already sent to the chat the list was asked for
     * (invoice form), with the dates and price it was sent at. */
    @SerializedName("sent_to_chat") val sentToChat: Boolean = false,
) {
    val displayPrice: String
        get() = priceUsdPerNight?.let { "$${it.toInt()}/night" } ?: (priceRaw ?: "Price on request")

    val displayName: String
        get() = refCode ?: title ?: location

    /** Quoted stay total, when the price was scraped for these dates. */
    val quotedTotalUsd: Double?
        get() = if (priceUsdPerNight != null && nights != null && nights > 0) priceUsdPerNight * nights else null
}

/** Body of POST /api/reservations (server/app/routers/reservations.py). */
data class ReservationRequest(
    @SerializedName("listing_id") val listingId: String,
    @SerializedName("chat_id") val chatId: String?,
    @SerializedName("offer_id") val offerId: String?,
    @SerializedName("check_in") val checkIn: String,
    @SerializedName("check_out") val checkOut: String,
    val guests: Int,
    val message: String? = null,
    @SerializedName("created_by") val createdBy: String? = null,
)

data class ReservationDto(
    val id: Long,
    @SerializedName("chat_id") val chatId: String?,
    @SerializedName("ref_code") val refCode: String?,
    @SerializedName("check_in") val checkIn: String,
    @SerializedName("check_out") val checkOut: String,
    val nights: Int,
    val guests: Int,
    @SerializedName("expected_total_usd") val expectedTotalUsd: Double?,
    @SerializedName("total_usd") val totalUsd: Double?,
    /** pending → in_progress (the PC is on Airbnb) → requested (sent to the
     * host; the guest got a WhatsApp) | dry_run (test mode, nothing booked) |
     * failed (nothing booked, can retry) | unknown (check Airbnb Trips) |
     * cancelled */
    val status: String,
    @SerializedName("trip_url") val tripUrl: String?,
    @SerializedName("error_message") val errorMessage: String?,
    @SerializedName("created_at") val createdAt: String,
) {
    val isFinished: Boolean
        get() = status !in setOf("pending", "in_progress")
}

data class HouseSendRequest(
    @SerializedName("chat_id") val chatId: String,
    @SerializedName("listing_ids") val listingIds: List<String>,
    val sender: String = "Front Desk",
    /** Parallel to listingIds: the dated offer each pick was shown with, so
     * the WhatsApp message and the invoice quote the same dates and price. */
    @SerializedName("offer_ids") val offerIds: List<String?> = emptyList(),
)

data class HouseSendResponse(
    val queued: Int,
    val messages: List<MessageDto>,
)

data class InvoiceDto(
    val id: String,
    @SerializedName("chat_id") val chatId: String?,
    @SerializedName("chat_name") val chatName: String? = null,
    @SerializedName("guest_name") val guestName: String,
    @SerializedName("id_number") val idNumber: String,
    val location: String,
    @SerializedName("property_name") val propertyName: String,
    @SerializedName("check_in") val checkIn: String?,
    @SerializedName("check_out") val checkOut: String?,
    val nights: Int,
    val rate: Double,
    val total: Double,
    val currency: String,
    /** "pending" (queued for invoice_worker.py) | "processing" |
     *  "ready" (PDF generated, queued to WhatsApp if chat_id set) | "error" */
    val status: String,
    @SerializedName("pdf_url") val pdfUrl: String?,
    @SerializedName("error_message") val errorMessage: String?,
    @SerializedName("created_at") val createdAt: String,
)
