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

// App is now always-dark by design (iOS-style dark interface, matches the
// reference screenshots) — light scheme kept only as a fallback so nothing
// breaks if isSystemInDarkTheme() is ever forced off.
private val DarkColors = darkColorScheme(
    primary = KarlonGold,
    onPrimary = KarlonBgBase,
    primaryContainer = KarlonBrown,
    onPrimaryContainer = KarlonGold,
    secondary = KarlonRingGlow,
    onSecondary = KarlonBgBase,
    background = KarlonBgBase,
    onBackground = KarlonTextPrimary,
    surface = KarlonBgElevated,
    onSurface = KarlonTextPrimary,
    surfaceVariant = KarlonBgCard,
    onSurfaceVariant = KarlonTextSecondary,
    outline = KarlonDivider,
    error = KarlonError,
)

private val LightColors = lightColorScheme(
    primary = KarlonGoldDim,
    onPrimary = Color.White,
    primaryContainer = KarlonGold,
    onPrimaryContainer = KarlonBrownDark,
    secondary = KarlonRingGlow,
    onSecondary = Color.White,
    background = Color(0xFFF7F3EC),
    onBackground = Color(0xFF26201A),
    surface = Color.White,
    onSurface = Color(0xFF26201A),
    surfaceVariant = Color(0xFFEDE6D8),
    onSurfaceVariant = Color(0xFF5B5148),
    error = KarlonError,
)

@Composable
fun KarlonTheme(
    darkTheme: Boolean = true,
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
