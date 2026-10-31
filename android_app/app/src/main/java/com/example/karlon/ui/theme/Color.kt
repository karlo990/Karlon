package com.example.karlon.ui.theme

import androidx.compose.ui.graphics.Color

// ── KarShApp — pink, white and green ──────────────────────────────
// A light, white app with the brand pink as the accent and the badge's
// retreat green for outgoing messages and presence. The value names keep
// their old Karlon* spelling so every screen picks the new palette up
// unchanged; KarlonGold now holds the pink accent.

// Backgrounds — white base, blush surfaces and cards
val KarlonBgBase       = Color(0xFFFFFFFF)   // app background
val KarlonBgElevated   = Color(0xFFFFF5F8)   // bars, sheets
val KarlonBgCard       = Color(0xFFFDEEF3)   // row / card surface
val KarlonBgCardPressed = Color(0xFFF8DCE6)

// Brand accent — pink
val KarlonGold      = Color(0xFFD6336C)   // pink accent (buttons, focus, selection)
val KarlonGoldDim   = Color(0xFFB02A5B)
val KarlonBrown     = Color(0xFFF8C8D8)   // soft pink container
val KarlonBrownDark = Color(0xFF5A1A33)

// Text
val KarlonTextPrimary   = Color(0xFF1C1B1F)
val KarlonTextSecondary = Color(0xFF5F5B63)
val KarlonTextTertiary  = Color(0xFF8E8A93)
val KarlonDivider       = Color(0xFFF0D9E1)

// Message bubbles
val KarlonBubbleOut = Color(0xFF1D7A4C)   // outgoing — retreat green, light text
val KarlonBubbleIn  = Color(0xFFF3F1F4)   // incoming — soft grey on white

// Unread / presence ring — greens that still read on white
val KarlonRingGlow   = Color(0xFF1FA35A)   // the lit arc
/** True WhatsApp badge green — used specifically for the unread-count pill,
 * distinct from KarlonRingGlow used for the avatar ring/unsaved dot. */
val KarlonWhatsAppGreen = Color(0xFF25D366)
val KarlonRingTrack  = Color(0xFFD9EFE1)   // resting track (unlit portion)
val KarlonRingCore   = Color(0xFF2EC86F)   // core used for the glow blur pass
val KarlonOnlineDot  = Color(0xFF32D74B)   // small "online" status dot

val KarlonError = Color(0xFFC62828)

// Badge green — the ring of the KARSHAPP badge and the launcher icon.
val KarlonBrandGreen = Color(0xFF1F7A45)

// Start-up progress bar (iPhone style): blush track, green-to-pink fill
val KarShAppBarTrack = Color(0xFFF3D3DF)
val KarShAppBarStart = Color(0xFF1D7A4C)
val KarShAppBarEnd   = Color(0xFFE5568A)
