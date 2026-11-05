package com.pcmote.ui.theme

import android.os.Build
import androidx.annotation.RequiresApi
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.MaterialExpressiveTheme
import androidx.compose.material3.MotionScheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalContext

private val FallbackDarkScheme = darkColorScheme()
private val FallbackLightScheme = lightColorScheme()

@RequiresApi(Build.VERSION_CODES.S)
@Composable
private fun dynamicScheme(darkTheme: Boolean) = if (darkTheme) {
    dynamicDarkColorScheme(LocalContext.current)
} else {
    dynamicLightColorScheme(LocalContext.current)
}

/**
 * Material 3 Expressive theme. On Android 12+ the palette is taken from the wallpaper,
 * otherwise the built-in baseline scheme is used.
 */
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun PcmoteTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit
) {
    val colorScheme = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S -> dynamicScheme(darkTheme)
        darkTheme -> FallbackDarkScheme
        else -> FallbackLightScheme
    }

    MaterialExpressiveTheme(
        colorScheme = colorScheme,
        motionScheme = MotionScheme.expressive(),
        content = content
    )
}
