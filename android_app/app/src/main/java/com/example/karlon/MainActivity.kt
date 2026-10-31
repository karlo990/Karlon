package com.example.karlon

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import com.example.karlon.data.remote.ServerConfig
import com.example.karlon.ui.navigation.KarlonNavHost
import com.example.karlon.ui.splash.SplashScreen
import com.example.karlon.ui.theme.KarlonTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val serverConfig = ServerConfig(applicationContext)

        setContent {
            KarlonTheme {
                // The badge and progress bar once per launch (not again on rotation)
                var starting by rememberSaveable { mutableStateOf(true) }
                Surface(modifier = Modifier.fillMaxSize()) {
                    if (starting) {
                        SplashScreen(onFinished = { starting = false })
                    } else {
                        KarlonNavHost(serverConfig = serverConfig, context = applicationContext)
                    }
                }
            }
        }
    }
}
