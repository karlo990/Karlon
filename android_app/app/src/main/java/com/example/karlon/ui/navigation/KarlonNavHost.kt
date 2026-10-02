package com.example.karlon.ui.navigation

import android.net.Uri
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.Receipt
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.example.karlon.data.model.ChatDto
import com.example.karlon.data.remote.ServerConfig
import com.example.karlon.data.repository.ChatRepository
import com.example.karlon.ui.chatdetail.ChatDetailScreen
import com.example.karlon.ui.chatdetail.ChatDetailViewModel
import com.example.karlon.ui.chatlist.ChatListScreen
import com.example.karlon.ui.chatlist.ChatListViewModel
import com.example.karlon.ui.invoice.InvoiceScreen
import com.example.karlon.ui.invoice.InvoiceViewModel
import com.example.karlon.ui.serverconfig.ServerSetupScreen
import com.example.karlon.ui.theme.KarlonGold

private object Routes {
    const val SERVER_SETUP = "server_setup"
    const val HOME = "home"
    const val CHAT_DETAIL = "chat_detail/{chatId}/{chatName}/{avatarTag}/{profilePic}"
    const val INVOICE_FROM_CHAT = "invoice_from_chat/{chatId}/{chatName}"

    fun chatDetail(chat: ChatDto) =
        "chat_detail/${Uri.encode(chat.id)}/${Uri.encode(chat.name)}/" +
            "${Uri.encode(chat.avatarEmoji ?: "?")}/${Uri.encode(chat.profilePicUrl ?: "")}"

    fun invoiceFromChat(chat: ChatDto) =
        "invoice_from_chat/${Uri.encode(chat.id)}/${Uri.encode(chat.name)}"
}

/**
 * Top-level nav graph. Whether we land on the server-setup screen or
 * straight on the chat list depends on whether a server URL is already
 * saved in DataStore — that read is suspend-only, so it happens inside
 * a [LaunchedEffect] rather than via `runBlocking` (which would freeze
 * the UI thread on every app launch, a real bug in an earlier draft of
 * this file).
 */
@Composable
fun KarlonNavHost(
    serverConfig: ServerConfig,
    context: android.content.Context,
) {
    val navController = rememberNavController()

    // Repository is recreated per base URL; this holder lets nested
    // screens grab the "current" one without re-plumbing it through
    // every NavHost argument.
    var repository by remember { mutableStateOf<ChatRepository?>(null) }
    var displayName by remember { mutableStateOf("") }

    // null = still checking DataStore; non-null = resolved start route.
    // Rendering waits on this instead of blocking with runBlocking.
    var startDestination by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        val savedUrl = serverConfig.currentServerUrl()
        if (savedUrl != null) {
            repository = ChatRepository(savedUrl, context)
            displayName = serverConfig.currentDisplayName() ?: "Guest"
            startDestination = Routes.HOME
        } else {
            startDestination = Routes.SERVER_SETUP
        }
    }

    val resolvedStart = startDestination
    if (resolvedStart == null) {
        // Brief splash while DataStore is read.
        Surface(modifier = Modifier.fillMaxSize()) {
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
        }
        return
    }

    NavHost(navController = navController, startDestination = resolvedStart) {

        composable(Routes.SERVER_SETUP) {
            ServerSetupScreen(
                serverConfig = serverConfig,
                onConnected = { baseUrl, name ->
                    repository = ChatRepository(baseUrl, context)
                    displayName = name
                    navController.navigate(Routes.HOME) {
                        popUpTo(Routes.SERVER_SETUP) { inclusive = true }
                    }
                },
            )
        }

        composable(Routes.HOME) {
            val repo = repository
            if (repo == null) {
                LaunchedEffect(Unit) {
                    navController.navigate(Routes.SERVER_SETUP) { popUpTo(0) }
                }
            } else {
                HomeTabsScreen(
                    repository = repo,
                    displayName = displayName,
                    onOpenChat = { chat -> navController.navigate(Routes.chatDetail(chat)) },
                    onOpenSettings = { navController.navigate(Routes.SERVER_SETUP) },
                )
            }
        }

        composable(
            route = Routes.CHAT_DETAIL,
            arguments = listOf(
                navArgument("chatId") { type = NavType.StringType },
                navArgument("chatName") { type = NavType.StringType },
                navArgument("avatarTag") { type = NavType.StringType },
                navArgument("profilePic") { type = NavType.StringType },
            ),
        ) { backStackEntry ->
            val repo = repository
            val chatId = backStackEntry.arguments?.getString("chatId").orEmpty()
            val chatName = backStackEntry.arguments?.getString("chatName").orEmpty()
            val avatarTag = backStackEntry.arguments?.getString("avatarTag").orEmpty()
            val profilePic = backStackEntry.arguments?.getString("profilePic").orEmpty()

            if (repo == null) {
                LaunchedEffect(Unit) {
                    navController.navigate(Routes.SERVER_SETUP) { popUpTo(0) }
                }
            } else {
                val chat = ChatDto(
                    id = chatId,
                    name = chatName,
                    avatarEmoji = avatarTag,
                    profilePicUrl = profilePic.ifBlank { null },
                    lastText = null,
                    lastAt = null,
                )
                val viewModel: ChatDetailViewModel = viewModel(
                    key = chatId,
                    factory = viewModelFactory { ChatDetailViewModel(repo, chatId, displayName) },
                )
                ChatDetailScreen(
                    chat = chat,
                    displayName = displayName,
                    viewModel = viewModel,
                    onBack = { navController.popBackStack() },
                    onGenerateInvoice = { navController.navigate(Routes.invoiceFromChat(chat)) },
                )
            }
        }

        composable(
            route = Routes.INVOICE_FROM_CHAT,
            arguments = listOf(
                navArgument("chatId") { type = NavType.StringType },
                navArgument("chatName") { type = NavType.StringType },
            ),
        ) { backStackEntry ->
            val repo = repository
            val chatId = backStackEntry.arguments?.getString("chatId").orEmpty()
            val chatName = backStackEntry.arguments?.getString("chatName").orEmpty()
            if (repo == null) {
                LaunchedEffect(Unit) {
                    navController.navigate(Routes.SERVER_SETUP) { popUpTo(0) }
                }
            } else {
                val prefillChat = ChatDto(
                    id = chatId, name = chatName, avatarEmoji = null,
                    profilePicUrl = null, lastText = null, lastAt = null,
                )
                val viewModel: InvoiceViewModel = viewModel(
                    key = "invoice_from_chat_$chatId",
                    factory = viewModelFactory {
                        InvoiceViewModel(repo, displayName, prefillFromChat = prefillChat)
                    },
                )
                InvoiceScreenWithBack(viewModel = viewModel, onBack = { navController.popBackStack() })
            }
        }
    }
}

/** [InvoiceScreen] wrapped with a back-enabled top bar — the tab version
 * (inside [HomeTabsScreen]) has no back button since it's a bottom-nav
 * destination, but this one is pushed onto the stack from a chat thread
 * and needs a way back to that chat. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun InvoiceScreenWithBack(viewModel: InvoiceViewModel, onBack: () -> Unit) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Generate invoice") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Default.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
    ) { padding ->
        Box(modifier = Modifier.padding(padding)) {
            InvoiceScreen(viewModel = viewModel)
        }
    }
}

/**
 * Bottom-nav shell for the two top-level destinations (Chats / Invoices).
 * Each tab keeps its own ViewModel instance across tab switches (keyed by
 * tab index) so scroll position / in-progress form state survives
 * switching tabs and coming back — only actually leaving HOME (e.g. into
 * a chat detail) tears them down.
 */
@Composable
private fun HomeTabsScreen(
    repository: ChatRepository,
    displayName: String,
    onOpenChat: (ChatDto) -> Unit,
    onOpenSettings: () -> Unit,
) {
    var selectedTab by remember { mutableIntStateOf(0) }

    Scaffold(
        bottomBar = {
            // Curved top edge instead of the flat default NavigationBar edge,
            // docked in a rounded "floating dock" card so it reads as a
            // deliberately shaped bottom section rather than a straight bar
            // — the warm gold/brown tone carries through from the rest of
            // the app's theme (KarlonGold), just applied to the bar itself
            // now instead of only the selected icon.
            Surface(
                shape = androidx.compose.foundation.shape.RoundedCornerShape(
                    topStart = 24.dp,
                    topEnd = 24.dp,
                ),
                color = MaterialTheme.colorScheme.background,
                tonalElevation = 3.dp,
                shadowElevation = 8.dp,
            ) {
                NavigationBar(
                    containerColor = androidx.compose.ui.graphics.Color.Transparent,
                    tonalElevation = 0.dp,
                    modifier = Modifier.padding(top = 6.dp),
                ) {
                    NavigationBarItem(
                        selected = selectedTab == 0,
                        onClick = { selectedTab = 0 },
                        icon = { Icon(Icons.Default.Chat, contentDescription = "Chats") },
                        label = { Text("Chats", fontWeight = if (selectedTab == 0) FontWeight.Bold else FontWeight.Normal) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = KarlonGold,
                            selectedTextColor = KarlonGold,
                            indicatorColor = KarlonGold.copy(alpha = 0.18f),
                        ),
                    )
                    NavigationBarItem(
                        selected = selectedTab == 1,
                        onClick = { selectedTab = 1 },
                        icon = { Icon(Icons.Default.Receipt, contentDescription = "Invoices") },
                        label = { Text("Invoices", fontWeight = if (selectedTab == 1) FontWeight.Bold else FontWeight.Normal) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = KarlonGold,
                            selectedTextColor = KarlonGold,
                            indicatorColor = KarlonGold.copy(alpha = 0.18f),
                        ),
                    )
                }
            }
        },
    ) { padding ->
        Box(modifier = Modifier.padding(padding).fillMaxSize()) {
            when (selectedTab) {
                0 -> {
                    val viewModel: ChatListViewModel = viewModel(
                        key = "chat_list_tab",
                        factory = viewModelFactory { ChatListViewModel(repository) },
                    )
                    ChatListScreen(
                        viewModel = viewModel,
                        onOpenChat = onOpenChat,
                        onOpenSettings = onOpenSettings,
                    )
                }
                1 -> {
                    val viewModel: InvoiceViewModel = viewModel(
                        key = "invoice_tab",
                        factory = viewModelFactory { InvoiceViewModel(repository, displayName) },
                    )
                    InvoiceScreen(viewModel = viewModel)
                }
            }
        }
    }
}
