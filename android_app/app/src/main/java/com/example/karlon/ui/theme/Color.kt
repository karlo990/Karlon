package com.example.karlon.ui.theme

import androidx.compose.ui.graphics.Color

// ── KARLCON — iOS-style dark palette ──────────────────────────────
// Rebuilt around a near-black backdrop (iOS "system background dark")
// with a warm gold accent kept from the brand mark, plus a dedicated
// glow-green reserved only for the unread ring (sampled from the
// watch-face reference: bright mint-green glow over a darker green
// track, exactly like the Activity ring).

// Backgrounds — layered like iOS: base -> elevated surface -> card
val KarlonBgBase       = Color(0xFF0A0A0C)   // true near-black app background
val KarlonBgElevated   = Color(0xFF1C1C1E)   // iOS "secondarySystemBackground" dark
val KarlonBgCard       = Color(0xFF232326)   // row / card surface, sits above base
val KarlonBgCardPressed = Color(0xFF2C2C2E)

// Brand accent — gold, kept from the KARLCON wordmark but tuned to glow on dark
val KarlonGold      = Color(0xFFE8B84D)
val KarlonGoldDim   = Color(0xFFB8903A)
val KarlonBrown     = Color(0xFF6B4A23)
val KarlonBrownDark = Color(0xFF4A3117)

// Text
val KarlonTextPrimary   = Color(0xFFF5F5F7)  // iOS label dark
val KarlonTextSecondary = Color(0xFFA0A0A6)  // iOS secondaryLabel dark
val KarlonTextTertiary  = Color(0xFF6C6C70)
val KarlonDivider       = Color(0xFF2C2C2E)

// Message bubbles — kept legible on the new dark canvas
val KarlonBubbleOut = Color(0xFF1D7A4C)   // outgoing — retreat green, deeper for contrast
val KarlonBubbleIn  = Color(0xFF232326)   // incoming — dark card grey

// Unread / presence ring — the "Apple Watch Activity ring" glow
val KarlonRingGlow   = Color(0xFF30E070)   // bright mint-green, the illuminated arc
/** True WhatsApp badge green — used specifically for the unread-count pill,
 * distinct from KarlonRingGlow's cooler mint used for the avatar ring/unsaved dot. */
val KarlonWhatsAppGreen = Color(0xFF25D366)
val KarlonRingTrack  = Color(0xFF163823)   // dim resting track (unlit portion)
val KarlonRingCore   = Color(0xFF6BFFAA)   // hot core used for the glow blur pass
val KarlonOnlineDot  = Color(0xFF32D74B)   // small "online" status dot, iOS system green

val KarlonError = Color(0xFFFF6B5E)

// Brand green sampled from karlcon_app_icon.png — the launcher icon's fill
// color. Kept here so it can be reused anywhere else in the UI on request,
// distinct from the gold-based accent used everywhere else today.
val KarlonBrandGreen = Color(0xFF1E623D)
