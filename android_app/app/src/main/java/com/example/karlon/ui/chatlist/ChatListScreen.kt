package com.example.karlon.ui.chatlist

import android.Manifest
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.LocationOff
import androidx.compose.material.icons.filled.LocationOn
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.karlon.R
import com.example.karlon.data.model.ChatDto
import com.example.karlon.location.LocationTrackingService
import com.example.karlon.ui.components.AvatarImage
import com.example.karlon.ui.components.ShimmerChatRow
import com.example.karlon.ui.components.SyncPulseDot
import com.example.karlon.ui.theme.KarlonBgCardPressed
import com.example.karlon.ui.theme.KarlonBrandGreen
import com.example.karlon.ui.theme.KarlonRingGlow
import com.example.karlon.ui.theme.KarlonRingCore
import com.example.karlon.ui.theme.KarlonWhatsAppGreen

/** iOS Messages-style chat list on a near-black canvas: compact avatars
 * with a glowing green unread ring (Apple Watch Activity-ring style),
 * a bold nav title, and slim hairline dividers inset past the avatar —
 * same information hierarchy as before, restyled onto the new dark
 * KARLCON palette. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatListScreen(
    viewModel: ChatListViewModel,
    onOpenChat: (ChatDto) -> Unit,
    onOpenSettings: () -> Unit,
) {
    val uiState by viewModel.uiState.collectAsState()
    val context = LocalContext.current

    // Local-only toggle state; the service itself is source of truth for
    // whether it's actually running, but there's no cheap way to query a
    // Service's running state from Compose, so this just tracks what the
    // user asked for in this app session.
    var isSharingLocation by remember { mutableStateOf(false) }

    // Background location is a *separate* runtime permission on API 30+,
    // and the system only allows requesting it after foreground location
    // is already granted — hence the two-step launcher chain below.
    val backgroundLocationLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ ->
        LocationTrackingService.start(context)
        isSharingLocation = true
    }

    val foregroundLocationLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        val fineGranted = grants[Manifest.permission.ACCESS_FINE_LOCATION] == true
        val coarseGranted = grants[Manifest.permission.ACCESS_COARSE_LOCATION] == true
        if (fineGranted || coarseGranted) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                backgroundLocationLauncher.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
            } else {
                LocationTrackingService.start(context)
                isSharingLocation = true
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        BrandRibbon()
                        // Quiet "still syncing" cue — replaces re-showing a
                        // full-screen spinner on every background refresh.
                        if ((uiState as? ChatListUiState.Loaded)?.isRefreshing == true) {
                            Spacer(Modifier.width(8.dp))
                            SyncPulseDot()
                        }
                    }
                },
                actions = {
                    IconButton(onClick = { viewModel.refresh() }) {
                        Icon(
                            Icons.Default.Refresh,
                            contentDescription = "Refresh",
                            tint = MaterialTheme.colorScheme.primary,
                        )
                    }
                    // Live-location-sharing button removed for now — the
                    // underlying LocationTrackingService/permission launcher
                    // plumbing above is left in place so it's a one-block
                    // paste to bring back later if needed.
                    IconButton(onClick = onOpenSettings) {
                        Icon(
                            Icons.Default.Settings,
                            contentDescription = "Server settings",
                            tint = MaterialTheme.colorScheme.primary,
                        )
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
            )
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { padding ->
        // Keyed on a coarse "which branch" marker rather than the whole
        // uiState — Loaded's isRefreshing flag toggling shouldn't cross-fade
        // the entire list out and back in on every background sync (that
        // would defeat the point of the quiet pulse dot above).
        AnimatedContent(
            targetState = when (uiState) {
                is ChatListUiState.Loading -> 0
                is ChatListUiState.Error -> 1
                is ChatListUiState.Loaded -> 2
            },
            transitionSpec = {
                // Cool, minimal cross-fade between loading/loaded/error —
                // no slide or bounce, just a clean dissolve (the same
                // restrained motion iOS uses switching between a skeleton
                // and its real content).
                (fadeIn(tween(260)) togetherWith fadeOut(tween(180)))
            },
            label = "chatListState",
        ) {
            when (val state = uiState) {
                is ChatListUiState.Loading -> {
                    LazyColumn(modifier = Modifier.padding(padding).fillMaxSize()) {
                        items(8) {
                            ShimmerChatRow()
                            HorizontalDivider(
                                color = MaterialTheme.colorScheme.surfaceVariant,
                                modifier = Modifier.padding(start = 72.dp),
                                thickness = 0.6.dp,
                            )
                        }
                    }
                }
                is ChatListUiState.Error -> {
                    Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(state.message, color = MaterialTheme.colorScheme.error)
                            Spacer(Modifier.height(12.dp))
                            Button(onClick = { viewModel.refresh() }) { Text("Retry") }
                        }
                    }
                }
                is ChatListUiState.Loaded -> {
                    if (state.chats.isEmpty()) {
                        Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                            Text("No chats yet", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    } else {
                        LazyColumn(
                            modifier = Modifier
                                .padding(padding)
                                .background(MaterialTheme.colorScheme.background),
                        ) {
                            items(state.chats, key = { it.id }) { chat ->
                                ChatRow(chat, onClick = { onOpenChat(chat) })
                                HorizontalDivider(
                                    color = MaterialTheme.colorScheme.surfaceVariant,
                                    modifier = Modifier.padding(start = 72.dp),
                                    thickness = 0.6.dp,
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

/**
 * The nav-bar brand mark: the physical KARLCON logo (mountain glyph +
 * wordmark, cropped from the brand sheet with the background stripped to
 * transparent) sitting on a pill-shaped green "ribbon" instead of a plain
 * text title. The ribbon carries a soft two-tone green gradient plus a
 * looping diagonal shine sweep — a subtle glint, like light passing over a
 * lacquered badge — so the brand corner reads as a designed mark rather
 * than a static label.
 */
@Composable
private fun BrandRibbon() {
    val shineTransition = rememberInfiniteTransition(label = "brandRibbonShine")
    val shineProgress by shineTransition.animateFloat(
        initialValue = -0.4f,
        targetValue = 1.4f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 2600, easing = LinearEasing),
            repeatMode = RepeatMode.Restart,
        ),
        label = "brandRibbonShineProgress",
    )

    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(50))
            .background(
                Brush.horizontalGradient(
                    colors = listOf(KarlonBrandGreen, KarlonRingGlow.copy(alpha = 0.85f), KarlonBrandGreen),
                ),
            )
            .background(
                // Faint top highlight so the ribbon reads as a raised,
                // slightly glossy pill rather than a flat fill.
                Brush.verticalGradient(
                    colors = listOf(Color.White.copy(alpha = 0.16f), Color.Transparent),
                ),
            )
            .padding(horizontal = 14.dp, vertical = 6.dp),
    ) {
        Image(
            painter = painterResource(R.drawable.karlcon_wordmark),
            contentDescription = "KARLCON Elite Retreats",
            contentScale = ContentScale.FillHeight,
            modifier = Modifier
                .height(30.dp)
                .drawWithShineSweep(shineProgress),
        )
    }
}

/**
 * Draws a thin bright diagonal band over the logo's own pixels and animates
 * it left to right on a loop — the classic "shine pass over a badge" glint.
 * Uses a Plus blend so the sweep only brightens what's already opaque
 * (the mountain glyph/wordmark) instead of painting a visible bar over the
 * transparent background around it.
 */
private fun Modifier.drawWithShineSweep(progress: Float): Modifier = this.drawWithContent {
    drawContent()
    val bandWidth = size.width * 0.25f
    val centerX = size.width * progress
    drawRect(
        brush = Brush.linearGradient(
            colors = listOf(
                Color.Transparent,
                KarlonRingCore.copy(alpha = 0.55f),
                Color.Transparent,
            ),
            start = Offset(centerX - bandWidth, 0f),
            end = Offset(centerX + bandWidth, size.height),
        ),
        blendMode = BlendMode.Plus,
    )
}

@Composable
private fun ChatRow(chat: ChatDto, onClick: () -> Unit) {
    var pressed by remember { mutableStateOf(false) }
    val rowBg by androidx.compose.animation.animateColorAsState(
        targetValue = if (pressed) KarlonBgCardPressed else MaterialTheme.colorScheme.background,
        animationSpec = tween(durationMillis = 150),
        label = "chatRowPress",
    )

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .background(rowBg)
            .padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        AvatarImage(
            photoUrl = chat.profilePicUrl,
            fallbackText = chat.avatarEmoji ?: chat.name,
            size = 44.dp,
            unreadCount = chat.unreadCount ?: 0,
        )
        Spacer(Modifier.width(14.dp))
        Column(modifier = Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (chat.isUnsaved) {
                    Box(
                        modifier = Modifier
                            .size(7.dp)
                            .background(KarlonRingGlow, shape = androidx.compose.foundation.shape.CircleShape),
                    )
                    Spacer(Modifier.width(6.dp))
                }
                Text(
                    text = chat.name,
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onBackground,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            Spacer(Modifier.height(2.dp))
            Text(
                text = chat.lastText ?: "No messages yet",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        // WhatsApp-style unread pill: true badge green, and fades out (not
        // an instant pop) the moment unreadCount drops to 0 — i.e. as soon
        // as the chat is opened and the server reports it read.
        androidx.compose.animation.AnimatedVisibility(
            visible = (chat.unreadCount ?: 0) > 0,
            enter = androidx.compose.animation.fadeIn(tween(180)) +
                androidx.compose.animation.scaleIn(tween(180), initialScale = 0.7f),
            exit = androidx.compose.animation.fadeOut(tween(280)),
        ) {
            Row {
                Spacer(Modifier.width(8.dp))
                Box(
                    modifier = Modifier
                        .background(KarlonWhatsAppGreen, shape = androidx.compose.foundation.shape.CircleShape)
                        .padding(horizontal = 7.dp, vertical = 2.dp),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = (chat.unreadCount ?: 0).coerceAtMost(99).toString(),
                        color = Color.White,
                        fontWeight = FontWeight.Bold,
                        fontSize = 12.sp,
                    )
                }
            }
        }
    }
}
