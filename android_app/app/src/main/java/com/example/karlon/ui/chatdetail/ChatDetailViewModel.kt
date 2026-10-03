package com.example.karlon.ui.chatdetail

import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.karlon.data.model.HouseListingDto
import com.example.karlon.data.model.MessageDto
import com.example.karlon.data.model.ReservationDto
import com.example.karlon.data.model.ReservationRequest
import com.example.karlon.data.repository.ChatRepository
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONObject
import retrofit2.HttpException

data class ChatDetailUiState(
    val messages: List<MessageDto> = emptyList(),
    val isLoading: Boolean = true,
    val isSending: Boolean = false,
    val errorMessage: String? = null,
)

/** Drives the house-icon popup: a location search box, up to 6
 * auto-populated available listings, a multi-select, and a Send action
 * that WhatsApps the picks (photos + price/title/link) to this chat. */
/** Offered in the location dropdown even before the scraper has houses
 * there; the server's own list (GET /api/houses/locations) is added to it. */
val DEFAULT_HOUSE_LOCATIONS = listOf(
    "Harare", "Bulawayo", "Victoria Falls", "Mutare", "Gweru", "Masvingo",
    "Kwekwe", "Kadoma", "Chinhoyi", "Marondera", "Bindura", "Kariba", "Nyanga",
)
const val DEFAULT_HOUSE_LOCATION = "Harare"
/** Dropdown entry that lists every city's houses together. */
const val ALL_HOUSE_LOCATIONS = "All cities"
/** How many houses one search shows: enough for KCER 101 to the latest. */
const val HOUSES_PAGE_SIZE = 150

data class AvailableHousesUiState(
    val isVisible: Boolean = false,
    val location: String = "",
    val locationOptions: List<String> = listOf(ALL_HOUSE_LOCATIONS) + DEFAULT_HOUSE_LOCATIONS,
    /** true = KCER order (KCER 101 first); false = newest scraped first. */
    val sortByRef: Boolean = true,
    /** Optional YYYY-MM-DD window — leave both blank to just see the
     * freshest scrape for the city; fill both in to narrow results to
     * listings actually priced for that exact window (see
     * server/app/routers/houses.py: available_listings()). */
    val checkIn: String = "",
    val checkOut: String = "",
    val isLoading: Boolean = false,
    val listings: List<HouseListingDto> = emptyList(),
    val selectedIds: Set<String> = emptySet(),
    val isSending: Boolean = false,
    val errorMessage: String? = null,
    val sendSuccess: Boolean = false,
    /** Reserve flow: confirm dialog → queued → PC books on Airbnb → result. */
    val reserveConfirmVisible: Boolean = false,
    val reserveGuests: Int = 2,
    val reserveMessage: String = "",
    val isReserving: Boolean = false,
    val reservation: ReservationDto? = null,
    val reserveError: String? = null,
) {
    /** Reserve needs exactly one house and its dates: from the search box,
     * else the dates the listing was priced for. */
    val reserveListing: HouseListingDto?
        get() = selectedIds.singleOrNull()?.let { id -> listings.firstOrNull { it.id == id } }

    val reserveCheckIn: String
        get() = checkIn.ifBlank { reserveListing?.checkIn.orEmpty() }

    val reserveCheckOut: String
        get() = checkOut.ifBlank { reserveListing?.checkOut.orEmpty() }

    val canReserve: Boolean
        get() = reserveListing != null && reserveCheckIn.isNotBlank() && reserveCheckOut.isNotBlank() &&
            !isReserving && reservation?.isFinished != false

    /** A location is required; dates are either both filled or both blank
     * — a half-filled date pair is ambiguous, so search stays disabled
     * until the person finishes or clears it (see the inline hint in
     * AvailableHousesDialog). */
    val canSearch: Boolean
        get() = location.isNotBlank() && checkIn.isBlank() == checkOut.isBlank()
}

/**
 * Loads history over REST, then keeps the list live by polling REST on
 * an interval (see ChatRepository.observeChat) for as long as this
 * screen is open — new text or image messages from ANY client (this
 * phone, the PC's own web UI, another phone) show up here within one
 * poll interval of the server having them.
 */
class ChatDetailViewModel(
    private val repository: ChatRepository,
    private val chatId: String,
    private val displayName: String,
) : ViewModel() {

    private val _uiState = MutableStateFlow(ChatDetailUiState())
    val uiState: StateFlow<ChatDetailUiState> = _uiState

    private val _housesUiState = MutableStateFlow(AvailableHousesUiState())
    val housesUiState: StateFlow<AvailableHousesUiState> = _housesUiState

    init {
        loadHistory()
        listenForLiveUpdates()
    }

    private fun loadHistory() {
        viewModelScope.launch {
            try {
                val history = repository.getMessages(chatId)
                _uiState.update { it.copy(messages = history, isLoading = false) }
            } catch (e: Exception) {
                _uiState.update { it.copy(isLoading = false, errorMessage = e.message ?: "Couldn't load messages") }
            }
        }
    }

    private fun listenForLiveUpdates() {
        viewModelScope.launch {
            repository.observeChat(chatId).collect { incoming ->
                _uiState.update { state ->
                    if (state.messages.any { it.id == incoming.id }) return@update state
                    state.copy(messages = state.messages + incoming)
                }
            }
        }
    }

    fun sendText(text: String) {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return
        viewModelScope.launch {
            _uiState.update { it.copy(isSending = true) }
            try {
                repository.sendText(chatId, displayName, trimmed)
                // The message also arrives back via the WebSocket broadcast,
                // deduped by id in listenForLiveUpdates — no local echo needed.
            } catch (e: Exception) {
                _uiState.update { it.copy(errorMessage = e.message ?: "Send failed") }
            } finally {
                _uiState.update { it.copy(isSending = false) }
            }
        }
    }

    fun sendImage(uri: Uri, caption: String = "") {
        viewModelScope.launch {
            _uiState.update { it.copy(isSending = true) }
            try {
                repository.sendImage(chatId, displayName, caption, uri)
            } catch (e: Exception) {
                _uiState.update { it.copy(errorMessage = e.message ?: "Image send failed") }
            } finally {
                _uiState.update { it.copy(isSending = false) }
            }
        }
    }

    /** Sends the fixed Terms & Conditions PDF — same outbox/wa_bridge
     * delivery path as an invoice PDF, just no per-booking generation step. */
    fun sendTerms() {
        viewModelScope.launch {
            _uiState.update { it.copy(isSending = true) }
            try {
                repository.sendTerms(chatId)
            } catch (e: Exception) {
                _uiState.update { it.copy(errorMessage = e.message ?: "Couldn't send Terms & Conditions") }
            } finally {
                _uiState.update { it.copy(isSending = false) }
            }
        }
    }

    fun resolvedMediaUrl(path: String?): String? = path?.let { repository.mediaUrl(it) }

    // ── Available-houses popup (house-icon button) ──────────────────────────

    /** Opens the popup on Harare (or [defaultLocation]) and immediately
     * fetches its up-to-6 fresh listings, then fills the location dropdown
     * with every place the server has houses for. */
    fun openHousesPopup(defaultLocation: String = DEFAULT_HOUSE_LOCATION) {
        val location = defaultLocation.ifBlank { DEFAULT_HOUSE_LOCATION }
        _housesUiState.update {
            AvailableHousesUiState(
                isVisible = true,
                location = location,
            )
        }
        loadHouses(location)
        viewModelScope.launch {
            val fromServer = try { repository.getHouseLocations() } catch (e: Exception) { emptyList() }
            val merged = (listOf(ALL_HOUSE_LOCATIONS) + DEFAULT_HOUSE_LOCATIONS + fromServer.map { loc ->
                loc.trim().split(" ").joinToString(" ") { w -> w.replaceFirstChar { it.uppercase() } }
            }).filter { it.isNotBlank() }.distinctBy { it.lowercase() }
            _housesUiState.update { it.copy(locationOptions = merged) }
        }
    }

    /** Both dates from the range picker at once (YYYY-MM-DD, or blank to
     * clear), then search. */
    fun onHousesDatesPicked(checkIn: String, checkOut: String) {
        _housesUiState.update { it.copy(checkIn = checkIn, checkOut = checkOut) }
        searchHouses()
    }

    fun onHousesSortChange(byRef: Boolean) {
        if (_housesUiState.value.sortByRef == byRef) return
        _housesUiState.update { it.copy(sortByRef = byRef) }
        searchHouses()
    }

    /** Picking a place from the dropdown searches it straight away. */
    fun onHousesLocationPicked(location: String) {
        _housesUiState.update { it.copy(location = location) }
        searchHouses()
    }

    fun dismissHousesPopup() {
        // A booking that's already queued keeps going on the PC; the guest
        // still gets the WhatsApp. Only the on-screen status stops.
        reservePollJob?.cancel()
        _housesUiState.value = AvailableHousesUiState()
    }

    fun onHousesLocationChange(location: String) {
        _housesUiState.update { it.copy(location = location) }
    }

    fun onHousesCheckInChange(checkIn: String) {
        _housesUiState.update { it.copy(checkIn = checkIn) }
    }

    fun onHousesCheckOutChange(checkOut: String) {
        _housesUiState.update { it.copy(checkOut = checkOut) }
    }

    /** Runs the search with whatever location/dates are currently in the
     * popup — this is what the search button calls. */
    fun searchHouses() {
        val state = _housesUiState.value
        if (!state.canSearch) return
        loadHouses(state.location, state.checkIn, state.checkOut)
    }

    /** Fetches up to 6 of the freshest scraped listings for [location] and
     * auto-populates the popup grid with them. When [checkIn]/[checkOut]
     * are both given, results are narrowed to listings actually priced for
     * that exact window; leave either blank to fall back to the freshest
     * scrape regardless of dates. */
    fun loadHouses(location: String, checkIn: String = "", checkOut: String = "") {
        val query = location.trim()
        if (query.isEmpty()) return
        val ci = checkIn.trim().ifBlank { null }
        val co = checkOut.trim().ifBlank { null }
        viewModelScope.launch {
            _housesUiState.update {
                it.copy(isLoading = true, errorMessage = null, sendSuccess = false, location = query)
            }
            try {
                val listings = repository.getAvailableHouses(
                    if (query.equals(ALL_HOUSE_LOCATIONS, ignoreCase = true)) "all" else query,
                    checkIn = ci, checkOut = co, limit = HOUSES_PAGE_SIZE,
                    sort = if (_housesUiState.value.sortByRef) "ref" else "newest",
                )
                _housesUiState.update {
                    it.copy(
                        isLoading = false,
                        listings = listings,
                        // Drop selections that no longer correspond to a listing on screen.
                        selectedIds = it.selectedIds.intersect(listings.map { l -> l.id }.toSet()),
                    )
                }
            } catch (e: Exception) {
                _housesUiState.update {
                    it.copy(isLoading = false, errorMessage = e.message ?: "Couldn't load available houses")
                }
            }
        }
    }

    fun toggleHouseSelection(listingId: String) {
        _housesUiState.update { state ->
            val next = state.selectedIds.toMutableSet()
            if (!next.add(listingId)) next.remove(listingId)
            state.copy(selectedIds = next)
        }
    }

    /** Sends every selected listing's photos + a price/title/link caption to
     * this chat over WhatsApp (see ChatRepository.sendHouses). */
    fun sendSelectedHouses() {
        val ids = _housesUiState.value.selectedIds.toList()
        if (ids.isEmpty()) return
        viewModelScope.launch {
            _housesUiState.update { it.copy(isSending = true, errorMessage = null) }
            try {
                repository.sendHouses(chatId, ids, displayName)
                _housesUiState.update {
                    it.copy(isSending = false, sendSuccess = true, selectedIds = emptySet())
                }
                // The queued messages arrive back through the normal WebSocket
                // broadcast (listenForLiveUpdates), same as any other send.
            } catch (e: Exception) {
                _housesUiState.update {
                    it.copy(isSending = false, errorMessage = e.message ?: "Couldn't send those listings")
                }
            }
        }
    }

    // ── Reserve (one selected house → Airbnb "Request to book" on the PC) ──

    private var reservePollJob: Job? = null

    fun openReserveConfirm() {
        if (!_housesUiState.value.canReserve) return
        _housesUiState.update { it.copy(reserveConfirmVisible = true, reserveError = null) }
    }

    fun dismissReserveConfirm() {
        _housesUiState.update { it.copy(reserveConfirmVisible = false) }
    }

    fun onReserveGuestsChange(guests: Int) {
        _housesUiState.update { it.copy(reserveGuests = guests.coerceIn(1, 16)) }
    }

    fun onReserveMessageChange(message: String) {
        _housesUiState.update { it.copy(reserveMessage = message) }
    }

    /** Queues the reservation; the PC (airbnb_parallel_system.py) opens the
     * listing, clicks Reserve, writes to the host and requests to book. When
     * it's done the server WhatsApps this chat "Your reservation has been
     * made… waiting for the host to share the live location". */
    fun confirmReservation() {
        val state = _housesUiState.value
        val listing = state.reserveListing ?: return
        if (!state.canReserve) return
        viewModelScope.launch {
            _housesUiState.update {
                it.copy(reserveConfirmVisible = false, isReserving = true, reserveError = null, reservation = null)
            }
            try {
                val created = repository.createReservation(
                    ReservationRequest(
                        listingId = listing.id,
                        chatId = chatId,
                        // The offer's price only applies to its own dates.
                        offerId = listing.offerId.takeIf {
                            listing.checkIn == state.reserveCheckIn && listing.checkOut == state.reserveCheckOut
                        },
                        checkIn = state.reserveCheckIn,
                        checkOut = state.reserveCheckOut,
                        guests = state.reserveGuests,
                        message = state.reserveMessage.trim().ifBlank { null },
                        createdBy = displayName,
                    )
                )
                _housesUiState.update { it.copy(isReserving = false, reservation = created) }
                pollReservation(created.id)
            } catch (e: Exception) {
                _housesUiState.update { it.copy(isReserving = false, reserveError = serverMessage(e)) }
            }
        }
    }

    /** Cancels a reservation the PC hasn't started yet. */
    fun cancelReservation() {
        val r = _housesUiState.value.reservation ?: return
        if (r.status != "pending") return
        viewModelScope.launch {
            try {
                val updated = repository.cancelReservation(r.id)
                reservePollJob?.cancel()
                _housesUiState.update { it.copy(reservation = updated) }
            } catch (e: Exception) {
                _housesUiState.update { it.copy(reserveError = serverMessage(e)) }
            }
        }
    }

    private fun pollReservation(id: Long) {
        reservePollJob?.cancel()
        reservePollJob = viewModelScope.launch {
            // Airbnb takes a minute or two; stop watching after ~15 min.
            repeat(180) {
                delay(5_000)
                val r = try { repository.getReservation(id) } catch (e: Exception) { null } ?: return@repeat
                _housesUiState.update { it.copy(reservation = r) }
                if (r.isFinished) return@launch
            }
        }
    }

    private fun serverMessage(e: Exception): String {
        if (e is HttpException) {
            val detail = try {
                e.response()?.errorBody()?.string()?.let { JSONObject(it).optString("detail") }
            } catch (ignored: Exception) { null }
            if (!detail.isNullOrBlank()) return detail
        }
        return e.message ?: "Couldn't queue the reservation"
    }

    override fun onCleared() {
        repository.close()
        super.onCleared()
    }
}
