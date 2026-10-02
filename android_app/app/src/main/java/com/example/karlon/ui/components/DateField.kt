package com.example.karlon.ui.components

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CalendarMonth
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextFieldColors
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * A check-in/check-out style date field: still a plain YYYY-MM-DD text
 * field (so a date can be typed or cleared by hand exactly as before),
 * but now with a 📅 calendar-icon button that opens a real date picker
 * instead of making the front desk count out the format by eye.
 *
 * Used for both the invoice form's check-in/check-out and the
 * available-houses popup's date-window fields — same widget, same feel,
 * in both places.
 *
 * Typed input is sanitized through [sanitizeDateInput] before it reaches
 * [onValueChange] — previously a stray keystroke could produce something
 * like "20226-09-07" (an extra digit slipped into the year) with no
 * feedback, which the server then treated as literally that string and
 * simply never matched any listing.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DateField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    fieldColors: TextFieldColors,
    modifier: Modifier = Modifier,
) {
    var showPicker by remember { mutableStateOf(false) }

    OutlinedTextField(
        value = value,
        onValueChange = { onValueChange(sanitizeDateInput(it)) },
        label = { Text(label) },
        placeholder = { Text("YYYY-MM-DD") },
        singleLine = true,
        colors = fieldColors,
        modifier = modifier,
        trailingIcon = {
            IconButton(onClick = { showPicker = true }) {
                Icon(Icons.Default.CalendarMonth, contentDescription = "Pick $label date")
            }
        },
    )

    if (showPicker) {
        val initialMillis = value.trim().takeIf { it.isNotBlank() }?.let { parseIsoDateToMillis(it) }
        val pickerState = rememberDatePickerState(initialSelectedDateMillis = initialMillis)
        DatePickerDialog(
            onDismissRequest = { showPicker = false },
            confirmButton = {
                TextButton(onClick = {
                    pickerState.selectedDateMillis?.let { onValueChange(millisToIsoDate(it)) }
                    showPicker = false
                }) { Text("OK") }
            },
            dismissButton = {
                TextButton(onClick = { showPicker = false }) { Text("Cancel") }
            },
        ) {
            DatePicker(state = pickerState)
        }
    }
}

private val isoFormatter = DateTimeFormatter.ISO_LOCAL_DATE

/**
 * Keeps free-typed input inside the YYYY-MM-DD shape: digits and dashes
 * only, hard-capped at 10 characters (the exact length of "YYYY-MM-DD").
 * This doesn't guarantee a *valid* calendar date — the picker is the
 * reliable path for that — it just makes it impossible to type something
 * the wrong length or shape, which is what produced the malformed
 * 11-character year in the first place.
 */
private fun sanitizeDateInput(raw: String): String =
    raw.filter { it.isDigit() || it == '-' }.take(10)

private fun millisToIsoDate(millis: Long): String =
    Instant.ofEpochMilli(millis).atZone(ZoneOffset.UTC).toLocalDate().format(isoFormatter)

private fun parseIsoDateToMillis(text: String): Long? =
    runCatching {
        java.time.LocalDate.parse(text, isoFormatter)
            .atStartOfDay(ZoneOffset.UTC)
            .toInstant()
            .toEpochMilli()
    }.getOrNull()
