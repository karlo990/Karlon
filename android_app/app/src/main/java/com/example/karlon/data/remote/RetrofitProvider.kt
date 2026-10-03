package com.example.karlon.data.remote

import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

/** Builds a fresh Retrofit + OkHttpClient for a given base URL. Rebuilt
 * whenever the user changes server address in Settings, rather than a
 * single app-wide singleton, since the whole point of this app is that
 * the server address is configurable at runtime. */
object RetrofitProvider {

    fun buildApiService(baseUrl: String): ApiService {
        val client = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .build()

        return Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(ApiService::class.java)
    }

    fun buildOkHttpClient(): OkHttpClient =
        OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS) // WebSockets: no read timeout
            .pingInterval(20, TimeUnit.SECONDS)
            .build()
}
