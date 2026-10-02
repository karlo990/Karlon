package com.example.karlon.ui.navigation

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.CreationExtras

/** Tiny generic factory so each screen can construct its ViewModel with
 * constructor args (a repository, a chatId, ...) without pulling in a DI
 * framework for what's otherwise a small app. */
fun <T : ViewModel> viewModelFactory(build: () -> T): ViewModelProvider.Factory =
    object : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <U : ViewModel> create(modelClass: Class<U>, extras: CreationExtras): U =
            build() as U
    }
