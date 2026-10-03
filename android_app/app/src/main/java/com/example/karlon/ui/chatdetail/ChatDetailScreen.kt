package com.example.karlon.ui.chatdetail

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.AttachMoney
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.EventAvailable
import androidx.compose.material.icons.filled.House
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Send
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.RectangleShape
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import androidx.compose.ui.res.stringResource

import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.compose.ui.res.pluralStringResource
import coil.compose.AsyncImage
import com.example.karlon.R
import com.example.karlon.data.model.ChatDto
import com.example.karlon.data.model.HouseListingDto
import com.example.karlon.data.model.ReservationDto
import com.example.karlon.ui.components.AvatarImage
import com.example.karlon.ui.components.MessageBubble
import com.example.karlon.ui.components.ShimmerLine
import com.example.karlon.ui.components.glassSurface
import kotlinx.coroutines.launch

/**
 * The chat thread itself: history + live messages, a text composer, and
 * an image-attach button — this screen is where the text<->image sync
 * is actually visible to the user.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatDetailScreen(
    chat: ChatDto,
    displayName: String,
    viewModel: ChatDetailViewModel,
    onBack: () -> Unit,
    onGenerateInvoice: () -> Unit = {},
) {
    val uiState by viewModel.uiState.collectAsState()
    val housesUiState by viewModel.housesUiState.collectAsState()
    var draft by remember { mutableStateOf("") }
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()

    val imagePicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.GetContent(),
    ) { uri: Uri? ->
        uri?.let { viewModel.sendImage(it) }
    }

    LaunchedEffect(uiState.messages.size) {
        if (uiState.messages.isNotEmpty()) {
            scope.launch { listState.animateScrollToItem(uiState.messages.size - 1) }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        AvatarImage(
                            photoUrl = chat.profilePicUrl,
                            fallbackText = chat.avatarEmoji ?: chat.name,
                            size = 36.dp,
                        )
                        Spacer(Modifier.width(10.dp))
                        Text(chat.name, fontWeight = FontWeight.SemiBold, maxLines = 1)
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Default.ArrowBack, contentDescription = "Back")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
            )
        },
        bottomBar = {
            Composer(
                draft = draft,
                onDraftChange = { draft = it },
                onSend = {
                    viewModel.sendText(draft)
                    draft = ""
                },
                onShowHouses = { viewModel.openHousesPopup() },
                isSending = uiState.isSending,
            )
        },
        // Docked in Scaffold's own FAB slot (not floating loose over the
        // content) — Scaffold automatically lifts this clear of bottomBar,
        // so it can never sit on top of or crowd the message input row.
        floatingActionButton = {
            var fabVisible by remember { mutableStateOf(false) }
            val fabScale by animateFloatAsState(
                targetValue = if (fabVisible) 1f else 0f,
                animationSpec = spring(dampingRatio = Spring.DampingRatioMediumBouncy),
                label = "sendTermsFabScale",
            )
            LaunchedEffect(Unit) { fabVisible = true }
            Column(
                horizontalAlignment = Alignment.End,
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                // Invoice icon — same SmallFloatingActionButton size as the
                // Terms & Conditions button below it. Sits above it in the
                // stack. Jumps straight into the invoice form with this
                // guest's name and WhatsApp chat already filled in (see
                // InvoiceViewModel's prefillFromChat). The house-browsing
                // entry point now lives in the composer row instead.
                SmallFloatingActionButton(
                    onClick = onGenerateInvoice,
                    modifier = Modifier.graphicsLayer { scaleX = fabScale; scaleY = fabScale },
                    containerColor = MaterialTheme.colorScheme.secondaryContainer,
                    contentColor = MaterialTheme.colorScheme.onSecondaryContainer,
                ) {
                    Icon(
                        Icons.Default.AttachMoney,
                        contentDescription = stringResource(R.string.generate_invoice),
                    )
                }
                SmallFloatingActionButton(
                    onClick = { viewModel.sendTerms() },
                    modifier = Modifier.graphicsLayer { scaleX = fabScale; scaleY = fabScale },
                    containerColor = MaterialTheme.colorScheme.primary,
                    contentColor = MaterialTheme.colorScheme.onPrimary,
                ) {
                    Icon(
                        Icons.Default.Description,
                        contentDescription = stringResource(R.string.send_terms),
                    )
                }
            }
        },
        floatingActionButtonPosition = FabPosition.End,
        containerColor = MaterialTheme.colorScheme.surfaceVariant,
    ) { padding ->
        Box(modifier = Modifier.padding(padding).fillMaxSize()) {
            AnimatedContent(
                targetState = uiState.isLoading,
                transitionSpec = { fadeIn(tween(260)) togetherWith fadeOut(tween(180)) },
                label = "chatDetailLoadingState",
            ) { isLoading ->
                if (isLoading) {
                    Column(
                        modifier = Modifier.fillMaxSize().padding(horizontal = 12.dp, vertical = 10.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        ShimmerLine(width = 220.dp, height = 40.dp)
                        ShimmerLine(width = 160.dp, height = 40.dp)
                        ShimmerLine(width = 240.dp, height = 40.dp)
                        ShimmerLine(width = 140.dp, height = 40.dp)
                    }
                } else {
                    LazyColumn(
                        state = listState,
                        modifier = Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 10.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        items(uiState.messages, key = { it.id }) { message ->
                            MessageBubble(
                                message = message,
                                // Was `message.sender == displayName`, which only
                                // matches messages the app itself composed (sender
                                // is literally set to displayName at send time).
                                // Everything wa_bridge imports from WhatsApp comes
                                // through with sender = "You (WhatsApp)" for your
                                // own outgoing messages (see wa_bridge.detect_direction
                                // -> scrape_messages) or the contact's name for
                                // theirs — neither of which is ever equal to
                                // displayName, so every WA-imported message (i.e.
                                // almost all of them) was rendering as incoming
                                // regardless of who actually sent it. `isOutbound`
                                // (direction == "out") is the field both paths
                                // agree on, so it's the one that actually means
                                // "mine" here.
                                isOwnMessage = message.isOutbound,
                                resolvedMediaUrl = viewModel.resolvedMediaUrl(message.mediaUrl),
                            )
                        }
                    }
                }
            }

            uiState.errorMessage?.let { error ->
                Snackbar(
                    modifier = Modifier.align(Alignment.BottomCenter).padding(12.dp),
                    containerColor = MaterialTheme.colorScheme.error,
                ) { Text(error) }
            }
        }
    }

    if (housesUiState.isVisible) {
        AvailableHousesDialog(
            state = housesUiState,
            onLocationChange = viewModel::onHousesLocationChange,
            onLocationPicked = viewModel::onHousesLocationPicked,
            onDatesPicked = viewModel::onHousesDatesPicked,
            onSortChange = viewModel::onHousesSortChange,
            onSearch = viewModel::searchHouses,
            onToggleSelect = viewModel::toggleHouseSelection,
            onSend = viewModel::sendSelectedHouses,
            onReserve = viewModel::openReserveConfirm,
            onCancelReservation = viewModel::cancelReservation,
            onDismiss = viewModel::dismissHousesPopup,
        )
        if (housesUiState.reserveConfirmVisible) {
            ReserveConfirmDialog(
                state = housesUiState,
                onGuestsChange = viewModel::onReserveGuestsChange,
                onMessageChange = viewModel::onReserveMessageChange,
                onConfirm = viewModel::confirmReservation,
                onDismiss = viewModel::dismissReserveConfirm,
            )
        }
    }
}

/**
 * Popup opened by the house-icon FAB: a location search box, a grid that
 * auto-populates with up to 6 of the freshest scraped listings for that
 * area the moment a search runs, and a Send button that WhatsApps every
 * checked listing's photos + price/title/link to this chat.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AvailableHousesDialog(
    state: AvailableHousesUiState,
    onLocationChange: (String) -> Unit,
    onLocationPicked: (String) -> Unit,
    onDatesPicked: (String, String) -> Unit,
    onSortChange: (Boolean) -> Unit,
    onSearch: () -> Unit,
    onToggleSelect: (String) -> Unit,
    onSend: () -> Unit,
    onReserve: () -> Unit,
    onCancelReservation: () -> Unit,
    onDismiss: () -> Unit,
) {
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(
            shape = MaterialTheme.shapes.extraLarge,
            color = MaterialTheme.colorScheme.surface,
            tonalElevation = 6.dp,
            modifier = Modifier.fillMaxWidth(0.95f).fillMaxHeight(0.92f),
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.House, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.width(8.dp))
                    Text(
                        stringResource(R.string.available_houses_title),
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold,
                        modifier = Modifier.weight(1f),
                    )
                    IconButton(onClick = onDismiss) {
                        Icon(Icons.Default.ArrowBack, contentDescription = stringResource(R.string.back))
                    }
                }

                Spacer(Modifier.height(10.dp))

                // Harare by default; the dropdown lists the other places (and
                // anything typed narrows it). Picking one searches right away.
                var locationMenuOpen by remember { mutableStateOf(false) }
                val typed = state.location.trim()
                val options = state.locationOptions.filter {
                    typed.isEmpty() || it.equals(typed, ignoreCase = true) || it.contains(typed, ignoreCase = true)
                }.ifEmpty { state.locationOptions }
                ExposedDropdownMenuBox(
                    expanded = locationMenuOpen,
                    onExpandedChange = { locationMenuOpen = it },
                ) {
                    OutlinedTextField(
                        value = state.location,
                        onValueChange = { onLocationChange(it); locationMenuOpen = true },
                        placeholder = { Text(stringResource(R.string.available_houses_location_hint)) },
                        singleLine = true,
                        trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = locationMenuOpen) },
                        modifier = Modifier.fillMaxWidth().menuAnchor(),
                        shape = MaterialTheme.shapes.large,
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                        keyboardActions = KeyboardActions(onSearch = { locationMenuOpen = false; onSearch() }),
                    )
                    ExposedDropdownMenu(
                        expanded = locationMenuOpen,
                        onDismissRequest = { locationMenuOpen = false },
                    ) {
                        options.forEach { place ->
                            DropdownMenuItem(
                                text = { Text(place) },
                                onClick = { locationMenuOpen = false; onLocationPicked(place) },
                                contentPadding = ExposedDropdownMenuDefaults.ItemContentPadding,
                            )
                        }
                    }
                }

                Spacer(Modifier.height(10.dp))

                // Dates are optional. One button opens a Material 3 date range
                // picker (check-in → check-out in one go); with dates, only
                // houses priced for exactly that stay are shown.
                var showDatePicker by remember { mutableStateOf(false) }
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    OutlinedButton(
                        onClick = { showDatePicker = true },
                        modifier = Modifier.weight(1f).heightIn(min = 52.dp),
                        shape = MaterialTheme.shapes.large,
                    ) {
                        Icon(Icons.Default.DateRange, contentDescription = null, modifier = Modifier.size(18.dp))
                        Spacer(Modifier.width(8.dp))
                        Text(
                            stayLabel(state.checkIn, state.checkOut)
                                ?: stringResource(R.string.available_houses_any_dates),
                            maxLines = 1,
                        )
                    }
                    if (state.checkIn.isNotBlank() || state.checkOut.isNotBlank()) {
                        IconButton(onClick = { onDatesPicked("", "") }) {
                            Icon(Icons.Default.Close, contentDescription = stringResource(R.string.available_houses_clear_dates))
                        }
                    }
                    Spacer(Modifier.width(8.dp))
                    FilledIconButton(onClick = onSearch, enabled = state.canSearch, modifier = Modifier.size(52.dp)) {
                        Icon(Icons.Default.Search, contentDescription = stringResource(R.string.search))
                    }
                }
                if (showDatePicker) {
                    StayRangePickerDialog(
                        checkIn = state.checkIn,
                        checkOut = state.checkOut,
                        onDismiss = { showDatePicker = false },
                        onConfirm = { ci, co -> showDatePicker = false; onDatesPicked(ci, co) },
                    )
                }

                Spacer(Modifier.height(10.dp))

                // Sort + count: filter chips, as Material 3 suggests for
                // narrowing search results (two choices, short labels).
                Row(verticalAlignment = Alignment.CenterVertically) {
                    FilterChip(
                        selected = state.sortByRef,
                        onClick = { onSortChange(true) },
                        label = { Text(stringResource(R.string.available_houses_sort_ref)) },
                    )
                    Spacer(Modifier.width(8.dp))
                    FilterChip(
                        selected = !state.sortByRef,
                        onClick = { onSortChange(false) },
                        label = { Text(stringResource(R.string.available_houses_sort_newest)) },
                    )
                    Spacer(Modifier.weight(1f))
                    if (!state.isLoading && state.listings.isNotEmpty()) {
                        Text(
                            pluralStringResource(R.plurals.available_houses_count, state.listings.size, state.listings.size),
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }

                Spacer(Modifier.height(12.dp))

                Box(modifier = Modifier.weight(1f)) {
                    when {
                        state.isLoading -> Column(
                            modifier = Modifier.fillMaxWidth().padding(vertical = 24.dp),
                            horizontalAlignment = Alignment.CenterHorizontally,
                        ) { CircularProgressIndicator() }

                        state.listings.isEmpty() -> Text(
                            stringResource(R.string.available_houses_empty),
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(vertical = 24.dp),
                        )

                        else -> LazyVerticalGrid(
                            columns = GridCells.Adaptive(minSize = 150.dp),
                            horizontalArrangement = Arrangement.spacedBy(10.dp),
                            verticalArrangement = Arrangement.spacedBy(10.dp),
                            modifier = Modifier.fillMaxSize(),
                        ) {
                            items(state.listings, key = { it.id }) { listing ->
                                HouseListingCard(
                                    listing = listing,
                                    isSelected = listing.id in state.selectedIds,
                                    onToggle = { onToggleSelect(listing.id) },
                                )
                            }
                        }
                    }
                }

                state.errorMessage?.let {
                    Spacer(Modifier.height(8.dp))
                    Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                }
                if (state.sendSuccess) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        stringResource(R.string.available_houses_sent),
                        color = MaterialTheme.colorScheme.primary,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }

                ReservationStatus(state, onCancelReservation)

                Spacer(Modifier.height(14.dp))

                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(
                        onClick = onReserve,
                        enabled = state.canReserve,
                        modifier = Modifier.weight(1f),
                    ) {
                        if (state.isReserving) {
                            CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                        } else {
                            Icon(Icons.Default.EventAvailable, contentDescription = null, modifier = Modifier.size(18.dp))
                            Spacer(Modifier.width(8.dp))
                            Text(stringResource(R.string.reserve))
                        }
                    }
                    Button(
                        onClick = onSend,
                        enabled = state.selectedIds.isNotEmpty() && !state.isSending,
                        modifier = Modifier.weight(1f),
                    ) {
                        if (state.isSending) {
                            CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                        } else {
                            Icon(Icons.Default.Send, contentDescription = null, modifier = Modifier.size(18.dp))
                            Spacer(Modifier.width(8.dp))
                            Text(
                                if (state.selectedIds.isEmpty()) stringResource(R.string.available_houses_send)
                                else stringResource(R.string.available_houses_send_count, state.selectedIds.size),
                            )
                        }
                    }
                }
                if (state.selectedIds.size > 1) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        stringResource(R.string.reserve_pick_one),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}

/** Where the queued booking is: waiting for the PC, on Airbnb, done. */
@Composable
private fun ReservationStatus(state: AvailableHousesUiState, onCancel: () -> Unit) {
    state.reserveError?.let {
        Spacer(Modifier.height(8.dp))
        Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
    }
    val r: ReservationDto = state.reservation ?: return
    val ref = r.refCode ?: ""
    val (text, isError) = when (r.status) {
        "pending" -> stringResource(R.string.reserve_status_pending, ref) to false
        "in_progress" -> stringResource(R.string.reserve_status_in_progress, ref) to false
        "requested" -> stringResource(R.string.reserve_status_requested, ref) to false
        "dry_run" -> stringResource(R.string.reserve_status_dry_run, ref, r.errorMessage.orEmpty()) to false
        "failed" -> stringResource(R.string.reserve_status_failed, r.errorMessage.orEmpty()) to true
        "unknown" -> stringResource(R.string.reserve_status_unknown, r.errorMessage.orEmpty()) to true
        "cancelled" -> stringResource(R.string.reserve_status_cancelled) to false
        else -> r.status to false
    }
    Spacer(Modifier.height(8.dp))
    Row(verticalAlignment = Alignment.CenterVertically) {
        if (!r.isFinished) {
            CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
            Spacer(Modifier.width(8.dp))
        }
        Text(
            text,
            color = if (isError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.primary,
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.weight(1f),
        )
        if (r.status == "pending") {
            TextButton(onClick = onCancel) { Text(stringResource(R.string.reserve_cancel)) }
        }
    }
}

/** Confirm before anything is queued: house, dates, guests, quoted total,
 * optional note to the host. */
@Composable
private fun ReserveConfirmDialog(
    state: AvailableHousesUiState,
    onGuestsChange: (Int) -> Unit,
    onMessageChange: (String) -> Unit,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit,
) {
    val listing = state.reserveListing ?: return
    AlertDialog(
        onDismissRequest = onDismiss,
        icon = { Icon(Icons.Default.EventAvailable, contentDescription = null) },
        title = { Text(stringResource(R.string.reserve_confirm_title, listing.displayName)) },
        text = {
            Column {
                Text(stringResource(R.string.reserve_confirm_dates, state.reserveCheckIn, state.reserveCheckOut))
                Spacer(Modifier.height(10.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(stringResource(R.string.reserve_guests), modifier = Modifier.weight(1f))
                    OutlinedIconButtonText("−", enabled = state.reserveGuests > 1) { onGuestsChange(state.reserveGuests - 1) }
                    Text(
                        state.reserveGuests.toString(),
                        style = MaterialTheme.typography.titleMedium,
                        modifier = Modifier.padding(horizontal = 12.dp),
                    )
                    OutlinedIconButtonText("+", enabled = state.reserveGuests < 16) { onGuestsChange(state.reserveGuests + 1) }
                }
                val quoted = listing.quotedTotalUsd.takeIf {
                    listing.checkIn == state.reserveCheckIn && listing.checkOut == state.reserveCheckOut
                }
                Spacer(Modifier.height(6.dp))
                Text(
                    if (quoted != null) stringResource(R.string.reserve_confirm_total, "%,.2f".format(quoted))
                    else stringResource(R.string.reserve_confirm_no_total),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(
                    value = state.reserveMessage,
                    onValueChange = onMessageChange,
                    label = { Text(stringResource(R.string.reserve_message_hint)) },
                    minLines = 2,
                    maxLines = 4,
                    modifier = Modifier.fillMaxWidth(),
                    keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences),
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    stringResource(R.string.reserve_confirm_note),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = { Button(onClick = onConfirm) { Text(stringResource(R.string.reserve_confirm_button)) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text(stringResource(R.string.reserve_confirm_cancel)) } },
    )
}

@Composable
private fun OutlinedIconButtonText(label: String, enabled: Boolean, onClick: () -> Unit) {
    OutlinedIconButton(onClick = onClick, enabled = enabled, modifier = Modifier.size(36.dp)) {
        Text(label, style = MaterialTheme.typography.titleMedium)
    }
}

@Composable
private fun HouseListingCard(
    listing: HouseListingDto,
    isSelected: Boolean,
    onToggle: () -> Unit,
) {
    Surface(
        shape = RoundedCornerShape(14.dp),
        color = if (isSelected) MaterialTheme.colorScheme.primaryContainer
            else MaterialTheme.colorScheme.surfaceVariant,
        modifier = Modifier.fillMaxWidth().clickable(onClick = onToggle),
    ) {
        Column {
            Box {
                AsyncImage(
                    model = listing.images.firstOrNull(),
                    contentDescription = listing.displayName,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxWidth().height(96.dp).clip(RoundedCornerShape(topStart = 14.dp, topEnd = 14.dp)),
                )
                if (isSelected) {
                    Surface(
                        shape = androidx.compose.foundation.shape.CircleShape,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.padding(6.dp).size(22.dp).align(Alignment.TopEnd),
                    ) {
                        Icon(
                            Icons.Default.Check,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.onPrimary,
                            modifier = Modifier.padding(3.dp),
                        )
                    }
                }
            }
            Column(modifier = Modifier.padding(8.dp)) {
                Text(
                    listing.refCode ?: listing.title?.takeIf { it.isNotBlank() }
                        ?: stringResource(R.string.available_houses_untitled),
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                )
                if (listing.refCode != null && !listing.airbnbTitle.isNullOrBlank()) {
                    // Staff-only: the Airbnb title is never sent to guests.
                    Text(
                        listing.airbnbTitle,
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 1,
                    )
                }
                val place = listing.neighbourhood?.takeIf { it.isNotBlank() }
                    ?: listing.location.replaceFirstChar { it.uppercase() }
                Text(
                    "📍 $place",
                    style = MaterialTheme.typography.labelSmall,
                    maxLines = 1,
                )
                listing.capacity?.takeIf { it.isNotBlank() }?.let {
                    Text(
                        "👥 $it",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 1,
                    )
                }
                Text(
                    listing.displayPrice,
                    style = MaterialTheme.typography.bodySmall,
                    fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
        }
    }
}

@Composable
private fun Composer(
    draft: String,
    onDraftChange: (String) -> Unit,
    onSend: () -> Unit,
    onShowHouses: () -> Unit,
    isSending: Boolean,
) {
    // Frosted-glass bottom bar: an opaque base (so message bubbles scrolling
    // underneath don't show through — there's no real backdrop blur in this
    // renderer) with the same glass highlight/border layered on top, so it
    // reads as "glass docked over the thread" rather than a flat fill.
    //
    // enableEdgeToEdge() (MainActivity) draws this screen behind the system
    // bars, and a plain custom Box in Scaffold's bottomBar slot doesn't get
    // any inset padding for free the way BottomAppBar/NavigationBar do — so
    // without this, the composer sat right under the phone's own gesture
    // bar (the row was there, just partly hidden behind system UI) and
    // would sit under the keyboard too once it opened. navigationBarsPadding
    // lifts it clear of the gesture/3-button bar; imePadding then lifts it
    // further, above the keyboard, whenever it's shown.
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.background)
            .glassSurface(shape = RectangleShape, tintAlpha = 0.05f, borderAlpha = 0.10f)
            .navigationBarsPadding()
            .imePadding(),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 8.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            // House icon replaces the old money-emoji button here — tapping
            // it opens the same "available listings" popup as the FAB used
            // to (see AvailableHousesDialog), auto-populated for this
            // chat's area. The invoice shortcut now lives in the FAB stack.
            IconButton(onClick = onShowHouses) {
                Icon(
                    Icons.Default.House,
                    contentDescription = stringResource(R.string.show_available_houses),
                    tint = MaterialTheme.colorScheme.primary,
                )
            }
            val sendEnabled = draft.isNotBlank() && !isSending
            OutlinedTextField(
                value = draft,
                onValueChange = onDraftChange,
                placeholder = { Text(stringResource(R.string.message_hint)) },
                modifier = Modifier.weight(1f),
                shape = MaterialTheme.shapes.extraLarge,
                // Sentence case is what every chat keyboard defaults to, and
                // the IME's own Send action now actually sends — previously
                // Send was declared here but nothing was wired to it, so the
                // keyboard's action button did nothing and the on-screen
                // send icon was the only way to send a message.
                keyboardOptions = KeyboardOptions(
                    imeAction = ImeAction.Send,
                    capitalization = KeyboardCapitalization.Sentences,
                ),
                keyboardActions = KeyboardActions(onSend = { if (sendEnabled) onSend() }),
                maxLines = 4,
            )
            Spacer(Modifier.width(6.dp))
            val sendTint by animateColorAsState(
                targetValue = if (sendEnabled) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.4f),
                animationSpec = tween(180),
                label = "sendButtonTint",
            )
            IconButton(
                onClick = onSend,
                enabled = sendEnabled,
            ) {
                Icon(
                    Icons.Default.Send,
                    contentDescription = stringResource(R.string.send),
                    tint = sendTint,
                )
            }
        }
    }
}

/** "5 Oct → 8 Oct · 3 nights", or null when no full date range is set. */
private fun stayLabel(checkIn: String, checkOut: String): String? = try {
    val ci = java.time.LocalDate.parse(checkIn)
    val co = java.time.LocalDate.parse(checkOut)
    val fmt = java.time.format.DateTimeFormatter.ofPattern("d MMM")
    val nights = java.time.temporal.ChronoUnit.DAYS.between(ci, co)
    "${ci.format(fmt)} → ${co.format(fmt)} · $nights night${if (nights == 1L) "" else "s"}"
} catch (e: Exception) {
    null
}

/** Check-in and check-out in one Material 3 date range picker; past days
 * can't be picked. Returns YYYY-MM-DD strings. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun StayRangePickerDialog(
    checkIn: String,
    checkOut: String,
    onDismiss: () -> Unit,
    onConfirm: (String, String) -> Unit,
) {
    fun toMillis(d: String): Long? = try {
        java.time.LocalDate.parse(d).atStartOfDay(java.time.ZoneOffset.UTC).toInstant().toEpochMilli()
    } catch (e: Exception) { null }
    fun toDate(ms: Long): String =
        java.time.Instant.ofEpochMilli(ms).atZone(java.time.ZoneOffset.UTC).toLocalDate().toString()
    val today = remember { java.time.LocalDate.now().atStartOfDay(java.time.ZoneOffset.UTC).toInstant().toEpochMilli() }
    val state = rememberDateRangePickerState(
        initialSelectedStartDateMillis = toMillis(checkIn),
        initialSelectedEndDateMillis = toMillis(checkOut),
        selectableDates = object : SelectableDates {
            override fun isSelectableDate(utcTimeMillis: Long) = utcTimeMillis >= today
        },
    )
    val start = state.selectedStartDateMillis
    val end = state.selectedEndDateMillis
    DatePickerDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(
                onClick = { if (start != null && end != null) onConfirm(toDate(start), toDate(end)) },
                enabled = start != null && end != null && end > start,
            ) { Text(stringResource(R.string.available_houses_dates_ok)) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(stringResource(R.string.reserve_confirm_cancel)) } },
    ) {
        DateRangePicker(
            state = state,
            modifier = Modifier.weight(1f),
            title = {
                Text(
                    stringResource(R.string.available_houses_pick_stay),
                    modifier = Modifier.padding(start = 24.dp, end = 12.dp, top = 16.dp),
                )
            },
            showModeToggle = false,
        )
    }
}
