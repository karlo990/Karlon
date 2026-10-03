package com.example.karlon

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import com.example.karlon.data.remote.ServerConfig
import com.example.karlon.ui.navigation.KarlonNavHost
import com.example.karlon.ui.theme.KarlonTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val serverConfig = ServerConfig(applicationContext)

        setContent {
            KarlonTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    KarlonNavHost(serverConfig = serverConfig, context = applicationContext)
                }
            }
        }
    }
}
