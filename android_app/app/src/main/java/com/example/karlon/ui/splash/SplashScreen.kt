package com.example.karlon.ui.splash

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.ProgressBarRangeInfo
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.progressBarRangeInfo
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.example.karlon.R
import com.example.karlon.ui.theme.KarShAppBarEnd
import com.example.karlon.ui.theme.KarShAppBarStart
import com.example.karlon.ui.theme.KarShAppBarTrack
import com.example.karlon.ui.theme.KarlonBgElevated

/**
 * Start-up screen: the KARSHAPP badge over white, with a thin rounded
 * progress bar under it like the one an iPhone shows while it boots. The bar
 * moves in a few uneven steps (fast, pause, fast) instead of one smooth sweep,
 * which is what makes the iPhone bar feel like real loading. About 1.8 s,
 * then [onFinished].
 */
@Composable
fun SplashScreen(onFinished: () -> Unit) {
    val progress = remember { Animatable(0f) }
    LaunchedEffect(Unit) {
        progress.animateTo(0.35f, tween(420, easing = FastOutSlowInEasing))
        progress.animateTo(0.42f, tween(260, easing = LinearOutSlowInEasing))
        progress.animateTo(0.78f, tween(480, easing = FastOutSlowInEasing))
        progress.animateTo(1f, tween(380, easing = LinearOutSlowInEasing))
        onFinished()
    }

    val loading = stringResource(R.string.splash_loading)
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Brush.verticalGradient(listOf(Color.White, Color.White, KarlonBgElevated))),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Image(
                painter = painterResource(R.drawable.karshapp_badge),
                contentDescription = "KarShApp Elite Retreats",
                modifier = Modifier.width(200.dp),
            )
            Spacer(Modifier.height(44.dp))
            Box(
                modifier = Modifier
                    .width(180.dp)
                    .height(5.dp)
                    .clip(RoundedCornerShape(50))
                    .background(KarShAppBarTrack)
                    .semantics {
                        contentDescription = loading
                        progressBarRangeInfo = ProgressBarRangeInfo(progress.value, 0f..1f)
                    },
            ) {
                Box(
                    modifier = Modifier
                        .fillMaxHeight()
                        .width(180.dp * progress.value)
                        .clip(RoundedCornerShape(50))
                        .background(Brush.horizontalGradient(listOf(KarShAppBarStart, KarShAppBarEnd))),
                )
            }
        }
    }
}
