package com.example.karlon.data.remote

import com.example.karlon.data.model.ChatDto
import com.example.karlon.data.model.HealthResponse
import com.example.karlon.data.model.HouseListingDto
import com.example.karlon.data.model.HouseSendRequest
import com.example.karlon.data.model.HouseSendResponse
import com.example.karlon.data.model.InvoiceCreateRequest
import com.example.karlon.data.model.InvoiceDto
import com.example.karlon.data.model.LocationOption
import com.example.karlon.data.model.LocationRequest
import com.example.karlon.data.model.MessageDto
import com.example.karlon.data.model.ReservationDto
import com.example.karlon.data.model.ReservationRequest
import com.example.karlon.data.model.SendMessageRequest
import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Path
import retrofit2.http.Query

/** REST surface — one function per Karlon endpoint the app actually uses.
 *  See server/app/routers/ for the FastAPI side of each of these. */
interface ApiService {

    @GET("api/health")
    suspend fun health(): Response<HealthResponse>

    @GET("api/chats")
    suspend fun getChats(): List<ChatDto>

    @GET("api/chats/{chatId}/messages")
    suspend fun getMessages(@Path("chatId") chatId: String): List<MessageDto>

    @POST("api/chats/{chatId}/messages")
    suspend fun sendMessage(
        @Path("chatId") chatId: String,
        @Body body: SendMessageRequest,
    ): MessageDto

    @Multipart
    @POST("api/chats/{chatId}/images")
    suspend fun sendImage(
        @Path("chatId") chatId: String,
        @Part("sender") sender: RequestBody,
        @Part("caption") caption: RequestBody,
        @Part file: MultipartBody.Part,
    ): MessageDto

    /** Queues the static Terms & Conditions PDF as a 'document' message for
     * this chat — rides the same outbox/wa_bridge path as invoice PDFs.
     * See server/app/routers/messages.py: send_terms(). */
    @POST("api/chats/{chatId}/send-terms")
    suspend fun sendTerms(@Path("chatId") chatId: String): MessageDto

    /** See server/app/routers/team_location.py. */
    @POST("api/team/location")
    suspend fun sendLocation(@Body body: LocationRequest): Response<Unit>

    /** See server/app/routers/invoices.py. */
    @GET("api/invoices/locations")
    suspend fun getInvoiceLocations(): List<LocationOption>

    @GET("api/invoices")
    suspend fun getInvoices(): List<InvoiceDto>

    @POST("api/invoices")
    suspend fun createInvoice(@Body body: InvoiceCreateRequest): InvoiceDto

    /** Up to `limit` (max 6) of the freshest scraped listings for a location —
     * powers the house-icon popup's auto-populated grid. Pass checkIn/checkOut
     * (YYYY-MM-DD) to only show listings actually priced for those exact
     * dates; omit either to fall back to the freshest scrape regardless of
     * dates. See server/app/routers/houses.py: available_listings(). */
    @GET("api/houses/available")
    suspend fun getAvailableHouses(
        @Query("location") location: String,
        @Query("check_in") checkIn: String? = null,
        @Query("check_out") checkOut: String? = null,
        @Query("limit") limit: Int = 6,
        /** "ref" = KCER order (KCER 101 first), "newest" = latest scraped first. */
        @Query("sort") sort: String = "newest",
        /** Invoice form: put the houses already sent to this chat first. */
        @Query("chat_id") chatId: String? = null,
    ): List<HouseListingDto>

    /** The houses already sent to a chat, latest first. */
    @GET("api/houses/sent")
    suspend fun getSentHouses(@Query("chat_id") chatId: String): List<HouseListingDto>

    /** Distinct locations the scraper has pushed listings for so far. */
    @GET("api/houses/locations")
    suspend fun getHouseLocations(): List<String>

    /** Queues the chosen listings' photos + a price/title/link caption as
     * outbound WhatsApp messages — rides the same outbox/wa_bridge path as
     * invoices and Terms & Conditions. See routers/houses.py: send_listings(). */
    @POST("api/houses/send")
    suspend fun sendHouses(@Body body: HouseSendRequest): HouseSendResponse

    /** Queues a booking; airbnb_parallel_system.py on the PC picks it up
     * (server/app/routers/reservations.py). 409 = already queued/booked. */
    @POST("api/reservations")
    suspend fun createReservation(@Body body: ReservationRequest): ReservationDto

    @GET("api/reservations/{id}")
    suspend fun getReservation(@Path("id") id: Long): ReservationDto

    @POST("api/reservations/{id}/cancel")
    suspend fun cancelReservation(@Path("id") id: Long): ReservationDto
}
