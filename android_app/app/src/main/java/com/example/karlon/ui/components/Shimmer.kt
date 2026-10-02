package com.example.karlon.ui.components

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * A soft, continuously-sweeping highlight across a placeholder shape — the
 * same "shimmer" language iOS uses for skeleton loading (App Store, Wallet,
 * Photos all use a near-identical diagonal light sweep instead of a plain
 * grey block). Wrap any placeholder shape's Modifier with this instead of
 * a flat .background(color) while its real content is loading.
 */
@Composable
fun Modifier.shimmer(shape: Shape = RoundedCornerShape(8.dp)): Modifier {
    val transition = rememberInfiniteTransition(label = "shimmerTransition")
    val translateAnim by transition.animateFloat(
        initialValue = -1f,
        targetValue = 2f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 1300, easing = LinearEasing),
            repeatMode = RepeatMode.Restart,
        ),
        label = "shimmerTranslate",
    )
    val base = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.06f)
    val highlight = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.14f)
    val brush = Brush.linearGradient(
        colors = listOf(base, highlight, base),
        start = Offset(translateAnim * 400f - 200f, 0f),
        end = Offset(translateAnim * 400f + 200f, 200f),
    )
    return this
        .clip(shape)
        .background(brush)
}

/** One shimmering line — the basic unit skeleton rows are built from. */
@Composable
fun ShimmerLine(width: Dp, height: Dp = 14.dp) {
    Box(Modifier.width(width).height(height).shimmer())
}

/**
 * A skeleton row shaped like a chat-list entry (avatar circle + two lines
 * of text) — shown in place of real rows while the chat list is first
 * loading, instead of a single centered spinner blocking the whole screen.
 */
@Composable
fun ShimmerChatRow() {
    Row(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp),
    ) {
        Box(Modifier.size(44.dp).shimmer(CircleShape))
        Spacer(Modifier.width(14.dp))
        Column {
            ShimmerLine(width = 140.dp, height = 15.dp)
            Spacer(Modifier.height(6.dp))
            ShimmerLine(width = 200.dp, height = 13.dp)
        }
    }
}

/**
 * A skeleton block shaped like the invoice history row — used while
 * invoice history is loading, mirroring InvoiceHistoryRow's layout.
 */
@Composable
fun ShimmerHistoryRow() {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .shimmer(RoundedCornerShape(12.dp))
            .padding(horizontal = 14.dp, vertical = 12.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Column {
            ShimmerLine(width = 120.dp, height = 15.dp)
            Spacer(Modifier.height(6.dp))
            ShimmerLine(width = 170.dp, height = 12.dp)
        }
    }
}

/**
 * Small pulsing dot used next to a title while a background sync is in
 * progress (e.g. chat list polling/WS reconnect) — a much quieter signal
 * than a spinner, matching how iOS shows "Updating..." with a soft pulse
 * rather than a busy indicator once the initial load is done.
 */
@Composable
fun SyncPulseDot(color: Color = MaterialTheme.colorScheme.secondary) {
    val transition = rememberInfiniteTransition(label = "syncPulse")
    val scale by transition.animateFloat(
        initialValue = 0.6f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 700, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "syncPulseScale",
    )
    val alpha by transition.animateFloat(
        initialValue = 0.35f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 700, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "syncPulseAlpha",
    )
    Box(
        modifier = Modifier
            .size(8.dp)
            .graphicsLayer { scaleX = scale; scaleY = scale }
            .background(color.copy(alpha = alpha), CircleShape),
    )
}
