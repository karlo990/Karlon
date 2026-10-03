package com.example.karlon.ui.serverconfig

import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.karlon.R
import com.example.karlon.data.remote.RetrofitProvider
import com.example.karlon.data.remote.ServerConfig
import kotlinx.coroutines.launch

/** The Karlcon HF Space — a stable, always-on public hub, so a fresh
 * install works immediately without LAN IPs or tunnels. Change this if
 * you redeploy under a different HF username/space name. */
private const val DEFAULT_SERVER_URL = "https://davincii-code-karlcon.hf.space/"

/**
 * First screen the app shows (and reachable again via "Change server").
 * This is what makes "PC local server -> remote phone" actually work:
 * the user types in wherever Karlon is reachable — a LAN IP for
 * same-WiFi use, or a tunnel/VPN URL (ngrok, Tailscale, etc. — see
 * server/README.md) for a genuinely remote phone — and every screen
 * after this one talks to that address.
 */
@Composable
fun ServerSetupScreen(
    serverConfig: ServerConfig,
    onConnected: (baseUrl: String, displayName: String) -> Unit,
) {
    val scope = rememberCoroutineScope()
    // Defaults to the HF Space hub (always-on, no LAN/tunnel setup needed);
    // still fully editable — clear it and type a LAN IP for local dev.
    var serverUrl by remember { mutableStateOf(DEFAULT_SERVER_URL) }
    var displayName by remember { mutableStateOf("") }
    var isConnecting by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        serverConfig.currentServerUrl()?.let { serverUrl = it.removeSuffix("/") }
        serverConfig.currentDisplayName()?.let { displayName = it }
    }

    Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 28.dp)
                .padding(top = 64.dp, bottom = 32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Image(
                painter = painterResource(id = R.drawable.karlon_logo),
                contentDescription = "KARLCON Elite Retreats",
                modifier = Modifier
                    .fillMaxWidth(0.7f)
                    .padding(bottom = 32.dp),
            )

            Text(
                text = stringResource_(R.string.server_setup_title),
                style = MaterialTheme.typography.headlineSmall,
                textAlign = androidx.compose.ui.text.style.TextAlign.Center,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                text = stringResource_(R.string.server_setup_subtitle),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = androidx.compose.ui.text.style.TextAlign.Center,
            )

            Spacer(Modifier.height(32.dp))

            OutlinedTextField(
                value = serverUrl,
                onValueChange = { serverUrl = it; errorMessage = null },
                label = { Text(stringResource_(R.string.server_url_hint)) },
                singleLine = true,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
                modifier = Modifier.fillMaxWidth(),
            )

            Spacer(Modifier.height(16.dp))

            OutlinedTextField(
                value = displayName,
                onValueChange = { displayName = it },
                label = { Text(stringResource_(R.string.display_name_hint)) },
                singleLine = true,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                modifier = Modifier.fillMaxWidth(),
            )

            errorMessage?.let {
                Spacer(Modifier.height(12.dp))
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium)
            }

            Spacer(Modifier.height(28.dp))

            Button(
                onClick = {
                    val normalized = ServerConfig.normalize(serverUrl)
                    val name = displayName.ifBlank { "Guest" }
                    isConnecting = true
                    errorMessage = null
                    scope.launch {
                        val api = RetrofitProvider.buildApiService(normalized)
                        val reachable = try {
                            api.health().isSuccessful
                        } catch (_: Exception) {
                            false
                        }
                        isConnecting = false
                        if (reachable) {
                            serverConfig.save(normalized, name)
                            onConnected(normalized, name)
                        } else {
                            errorMessage = "Couldn't reach that server. Check the address and that Karlon is running."
                        }
                    }
                },
                enabled = serverUrl.isNotBlank() && !isConnecting,
                colors = ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.primary,
                    contentColor = MaterialTheme.colorScheme.onPrimary,
                ),
                modifier = Modifier.fillMaxWidth().height(50.dp),
            ) {
                if (isConnecting) {
                    CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
                } else {
                    Text(stringResource_(R.string.connect_button), fontWeight = FontWeight.Medium)
                }
            }
        }
    }
}

/** Small local alias so this file reads cleanly; delegates to the
 * standard Compose stringResource(). */
@Composable
private fun stringResource_(id: Int) = androidx.compose.ui.res.stringResource(id)
