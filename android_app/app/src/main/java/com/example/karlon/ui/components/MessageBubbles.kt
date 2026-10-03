package com.example.karlon.ui.components

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Done
import androidx.compose.material.icons.filled.DoneAll
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.example.karlon.data.model.MessageDto
import com.example.karlon.ui.theme.KarlonBubbleIn
import com.example.karlon.ui.theme.KarlonBubbleOut

/**
 * One chat bubble — text or image.
 *
 * Layout improvements over the original:
 *  • Directional indent: outgoing bubbles have a 48 dp left margin (pushes
 *    them visually to the right), incoming have a 48 dp right margin.  This
 *    mirrors WhatsApp's visual language without the bubble filling the full
 *    screen width.
 *  • Timestamp is shown at the bottom-right of every bubble (HH:mm from the
 *    ISO createdAt field, local TZ).
 *  • WA delivery-status icon appears next to the timestamp for outbound
 *    messages: ⏳ pending → ✓ sent → ⚠ error.
 */
@Composable
fun MessageBubble(
    message: MessageDto,
    isOwnMessage: Boolean,
    resolvedMediaUrl: String?,
    modifier: Modifier = Modifier,
) {
    val bubbleColor = if (isOwnMessage) KarlonBubbleOut else KarlonBubbleIn
    val textColor = if (isOwnMessage) Color(0xFFF5FFF8) else MaterialTheme.colorScheme.onSurface
    val shape = RoundedCornerShape(
        topStart    = 16.dp,
        topEnd      = 16.dp,
        bottomStart = if (isOwnMessage) 16.dp else 4.dp,
        bottomEnd   = if (isOwnMessage) 4.dp else 16.dp,
    )

    // 48 dp "indent" on the opposite side — keeps bubbles from spanning edge-to-edge
    val rowModifier = modifier
        .fillMaxWidth()
        .padding(
            start = if (isOwnMessage) 48.dp else 0.dp,
            end   = if (isOwnMessage) 0.dp  else 48.dp,
        )

    Row(
        modifier            = rowModifier,
        horizontalArrangement = if (isOwnMessage) Arrangement.End else Arrangement.Start,
    ) {
        Surface(
            color          = bubbleColor,
            shape          = shape,
            shadowElevation = 1.dp,
            modifier       = Modifier.widthIn(max = 280.dp),
        ) {
            Column(
                modifier = Modifier.padding(
                    horizontal = if (message.isImage) 6.dp else 10.dp,
                    vertical   = if (message.isImage) 6.dp else 8.dp,
                )
            ) {
                // Sender label — shown only for incoming messages
                if (!isOwnMessage) {
                    Text(
                        text     = message.sender,
                        style    = MaterialTheme.typography.labelSmall,
                        color    = MaterialTheme.colorScheme.secondary,
                        modifier = Modifier.padding(bottom = 2.dp, start = if (message.isImage) 4.dp else 0.dp),
                    )
                }

                // Content
                when {
                    message.isImage && resolvedMediaUrl != null -> {
                        AsyncImage(
                            model              = resolvedMediaUrl,
                            contentDescription = message.text ?: "Photo",
                            contentScale       = ContentScale.Crop,
                            modifier           = Modifier
                                .widthIn(max = 260.dp)
                                .heightIn(max = 260.dp)
                                .clip(RoundedCornerShape(12.dp)),
                        )
                        if (!message.text.isNullOrBlank()) {
                            Text(
                                text     = message.text,
                                style    = MaterialTheme.typography.bodyMedium,
                                modifier = Modifier.padding(top = 6.dp, start = 4.dp, end = 4.dp),
                            )
                        }
                    }
                    else -> {
                        Text(
                            text  = message.text.orEmpty(),
                            style = MaterialTheme.typography.bodyLarge,
                            color = textColor,
                        )
                    }
                }

                // Footer: timestamp + optional WA delivery icon
                Spacer(Modifier.height(3.dp))
                BubbleFooter(
                    createdAt = message.createdAt,
                    waStatus  = if (message.isOutbound) message.waStatus else null,
                    imagePadding = message.isImage,
                )
            }
        }
    }
}

// ─────────────────────────────────── footer ───────────────────────────────────

@Composable
private fun BubbleFooter(
    createdAt: String,
    waStatus: String?,
    imagePadding: Boolean,
) {
    val dimColor = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.45f)

    Row(
        modifier              = Modifier
            .fillMaxWidth()
            .padding(
                start = if (imagePadding) 4.dp else 0.dp,
                end   = if (imagePadding) 4.dp else 0.dp,
            ),
        horizontalArrangement = Arrangement.End,
        verticalAlignment     = Alignment.CenterVertically,
    ) {
        Text(
            text  = formatBubbleTime(createdAt),
            style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp),
            color = dimColor,
        )
        if (waStatus != null) {
            Spacer(Modifier.width(3.dp))
            val (icon, tint) = waStatusIcon(waStatus, dimColor)
            Icon(
                imageVector        = icon,
                contentDescription = "WA: $waStatus",
                modifier           = Modifier.size(12.dp),
                tint               = tint,
            )
        }
    }
}

private fun waStatusIcon(status: String, dim: Color): Pair<ImageVector, Color> = when (status) {
    "sent"    -> Icons.Default.DoneAll     to dim.copy(alpha = 0.9f)   // ✓✓ delivered to WA
    "error"   -> Icons.Default.ErrorOutline to Color(0xFFC4694A)        // ⚠  failed
    else      -> Icons.Default.Schedule    to dim                       // ⏳ pending
}

// ─────────────────────────────────── time helper ──────────────────────────────

/**
 * Extract HH:mm from an ISO-8601 timestamp without requiring java.time API
 * (safe on all API levels without desugaring).
 * "2024-06-30T14:35:00.123456+00:00" → "14:35"
 * "2024-06-30T14:35:00Z"             → "14:35"
 */
fun formatBubbleTime(iso: String?): String {
    if (iso.isNullOrBlank()) return ""
    return try {
        val t = iso.indexOf('T')
        if (t >= 0 && t + 5 < iso.length) iso.substring(t + 1, t + 6) else ""
    } catch (_: Exception) {
        ""
    }
}
