package com.example.karlon.ui.components

import androidx.compose.animation.core.EaseOutCubic
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * Apple-style "frosted glass" surface — the look used across iOS/visionOS
 * sheets and cards: a translucent tinted fill, a soft brighter highlight
 * along the top edge (as if light were catching the top of the pane), and
 * a hairline 1px border a shade brighter than the fill. There's no real
 * background blur here (Compose has no cheap cross-platform blur-behind on
 * older API levels), so the "glass" reads through layered translucency and
 * a gradient instead — the same trick iOS used before real backdrop blur.
 *
 * Usage: Modifier.glassSurface() on a Box/Column instead of
 * .background(color, shape).
 */
fun Modifier.glassSurface(
    shape: Shape = RoundedCornerShape(20.dp),
    tint: Color = Color.White,
    tintAlpha: Float = 0.06f,
    borderAlpha: Float = 0.14f,
): Modifier = this
    .clip(shape)
    .background(
        Brush.verticalGradient(
            colors = listOf(
                // brighter sliver at the top — the "light catching glass" edge
                tint.copy(alpha = tintAlpha * 1.8f),
                tint.copy(alpha = tintAlpha),
            ),
        ),
    )
    .border(1.dp, tint.copy(alpha = borderAlpha), shape)

/**
 * Same idea, but as a standalone composable when you want the glass pane to
 * own its own padding/content slot rather than applying the modifier
 * directly to an existing layout.
 */
@Composable
fun GlassCard(
    modifier: Modifier = Modifier,
    shape: Shape = RoundedCornerShape(20.dp),
    tint: Color = Color.White,
    tintAlpha: Float = 0.06f,
    content: @Composable () -> Unit,
) {
    Box(modifier = modifier.glassSurface(shape = shape, tint = tint, tintAlpha = tintAlpha)) {
        content()
    }
}

/**
 * A glass pane that eases in with a gentle rise + fade the first time it
 * enters composition — used for form cards and dialogs so they don't just
 * snap on screen. Apple's own sheets/cards use this exact "settle in from
 * slightly below, slightly faded" motion.
 */
@Composable
fun AnimatedGlassCard(
    modifier: Modifier = Modifier,
    shape: Shape = RoundedCornerShape(20.dp),
    tint: Color = Color.White,
    tintAlpha: Float = 0.06f,
    riseDistance: Dp = 14.dp,
    content: @Composable () -> Unit,
) {
    var entered by remember { mutableStateOf(false) }
    val progress by animateFloatAsState(
        targetValue = if (entered) 1f else 0f,
        animationSpec = tween(durationMillis = 420, easing = EaseOutCubic),
        label = "glassCardEnter",
    )
    LaunchedEffect(Unit) { entered = true }
    val density = LocalDensity.current
    val riseDistancePx = with(density) { riseDistance.toPx() }

    Box(
        modifier = modifier
            .graphicsLayer {
                alpha = progress
                translationY = riseDistancePx * (1f - progress)
            }
            .glassSurface(shape = shape, tint = tint, tintAlpha = tintAlpha),
    ) {
        content()
    }
}
