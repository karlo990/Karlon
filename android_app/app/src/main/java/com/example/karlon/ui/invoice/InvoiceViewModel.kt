package com.example.karlon.ui.invoice

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.karlon.data.model.ChatDto
import com.example.karlon.data.model.HouseListingDto
import com.example.karlon.data.model.InvoiceCreateRequest
import com.example.karlon.data.model.InvoiceDto
import com.example.karlon.data.model.LocationOption
import com.example.karlon.data.model.PropertyOption
import com.example.karlon.data.repository.ChatRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/** Everything the form needs to know before the guest name/ID are even typed. */
data class InvoiceCatalogue(
    val locations: List<LocationOption> = emptyList(),
    val chats: List<ChatDto> = emptyList(),
)

/** A selectable entry in the Property dropdown — either a configured
 * property (fixed rate from the server's location/property catalogue) or a
 * currently-scraped Airbnb listing for the same location (rate/title come
 * straight off the listing, so picking one prefills the invoice with real
 * live pricing instead of a static rate card). */
data class PropertyChoice(
    val label: String,
    val rate: Double,
    val fromListing: HouseListingDto? = null,
) {
    val asPropertyOption: PropertyOption
        get() = PropertyOption(name = label, rate = rate)
}

data class InvoiceFormState(
    val guestName: String = "",
    val idNumber: String = "",
    val location: String? = null,
    val property: PropertyOption? = null,
    /** The house picked from the live/sent list (null for a rate-card
     * property): sent with the invoice so it links to the listing. */
    val listing: HouseListingDto? = null,
    val checkIn: String = "",
    val checkOut: String = "",
    val nights: Int = 1,
    val guests: Int = 1,
    val rateOverride: String = "",
    /** null = generate the PDF only; set = also queue it onto that chat's
     * WhatsApp outbox once invoice_worker.py finishes rendering it. */
    val sendToChat: ChatDto? = null,
    /** True when [sendToChat] was picked automatically by matching the guest
     * name against known contacts, rather than chosen by hand in the
     * dropdown. Lets typing continue to refine the auto-match, while a
     * manual pick (or manually turning the toggle off) locks it in and
     * stops auto-matching from overriding the front desk's own choice. */
    val sendToChatAutoMatched: Boolean = false,
    val submitting: Boolean = false,
    val submitError: String? = null,
    val lastSubmitted: InvoiceDto? = null,
) {
    val rate: Double
        get() = rateOverride.toDoubleOrNull() ?: property?.rate ?: 0.0

    val total: Double
        get() = rate * nights.coerceAtLeast(1)

    /** Guest name, ID, a location+property pair, and at least one night — the
     * floor for what an invoice needs before it's worth sending to the server. */
    val canSubmit: Boolean
        get() = guestName.isNotBlank() && idNumber.isNotBlank() &&
            location != null && property != null && nights >= 1 && !submitting
}

sealed interface InvoiceHistoryState {
    data object Loading : InvoiceHistoryState
    data class Loaded(val invoices: List<InvoiceDto>) : InvoiceHistoryState
    data class Error(val message: String) : InvoiceHistoryState
}

class InvoiceViewModel(
    private val repository: ChatRepository,
    private val displayName: String,
    /** Set when opened via the 💵 button on a chat thread — prefills the
     * guest name from the chat and locks "send to WhatsApp" onto that chat
     * (a deliberate, explicit pick, not an auto-match) rather than making
     * the front desk re-select it from the dropdown. */
    private val prefillFromChat: ChatDto? = null,
) : ViewModel() {

    private val _catalogue = MutableStateFlow(InvoiceCatalogue())
    val catalogue: StateFlow<InvoiceCatalogue> = _catalogue

    private val _form = MutableStateFlow(
        if (prefillFromChat != null) {
            InvoiceFormState(
                guestName = prefillFromChat.name,
                sendToChat = prefillFromChat,
                sendToChatAutoMatched = false,
            )
        } else InvoiceFormState()
    )
    val form: StateFlow<InvoiceFormState> = _form

    private val _history = MutableStateFlow<InvoiceHistoryState>(InvoiceHistoryState.Loading)
    val history: StateFlow<InvoiceHistoryState> = _history

    /** Live-scraped listings for the currently-selected location, folded
     * into the Property dropdown alongside the fixed rate-card entries. */
    private val _houseListings = MutableStateFlow<List<HouseListingDto>>(emptyList())
    val houseListings: StateFlow<List<HouseListingDto>> = _houseListings

    init {
        loadCatalogue()
        loadHistory()
        loadSentOptions()
    }

    /** Opened from a chat: the houses already sent to that guest fill the
     * Property list (first, flagged "Sent to this guest") and their city is
     * preselected, so the invoice is one tap away. */
    private fun loadSentOptions() {
        val chat = prefillFromChat ?: return
        viewModelScope.launch {
            val sent = try { repository.getSentHouses(chat.id) } catch (_: Exception) { emptyList() }
            val first = sent.firstOrNull() ?: return@launch
            val city = first.location.split(" ").joinToString(" ") { w -> w.replaceFirstChar { it.uppercase() } }
            _form.value = _form.value.copy(location = city, property = null, listing = null)
            _houseListings.value = try {
                repository.getAvailableHouses(location = city, limit = 12, chatId = chat.id)
            } catch (_: Exception) {
                sent
            }
        }
    }

    private fun loadCatalogue() {
        viewModelScope.launch {
            val locations = try {
                repository.getInvoiceLocations()
            } catch (_: Exception) {
                emptyList()
            }
            val chats = try {
                repository.getChats()
            } catch (_: Exception) {
                emptyList()
            }
            // Non-fatal on either failure — the form still works standalone
            // (no WhatsApp auto-send) if chats or the catalogue can't load.
            _catalogue.value = InvoiceCatalogue(locations = locations, chats = chats)
        }
    }

    fun loadHistory() {
        viewModelScope.launch {
            _history.value = InvoiceHistoryState.Loading
            try {
                _history.value = InvoiceHistoryState.Loaded(repository.getInvoices())
            } catch (e: Exception) {
                _history.value = InvoiceHistoryState.Error(e.message ?: "Couldn't load invoices")
            }
        }
    }

    fun onGuestNameChange(v: String) {
        val current = _form.value
        // Only let auto-matching touch sendToChat while nothing's been
        // manually picked yet — either it's still empty, or the current
        // value was itself an auto-match from an earlier keystroke.
        val shouldAutoMatch = current.sendToChat == null || current.sendToChatAutoMatched
        _form.value = if (shouldAutoMatch) {
            val match = findConfidentChatMatch(v, _catalogue.value.chats)
            current.copy(guestName = v, sendToChat = match, sendToChatAutoMatched = match != null)
        } else {
            current.copy(guestName = v)
        }
    }
    fun onIdNumberChange(v: String) { _form.value = _form.value.copy(idNumber = v) }
    fun onCheckInChange(v: String) { _form.value = _form.value.copy(checkIn = v) }
    fun onCheckOutChange(v: String) { _form.value = _form.value.copy(checkOut = v) }
    fun onRateOverrideChange(v: String) { _form.value = _form.value.copy(rateOverride = v) }

    fun onGuestsChange(v: Int) {
        _form.value = _form.value.copy(guests = v.coerceIn(1, 30))
    }

    fun onNightsChange(v: Int) {
        _form.value = _form.value.copy(nights = v.coerceAtLeast(1))
    }

    /** Picking a location resets the property (its list of options just
     * changed) and re-fetches the freshest scraped listings for that city
     * so they can appear alongside the fixed-rate properties. */
    fun onLocationChange(location: String) {
        _form.value = _form.value.copy(location = location, property = null)
        _houseListings.value = emptyList()
        viewModelScope.launch {
            _houseListings.value = try {
                repository.getAvailableHouses(location = location, limit = 12, chatId = prefillFromChat?.id)
            } catch (_: Exception) {
                emptyList()
            }
        }
    }

    fun onPropertyChange(property: PropertyOption) {
        _form.value = _form.value.copy(property = property, listing = null)
    }

    /** Selecting a scraped listing from the Property dropdown — same as
     * picking a normal property, but the rate/name come straight off that
     * listing instead of the static rate card, and the dates it was priced
     * for are copied in too when the invoice's own dates are still blank. */
    fun onListingChosenAsProperty(listing: HouseListingDto) {
        val current = _form.value
        // A house that was sent to this guest brings the dates and price it
        // was offered at, replacing any earlier pick's dates.
        val useDates = listing.sentToChat || current.checkIn.isBlank() || current.checkOut.isBlank()
        _form.value = current.copy(
            property = PropertyOption(
                name = listing.refCode ?: listing.title?.takeIf { it.isNotBlank() } ?: listing.url,
                rate = listing.priceUsdPerNight ?: 0.0,
            ),
            listing = listing,
            checkIn = if (useDates) listing.checkIn ?: current.checkIn else current.checkIn,
            checkOut = if (useDates) listing.checkOut ?: current.checkOut else current.checkOut,
        )
    }

    /** Picking a past guest from the suggestion list under the name field —
     * fills in everything that invoice recorded (ID, location, property,
     * rate) so a returning guest's repeat booking doesn't need retyping. */
    fun onGuestSuggestionSelect(invoice: InvoiceDto) {
        _form.value = _form.value.copy(
            guestName = invoice.guestName,
            idNumber = invoice.idNumber,
            location = invoice.location,
            property = PropertyOption(name = invoice.propertyName, rate = invoice.rate),
            sendToChatAutoMatched = false,
        )
        onLocationChange(invoice.location)
        // onLocationChange resets property to null (location just "changed"
        // as far as the form is concerned) — restore it after.
        _form.value = _form.value.copy(
            property = PropertyOption(name = invoice.propertyName, rate = invoice.rate),
        )
    }

    /** Past guests whose saved name loosely matches what's typed so far —
     * powers the suggestion list under the guest name field. Reuses the
     * same relaxed token-overlap algorithm as chat auto-matching. */
    fun guestSuggestions(typed: String, invoices: List<InvoiceDto>): List<InvoiceDto> {
        val typedTokens = nameTokens(typed)
        if (typedTokens.isEmpty()) return emptyList()
        return invoices
            .distinctBy { it.guestName.lowercase().trim() }
            .filter { inv ->
                val invTokens = nameTokens(inv.guestName)
                invTokens.isNotEmpty() && typedTokens.all { t -> invTokens.any { it.startsWith(t) } }
            }
            .take(5)
    }

    fun onSendToChatChange(chat: ChatDto?) {
        // A manual pick (or manually flipping the toggle off, chat = null)
        // is a deliberate decision by the front desk — lock it in so it
        // doesn't get silently overwritten by auto-match on the next
        // keystroke in the guest name field.
        _form.value = _form.value.copy(sendToChat = chat, sendToChatAutoMatched = false)
    }

    fun dismissError() {
        _form.value = _form.value.copy(submitError = null)
    }

    fun clearLastSubmitted() {
        _form.value = _form.value.copy(lastSubmitted = null)
    }

    fun submit() {
        val f = _form.value
        if (!f.canSubmit) return
        val location = f.location ?: return
        val property = f.property ?: return

        _form.value = f.copy(submitting = true, submitError = null)
        viewModelScope.launch {
            try {
                val created = repository.createInvoice(
                    InvoiceCreateRequest(
                        guestName = f.guestName.trim(),
                        idNumber = f.idNumber.trim(),
                        location = location,
                        propertyName = property.name,
                        checkIn = f.checkIn.trim().ifBlank { null },
                        checkOut = f.checkOut.trim().ifBlank { null },
                        nights = f.nights,
                        guests = f.guests,
                        listingUrl = f.listing?.url,
                        listingOfferId = f.listing?.takeIf { it.checkIn == f.checkIn.trim() && it.checkOut == f.checkOut.trim() }
                            ?.offerId,
                        rate = f.rate,
                        createdBy = displayName,
                        chatId = f.sendToChat?.id,
                    ),
                )
                // Reset to a clean form but keep the location/property/chat
                // picked, since the same front-desk session often books the
                // same property back-to-back for different guests.
                _form.value = InvoiceFormState(
                    location = location,
                    property = property,
                    listing = f.listing,
                    sendToChat = f.sendToChat,
                    lastSubmitted = created,
                )
                loadHistory()
            } catch (e: Exception) {
                _form.value = _form.value.copy(
                    submitting = false,
                    submitError = e.message ?: "Couldn't create invoice",
                )
            }
        }
    }

    /**
     * Finds a contact whose saved name confidently matches the typed guest
     * name — used to auto-select "send to chat" as the front desk types,
     * instead of making them hunt through the dropdown by hand.
     *
     * Deliberately conservative: unsaved numbers (no real name to match
     * against) are excluded, at least two name tokens must be typed before
     * guessing, a match requires every token on the shorter side to appear
     * in the longer side, and an ambiguous result (more than one contact
     * matches) returns null rather than guessing — a misdirected invoice is
     * worse than one the front desk has to pick manually.
     */
    private fun findConfidentChatMatch(guestName: String, chats: List<ChatDto>): ChatDto? {
        val guestTokens = nameTokens(guestName)
        if (guestTokens.size < 2) return null

        val candidates = chats.filter { !it.isUnsaved }.filter { chat ->
            val chatTokens = nameTokens(chat.name)
            if (chatTokens.isEmpty()) return@filter false
            val (shorter, longer) = if (chatTokens.size <= guestTokens.size) {
                chatTokens to guestTokens
            } else {
                guestTokens to chatTokens
            }
            shorter.all { it in longer }
        }
        return candidates.singleOrNull()
    }

    private fun nameTokens(name: String): List<String> =
        name.lowercase()
            .replace(Regex("[^a-z\\s]"), "")
            .split(Regex("\\s+"))
            .filter { it.length >= 2 }
}
