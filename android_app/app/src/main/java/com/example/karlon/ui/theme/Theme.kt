package com.example.karlon.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

// KarShApp is light by design: white with pink and green. The dark scheme
// is kept only as a fallback.
private val DarkColors = darkColorScheme(
    primary = KarlonGold,
    onPrimary = Color.White,
    primaryContainer = KarlonGoldDim,
    onPrimaryContainer = Color.White,
    secondary = KarlonRingGlow,
    onSecondary = Color.White,
    background = Color(0xFF121014),
    onBackground = Color(0xFFF5F2F6),
    surface = Color(0xFF1C191E),
    onSurface = Color(0xFFF5F2F6),
    surfaceVariant = Color(0xFF2A2530),
    onSurfaceVariant = Color(0xFFBDB6C2),
    error = KarlonError,
)

private val LightColors = lightColorScheme(
    primary = KarlonGold,
    onPrimary = Color.White,
    primaryContainer = KarlonBrown,
    onPrimaryContainer = KarlonBrownDark,
    secondary = KarlonBrandGreen,
    onSecondary = Color.White,
    tertiary = KarlonRingGlow,
    background = KarlonBgBase,
    onBackground = KarlonTextPrimary,
    surface = KarlonBgElevated,
    onSurface = KarlonTextPrimary,
    surfaceVariant = KarlonBgCard,
    onSurfaceVariant = KarlonTextSecondary,
    outline = KarlonDivider,
    error = KarlonError,
)

@Composable
fun KarlonTheme(
    darkTheme: Boolean = false,
    content: @Composable () -> Unit,
) {
    val colorScheme = if (darkTheme) DarkColors else LightColors
    val view = LocalView.current
    if (!view.isInEditMode) {
        val window = (view.context as? android.app.Activity)?.window
        window?.let {
            WindowCompat.getInsetsController(it, view).isAppearanceLightStatusBars = !darkTheme
            it.statusBarColor = colorScheme.background.toArgb()
            it.navigationBarColor = colorScheme.background.toArgb()
        }
    }

    MaterialTheme(
        colorScheme = colorScheme,
        typography = KarlonTypography,
        content = content,
    )
}
