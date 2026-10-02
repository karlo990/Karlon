package com.example.karlon.ui.chatlist

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.karlon.data.model.ChatDto
import com.example.karlon.data.repository.ChatRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

sealed interface ChatListUiState {
    data object Loading : ChatListUiState
    data class Loaded(val chats: List<ChatDto>, val isRefreshing: Boolean = false) : ChatListUiState
    data class Error(val message: String) : ChatListUiState
}

class ChatListViewModel(private val repository: ChatRepository) : ViewModel() {

    private val _uiState = MutableStateFlow<ChatListUiState>(ChatListUiState.Loading)
    val uiState: StateFlow<ChatListUiState> = _uiState

    init {
        refresh()
    }

    /**
     * Loads (or reloads) the chat list. When a list is already on screen this
     * is treated as a quiet background sync — the existing chats stay put
     * with `isRefreshing = true` (shown as a small pulse dot next to the nav
     * title in ChatListScreen), instead of dropping back to a blank loading
     * skeleton on every poll/manual refresh. Only the very first load, or a
     * refresh with nothing successfully loaded yet, shows the full Loading
     * skeleton state.
     */
    fun refresh() {
        viewModelScope.launch {
            val current = _uiState.value
            _uiState.value = when (current) {
                is ChatListUiState.Loaded -> current.copy(isRefreshing = true)
                else -> ChatListUiState.Loading
            }
            try {
                val chats = repository.getChats()
                _uiState.value = ChatListUiState.Loaded(chats, isRefreshing = false)
            } catch (e: Exception) {
                // A failed background sync shouldn't blow away a perfectly
                // good list that's already on screen — just stop pulsing and
                // let the next successful poll update it. Only surface a
                // full error state if we had nothing loaded to fall back on.
                _uiState.value = when (current) {
                    is ChatListUiState.Loaded -> current.copy(isRefreshing = false)
                    else -> ChatListUiState.Error(e.message ?: "Couldn't load chats")
                }
            }
        }
    }
}
