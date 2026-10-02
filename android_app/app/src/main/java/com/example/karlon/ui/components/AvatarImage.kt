package com.example.karlon.ui.components

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.blur
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.example.karlon.ui.theme.KarlonRingCore
import com.example.karlon.ui.theme.KarlonRingGlow
import com.example.karlon.ui.theme.KarlonRingTrack

/**
 * Round avatar, iOS-sized (compact, ~44dp default — matches the iOS
 * Messages row avatar rather than the old oversized Material one).
 *
 * The photo always fills the full circle edge-to-edge (ContentScale.Crop,
 * no inset) — no more visible background peeking around a small image.
 *
 * When [unreadCount] > 0, a slim glowing ring is drawn just outside the
 * avatar — same visual language as the Apple Watch Activity ring in the
 * reference: a soft blurred glow layer behind a crisp bright-green arc,
 * animating a gentle pulse so it reads as "live/unread" at a glance.
 */
@Composable
fun AvatarImage(
    photoUrl: String?,
    fallbackText: String,
    size: Dp = 44.dp,
    unreadCount: Int = 0,
    modifier: Modifier = Modifier,
) {
    val hasUnread = unreadCount > 0
    val ringWidth = 2.4.dp
    val ringGap = 3.dp
    val outerSize = if (hasUnread) size + (ringGap + ringWidth) * 2 else size

    val transition = rememberInfiniteTransition(label = "unread_ring_pulse")
    val pulse by transition.animateFloat(
        initialValue = 0.55f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(1100, easing = androidx.compose.animation.core.FastOutSlowInEasing),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "unread_ring_pulse_alpha",
    )

    Box(
        modifier = modifier.size(outerSize),
        contentAlignment = Alignment.Center,
    ) {
        if (hasUnread) {
            // Soft glow pass — blurred, wider stroke, sits behind the crisp ring
            androidx.compose.foundation.Canvas(
                modifier = Modifier
                    .size(outerSize)
                    .blur(6.dp),
            ) {
                val stroke = Stroke(width = ringWidth.toPx() * 2.4f, cap = StrokeCap.Round)
                val arcSize = Size(outerSize.toPx() - stroke.width, outerSize.toPx() - stroke.width)
                drawArc(
                    color = KarlonRingCore.copy(alpha = 0.55f * pulse),
                    startAngle = -90f,
                    sweepAngle = 360f,
                    useCenter = false,
                    style = stroke,
                    size = arcSize,
                    topLeft = androidx.compose.ui.geometry.Offset(stroke.width / 2, stroke.width / 2),
                )
            }
            // Crisp illuminated ring — dim track + bright glowing arc on top
            androidx.compose.foundation.Canvas(modifier = Modifier.size(outerSize)) {
                val stroke = Stroke(width = ringWidth.toPx(), cap = StrokeCap.Round)
                val inset = stroke.width / 2
                val arcSize = Size(outerSize.toPx() - stroke.width, outerSize.toPx() - stroke.width)
                // resting track (unlit)
                drawArc(
                    color = KarlonRingTrack,
                    startAngle = -90f,
                    sweepAngle = 360f,
                    useCenter = false,
                    style = stroke,
                    size = arcSize,
                    topLeft = androidx.compose.ui.geometry.Offset(inset, inset),
                )
                // illuminated portion — bright gradient arc, pulsing brightness
                drawArc(
                    brush = Brush.sweepGradient(listOf(KarlonRingGlow, KarlonRingCore, KarlonRingGlow)),
                    startAngle = -90f,
                    sweepAngle = 360f,
                    useCenter = false,
                    style = stroke,
                    size = arcSize,
                    topLeft = androidx.compose.ui.geometry.Offset(inset, inset),
                    alpha = pulse,
                )
            }
        }

        if (!photoUrl.isNullOrBlank()) {
            AsyncImage(
                model = photoUrl,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.size(size).clip(CircleShape),
            )
        } else {
            Box(
                modifier = Modifier
                    .size(size)
                    .clip(CircleShape)
                    .background(MaterialTheme.colorScheme.primaryContainer),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = fallbackText.take(2).uppercase(),
                    color = MaterialTheme.colorScheme.onPrimaryContainer,
                    style = MaterialTheme.typography.titleMedium,
                )
            }
        }
    }
}
