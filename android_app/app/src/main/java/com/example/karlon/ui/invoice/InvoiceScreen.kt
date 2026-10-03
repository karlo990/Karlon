@file:OptIn(ExperimentalMaterial3Api::class)

package com.example.karlon.ui.invoice

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Remove
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.karlon.data.model.ChatDto
import com.example.karlon.data.model.InvoiceDto
import com.example.karlon.data.model.PropertyOption
import com.example.karlon.ui.components.AnimatedGlassCard
import com.example.karlon.ui.components.ShimmerHistoryRow
import com.example.karlon.ui.components.glassSurface
import com.example.karlon.ui.theme.KarlonError
import com.example.karlon.ui.theme.KarlonGold
import com.example.karlon.ui.theme.KarlonRingGlow

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InvoiceScreen(viewModel: InvoiceViewModel) {
    val form by viewModel.form.collectAsState()
    val catalogue by viewModel.catalogue.collectAsState()
    val history by viewModel.history.collectAsState()
    val houseListings by viewModel.houseListings.collectAsState()
    val pastInvoices = (history as? InvoiceHistoryState.Loaded)?.invoices.orEmpty()

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text("Invoices", fontWeight = FontWeight.Bold, fontSize = 22.sp,
                        color = MaterialTheme.colorScheme.onBackground)
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
            )
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 16.dp),
            contentPadding = PaddingValues(vertical = 16.dp),
        ) {
            item {
                InvoiceForm(
                    form = form,
                    locations = catalogue.locations,
                    chats = catalogue.chats,
                    houseListings = houseListings,
                    guestSuggestions = viewModel.guestSuggestions(form.guestName, pastInvoices),
                    onGuestNameChange = viewModel::onGuestNameChange,
                    onGuestSuggestionSelect = viewModel::onGuestSuggestionSelect,
                    onIdNumberChange = viewModel::onIdNumberChange,
                    onLocationChange = viewModel::onLocationChange,
                    onPropertyChange = viewModel::onPropertyChange,
                    onListingChosenAsProperty = viewModel::onListingChosenAsProperty,
                    onCheckInChange = viewModel::onCheckInChange,
                    onCheckOutChange = viewModel::onCheckOutChange,
                    onNightsChange = viewModel::onNightsChange,
                    onGuestsChange = viewModel::onGuestsChange,
                    onRateOverrideChange = viewModel::onRateOverrideChange,
                    onSendToChatChange = viewModel::onSendToChatChange,
                    onSubmit = viewModel::submit,
                )
                Spacer(Modifier.height(28.dp))
                Text(
                    "History",
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onBackground,
                )
                Spacer(Modifier.height(10.dp))
            }

            when (val h = history) {
                is InvoiceHistoryState.Loading -> {
                    items(3) { ShimmerHistoryRow(); Spacer(Modifier.height(8.dp)) }
                }
                is InvoiceHistoryState.Error -> item {
                    Text(h.message, color = MaterialTheme.colorScheme.error)
                }
                is InvoiceHistoryState.Loaded -> {
                    if (h.invoices.isEmpty()) {
                        item {
                            Text(
                                "No invoices yet — the first one you generate will show up here.",
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    } else {
                        items(h.invoices, key = { it.id }) { invoice ->
                            InvoiceHistoryRow(invoice)
                            Spacer(Modifier.height(8.dp))
                        }
                    }
                }
            }
            item { Spacer(Modifier.height(24.dp)) }
        }
    }

    form.submitError?.let { error ->
        AlertDialog(
            onDismissRequest = viewModel::dismissError,
            confirmButton = { TextButton(onClick = viewModel::dismissError) { Text("OK") } },
            title = { Text("Couldn't create invoice") },
            text = { Text(error) },
        )
    }

    form.lastSubmitted?.let { invoice ->
        AlertDialog(
            onDismissRequest = viewModel::clearLastSubmitted,
            confirmButton = { TextButton(onClick = viewModel::clearLastSubmitted) { Text("OK") } },
            title = { Text("Invoice queued") },
            text = {
                Text(
                    "Invoice for ${invoice.guestName} is generating on the server. " +
                        if (invoice.chatId != null)
                            "It'll be sent to WhatsApp automatically once it's ready."
                        else "Check History below once it's ready."
                )
            },
        )
    }
}

@Composable
private fun InvoiceForm(
    form: InvoiceFormState,
    locations: List<com.example.karlon.data.model.LocationOption>,
    chats: List<ChatDto>,
    houseListings: List<com.example.karlon.data.model.HouseListingDto>,
    guestSuggestions: List<InvoiceDto>,
    onGuestNameChange: (String) -> Unit,
    onGuestSuggestionSelect: (InvoiceDto) -> Unit,
    onIdNumberChange: (String) -> Unit,
    onLocationChange: (String) -> Unit,
    onPropertyChange: (PropertyOption) -> Unit,
    onListingChosenAsProperty: (com.example.karlon.data.model.HouseListingDto) -> Unit,
    onCheckInChange: (String) -> Unit,
    onCheckOutChange: (String) -> Unit,
    onNightsChange: (Int) -> Unit,
    onGuestsChange: (Int) -> Unit,
    onRateOverrideChange: (String) -> Unit,
    onSendToChatChange: (ChatDto?) -> Unit,
    onSubmit: () -> Unit,
) {
    val fieldColors = OutlinedTextFieldDefaults.colors(
        focusedBorderColor = KarlonGold,
        unfocusedBorderColor = MaterialTheme.colorScheme.outline,
        focusedTextColor = MaterialTheme.colorScheme.onBackground,
        unfocusedTextColor = MaterialTheme.colorScheme.onBackground,
        focusedLabelColor = KarlonGold,
        cursorColor = KarlonGold,
    )

    AnimatedGlassCard(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp),
    ) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(16.dp),
    ) {
        Text(
            "New invoice",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
            color = MaterialTheme.colorScheme.onBackground,
        )
        Spacer(Modifier.height(14.dp))

        GuestNameField(
            value = form.guestName,
            onValueChange = onGuestNameChange,
            suggestions = guestSuggestions,
            onSuggestionSelect = onGuestSuggestionSelect,
            fieldColors = fieldColors,
        )
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = form.idNumber,
            onValueChange = onIdNumberChange,
            label = { Text("ID / passport number") },
            singleLine = true,
            colors = fieldColors,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(10.dp))

        LocationDropdown(
            locations = locations,
            selected = form.location,
            onSelect = onLocationChange,
            fieldColors = fieldColors,
        )
        Spacer(Modifier.height(10.dp))

        val properties = locations.firstOrNull { it.location == form.location }?.properties.orEmpty()
        PropertyDropdown(
            properties = properties,
            listings = houseListings,
            selected = form.property,
            enabled = form.location != null,
            onSelect = onPropertyChange,
            onSelectListing = onListingChosenAsProperty,
            fieldColors = fieldColors,
        )
        Spacer(Modifier.height(10.dp))

        Row(Modifier.fillMaxWidth()) {
            com.example.karlon.ui.components.DateField(
                value = form.checkIn,
                onValueChange = onCheckInChange,
                label = "Check-in",
                fieldColors = fieldColors,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(10.dp))
            com.example.karlon.ui.components.DateField(
                value = form.checkOut,
                onValueChange = onCheckOutChange,
                label = "Check-out",
                fieldColors = fieldColors,
                modifier = Modifier.weight(1f),
            )
        }
        Spacer(Modifier.height(14.dp))

        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text("Nights", color = MaterialTheme.colorScheme.onBackground, modifier = Modifier.weight(1f))
            IconButton(onClick = { onNightsChange(form.nights - 1) }) {
                Icon(Icons.Default.Remove, contentDescription = "Fewer nights", tint = KarlonGold)
            }
            AnimatedContent(
                targetState = form.nights,
                transitionSpec = { fadeIn(tween(160)) togetherWith fadeOut(tween(120)) },
                label = "nightsCount",
            ) { nights ->
                Text(
                    nights.toString(),
                    color = MaterialTheme.colorScheme.onBackground,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.padding(horizontal = 4.dp),
                )
            }
            IconButton(onClick = { onNightsChange(form.nights + 1) }) {
                Icon(Icons.Default.Add, contentDescription = "More nights", tint = KarlonGold)
            }
        }

        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text("Guests", color = MaterialTheme.colorScheme.onBackground, modifier = Modifier.weight(1f))
            IconButton(onClick = { onGuestsChange(form.guests - 1) }, enabled = form.guests > 1) {
                Icon(Icons.Default.Remove, contentDescription = "Fewer guests", tint = KarlonGold)
            }
            Text(
                form.guests.toString(),
                color = MaterialTheme.colorScheme.onBackground,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.padding(horizontal = 4.dp),
            )
            IconButton(onClick = { onGuestsChange(form.guests + 1) }, enabled = form.guests < 30) {
                Icon(Icons.Default.Add, contentDescription = "More guests", tint = KarlonGold)
            }
        }

        OutlinedTextField(
            value = form.rateOverride,
            onValueChange = onRateOverrideChange,
            label = { Text("Rate per night (${form.property?.let { "default %.2f".format(it.rate) } ?: "USD"})") },
            singleLine = true,
            colors = fieldColors,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(14.dp))

        ChatAttachRow(
            chats = chats,
            selected = form.sendToChat,
            autoMatched = form.sendToChatAutoMatched,
            onSelect = onSendToChatChange,
        )
        Spacer(Modifier.height(16.dp))

        HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.6.dp)
        Spacer(Modifier.height(12.dp))

        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.weight(1f)) {
                Text("Total", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp)
                Text(
                    "USD %.2f".format(form.total),
                    color = KarlonGold,
                    fontWeight = FontWeight.Bold,
                    fontSize = 20.sp,
                )
            }
            Button(
                onClick = onSubmit,
                enabled = form.canSubmit,
                colors = ButtonDefaults.buttonColors(
                    containerColor = KarlonGold,
                    contentColor = Color(0xFF1A1204),
                    disabledContainerColor = MaterialTheme.colorScheme.surfaceVariant,
                ),
            ) {
                AnimatedContent(
                    targetState = form.submitting,
                    transitionSpec = { fadeIn(tween(180)) togetherWith fadeOut(tween(120)) },
                    label = "submitButtonContent",
                ) { submitting ->
                    if (submitting) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(18.dp),
                            strokeWidth = 2.dp,
                            color = Color(0xFF1A1204),
                        )
                    } else {
                        Text("Generate invoice", fontWeight = FontWeight.Bold)
                    }
                }
            }
        }
    }
    }
}

@Composable
private fun ChatAttachRow(
    chats: List<ChatDto>,
    selected: ChatDto?,
    autoMatched: Boolean,
    onSelect: (ChatDto?) -> Unit,
) {
    var expanded by remember { mutableStateOf(false) }
    Column {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(
                checked = selected != null,
                onCheckedChange = { checked ->
                    if (!checked) onSelect(null) else expanded = true
                },
                colors = SwitchDefaults.colors(checkedThumbColor = KarlonRingGlow),
            )
            Spacer(Modifier.width(8.dp))
            Text(
                "Send to WhatsApp chat once ready",
                color = MaterialTheme.colorScheme.onBackground,
                fontSize = 14.sp,
            )
        }
        if (selected != null) {
            AnimatedVisibility(
                visible = true,
                enter = fadeIn(tween(220)) + expandVertically(tween(220)),
                exit = fadeOut(tween(150)) + shrinkVertically(tween(150)),
            ) {
                Column {
                    Spacer(Modifier.height(6.dp))
                    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
                        OutlinedTextField(
                            value = selected.name,
                            onValueChange = {},
                            readOnly = true,
                            label = { Text("Chat") },
                            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
                            colors = OutlinedTextFieldDefaults.colors(
                                focusedBorderColor = KarlonGold,
                                unfocusedBorderColor = MaterialTheme.colorScheme.outline,
                                focusedTextColor = MaterialTheme.colorScheme.onBackground,
                                unfocusedTextColor = MaterialTheme.colorScheme.onBackground,
                            ),
                            modifier = Modifier.menuAnchor().fillMaxWidth(),
                        )
                        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                            chats.forEach { chat ->
                                DropdownMenuItem(
                                    text = { Text(chat.name) },
                                    onClick = { onSelect(chat); expanded = false },
                                )
                            }
                        }
                    }
                    // Flags an auto-picked chat so the front desk can double
                    // check it before the invoice actually sends — a wrong
                    // guess here means a misdirected invoice, so this stays
                    // visible instead of silently trusting the match.
                    if (autoMatched) {
                        Spacer(Modifier.height(4.dp))
                        Text(
                            "Matched automatically from guest name — check before sending",
                            color = KarlonRingGlow,
                            fontSize = 12.sp,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun LocationDropdown(
    locations: List<com.example.karlon.data.model.LocationOption>,
    selected: String?,
    onSelect: (String) -> Unit,
    fieldColors: TextFieldColors,
) {
    var expanded by remember { mutableStateOf(false) }
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = selected ?: "",
            onValueChange = {},
            readOnly = true,
            label = { Text("Location") },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            colors = fieldColors,
            modifier = Modifier.menuAnchor().fillMaxWidth(),
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            if (locations.isEmpty()) {
                DropdownMenuItem(text = { Text("No locations configured") }, onClick = {}, enabled = false)
            }
            locations.forEach { loc ->
                DropdownMenuItem(
                    text = { Text(loc.location) },
                    onClick = { onSelect(loc.location); expanded = false },
                )
            }
        }
    }
}

@Composable
private fun PropertyDropdown(
    properties: List<PropertyOption>,
    listings: List<com.example.karlon.data.model.HouseListingDto>,
    selected: PropertyOption?,
    enabled: Boolean,
    onSelect: (PropertyOption) -> Unit,
    onSelectListing: (com.example.karlon.data.model.HouseListingDto) -> Unit,
    fieldColors: TextFieldColors,
) {
    var expanded by remember { mutableStateOf(false) }
    val alpha by androidx.compose.animation.core.animateFloatAsState(
        targetValue = if (enabled) 1f else 0.5f,
        animationSpec = tween(220),
        label = "propertyDropdownEnabledFade",
    )
    ExposedDropdownMenuBox(
        expanded = expanded && enabled,
        onExpandedChange = { if (enabled) expanded = it },
        modifier = Modifier.graphicsLayer { this.alpha = alpha },
    ) {
        OutlinedTextField(
            value = selected?.name ?: "",
            onValueChange = {},
            readOnly = true,
            enabled = enabled,
            label = { Text("Property") },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            colors = fieldColors,
            modifier = Modifier.menuAnchor().fillMaxWidth(),
        )
        ExposedDropdownMenu(expanded = expanded && enabled, onDismissRequest = { expanded = false }) {
            if (properties.isEmpty() && listings.isEmpty()) {
                DropdownMenuItem(text = { Text("No properties for this location") }, onClick = {}, enabled = false)
            }
            properties.forEach { prop ->
                DropdownMenuItem(
                    text = { Text("${prop.name} — %.2f/night".format(prop.rate)) },
                    onClick = { onSelect(prop); expanded = false },
                )
            }
            if (listings.isNotEmpty()) {
                HorizontalDivider(color = MaterialTheme.colorScheme.outline, thickness = 0.4.dp)
                Text(
                    if (listings.any { it.sentToChat }) "Sent to this guest first, then available now"
                    else "Available now (live scrape)",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                )
                listings.forEach { listing ->
                    DropdownMenuItem(
                        text = {
                            Column {
                                Text(
                                    (if (listing.sentToChat) "✅ " else "🏠 ") +
                                        "${listing.title ?: listing.displayName} — ${listing.displayPrice}",
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                )
                                val stay = listOfNotNull(listing.checkIn, listing.checkOut).joinToString(" → ")
                                if (stay.isNotBlank() || listing.sentToChat) {
                                    Text(
                                        listOfNotNull(
                                            if (listing.sentToChat) "Sent to this guest" else null,
                                            stay.ifBlank { null },
                                            listing.nights?.let { "$it night${if (it == 1) "" else "s"}" },
                                        ).joinToString(" · "),
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                            }
                        },
                        onClick = { onSelectListing(listing); expanded = false },
                    )
                }
            }
        }
    }
}

@Composable
private fun GuestNameField(
    value: String,
    onValueChange: (String) -> Unit,
    suggestions: List<InvoiceDto>,
    onSuggestionSelect: (InvoiceDto) -> Unit,
    fieldColors: TextFieldColors,
) {
    // Deliberate identity cache: as the front desk types, past guests whose
    // saved name loosely overlaps (relaxed token match, not exact string
    // match — see InvoiceViewModel.guestSuggestions/nameTokens) surface here
    // so returning guests never need to retype ID/location/property/rate.
    var expanded by remember { mutableStateOf(false) }
    ExposedDropdownMenuBox(
        expanded = expanded && suggestions.isNotEmpty(),
        onExpandedChange = { expanded = it },
    ) {
        OutlinedTextField(
            value = value,
            onValueChange = { onValueChange(it); expanded = true },
            label = { Text("Guest full name") },
            singleLine = true,
            colors = fieldColors,
            modifier = Modifier.menuAnchor().fillMaxWidth(),
        )
        ExposedDropdownMenu(
            expanded = expanded && suggestions.isNotEmpty(),
            onDismissRequest = { expanded = false },
        ) {
            suggestions.forEach { inv ->
                DropdownMenuItem(
                    text = {
                        Column {
                            Text(inv.guestName, fontWeight = FontWeight.SemiBold)
                            Text(
                                "${inv.propertyName} · ${inv.location}",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    },
                    onClick = { onSuggestionSelect(inv); expanded = false },
                )
            }
        }
    }
}

@Composable
private fun InvoiceHistoryRow(invoice: InvoiceDto) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .glassSurface(shape = RoundedCornerShape(12.dp))
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                invoice.guestName,
                color = MaterialTheme.colorScheme.onBackground,
                fontWeight = FontWeight.Bold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(2.dp))
            Text(
                "${invoice.propertyName} · ${invoice.location} · ${invoice.currency} %.2f".format(invoice.total),
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                fontSize = 13.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Spacer(Modifier.width(10.dp))
        StatusChip(invoice.status)
    }
}

@Composable
private fun StatusChip(status: String) {
    val (bg, fg, label) = when (status) {
        "ready" -> Triple(KarlonRingGlow.copy(alpha = 0.18f), KarlonRingGlow, "Ready")
        "processing" -> Triple(KarlonGold.copy(alpha = 0.18f), KarlonGold, "Rendering")
        "pending" -> Triple(MaterialTheme.colorScheme.outline.copy(alpha = 0.3f), MaterialTheme.colorScheme.onSurfaceVariant, "Queued")
        "error" -> Triple(KarlonError.copy(alpha = 0.18f), KarlonError, "Error")
        else -> Triple(MaterialTheme.colorScheme.outline.copy(alpha = 0.3f), MaterialTheme.colorScheme.onSurfaceVariant, status)
    }
    Box(
        modifier = Modifier.background(bg, CircleShape).padding(horizontal = 10.dp, vertical = 5.dp),
    ) {
        Text(label, color = fg, fontSize = 12.sp, fontWeight = FontWeight.Bold)
    }
}
