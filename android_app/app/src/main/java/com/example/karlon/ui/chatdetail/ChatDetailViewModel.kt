package com.example.karlon.ui.chatdetail

import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.karlon.data.model.HouseListingDto
import com.example.karlon.data.model.MessageDto
import com.example.karlon.data.repository.ChatRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class ChatDetailUiState(
    val messages: List<MessageDto> = emptyList(),
    val isLoading: Boolean = true,
    val isSending: Boolean = false,
    val errorMessage: String? = null,
)

/** Drives the house-icon popup: a location search box, up to 6
 * auto-populated available listings, a multi-select, and a Send action
 * that WhatsApps the picks (photos + price/title/link) to this chat. */
data class AvailableHousesUiState(
    val isVisible: Boolean = false,
    val location: String = "",
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
) {
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

    /** Opens the popup and, if a location is already known (or was typed
     * before), immediately fetches its up-to-6 fresh listings. */
    fun openHousesPopup(defaultLocation: String = "") {
        _housesUiState.update {
            AvailableHousesUiState(
                isVisible = true,
                location = defaultLocation,
            )
        }
        if (defaultLocation.isNotBlank()) loadHouses(defaultLocation)
    }

    fun dismissHousesPopup() {
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
                val listings = repository.getAvailableHouses(query, checkIn = ci, checkOut = co, limit = 6)
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

    override fun onCleared() {
        repository.close()
        super.onCleared()
    }
}
