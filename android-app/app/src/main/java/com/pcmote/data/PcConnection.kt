package com.pcmote.data

import android.util.Base64
import android.util.Log
import com.google.gson.Gson
import com.pcmote.model.CommandMessage
import com.pcmote.model.NowPlaying
import com.pcmote.model.PlaybackStatus
import com.pcmote.model.StateUpdateMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.math.pow

sealed interface ConnectionState {
    data object Disconnected : ConnectionState
    data class Connecting(val attempt: Int) : ConnectionState
    data class Connected(val host: String, val port: Int) : ConnectionState
    data class Failed(val message: String) : ConnectionState
}

/**
 * Single source of truth for the WebSocket link to the PC daemon.
 *
 * The [com.pcmote.service.MediaProxyService] renders [nowPlaying] into the media session,
 * the Compose UI observes the same flows and issues [CommandMessage]s.
 */
object PcConnection {

    private const val TAG = "PcConnection"
    const val DEFAULT_PORT = 8765

    private const val MIN_BACKOFF_MS = 1_000L
    private const val MAX_BACKOFF_MS = 30_000L

    private val gson = Gson()

    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private val _connectionState = MutableStateFlow<ConnectionState>(ConnectionState.Disconnected)
    val connectionState: StateFlow<ConnectionState> = _connectionState.asStateFlow()

    private val _nowPlaying = MutableStateFlow<NowPlaying?>(null)
    val nowPlaying: StateFlow<NowPlaying?> = _nowPlaying.asStateFlow()

    private val _lastError = MutableStateFlow<String?>(null)
    val lastError: StateFlow<String?> = _lastError.asStateFlow()

    private var socket: WebSocket? = null
    private var reconnectJob: Job? = null
    private var manualDisconnect = false
    private var attempt = 0

    private var targetHost: String? = null
    private var targetPort: Int = DEFAULT_PORT

    /** Cache of the last album art so repeated updates never re-download it. */
    private var cachedArtworkHash: String? = null
    private var cachedArtworkBytes: ByteArray? = null

    fun connect(host: String, port: Int = DEFAULT_PORT) {
        val cleanHost = host.trim().removePrefix("ws://").removeSuffix("/")
        if (cleanHost.isEmpty()) {
            _lastError.value = "Invalid IP address or hostname."
            return
        }
        val cleanPort = if (port in 1..65_535) port else DEFAULT_PORT

        manualDisconnect = false
        targetHost = cleanHost
        targetPort = cleanPort
        attempt = 0
        _lastError.value = null

        closeSocket()
        reconnectJob?.cancel()
        openSocket(cleanHost, cleanPort)
    }

    fun disconnect() {
        manualDisconnect = true
        reconnectJob?.cancel()
        reconnectJob = null
        closeSocket()
        _connectionState.value = ConnectionState.Disconnected
        _nowPlaying.value = null
    }

    fun send(command: CommandMessage): Boolean {
        val current = socket
        if (current == null) {
            _lastError.value = "You are not connected to the PC."
            return false
        }
        return try {
            val payload = gson.toJson(command)
            val queued = current.send(payload)
            if (!queued) {
                _lastError.value = "The message could not be sent (connection is closed)."
            }
            queued
        } catch (t: Throwable) {
            Log.e(TAG, "send() failed", t)
            _lastError.value = "Failed to send the command: ${t.message}"
            false
        }
    }

    private fun closeSocket() {
        val current = socket
        socket = null
        try {
            current?.close(1000, "client closing")
        } catch (t: Throwable) {
            Log.w(TAG, "close() failed", t)
        }
    }

    private fun openSocket(host: String, port: Int) {
        val url = "ws://$host:$port"
        _connectionState.value = ConnectionState.Connecting(attempt)

        val request = try {
            Request.Builder().url(url).build()
        } catch (t: Throwable) {
            Log.e(TAG, "Invalid WebSocket URL: $url", t)
            _connectionState.value = ConnectionState.Failed("Invalid address: $url")
            scheduleReconnect()
            return
        }

        socket = client.newWebSocket(request, object : WebSocketListener() {

            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "WebSocket open: $url")
                attempt = 0
                _connectionState.value = ConnectionState.Connected(host, port)
                _lastError.value = null
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleIncoming(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket closing: $code $reason")
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket closed: $code $reason")
                socket = null
                if (!manualDisconnect) {
                    _connectionState.value = ConnectionState.Failed("Connection was closed (code $code).")
                    scheduleReconnect()
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "WebSocket failure", t)
                socket = null
                if (manualDisconnect) return
                _connectionState.value = ConnectionState.Failed(
                    t.message?.takeIf { it.isNotBlank() } ?: "Network error"
                )
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (manualDisconnect) return
        val host = targetHost ?: return
        val port = targetPort

        reconnectJob?.cancel()
        val backoff = min(
            MAX_BACKOFF_MS,
            (MIN_BACKOFF_MS * 2.0.pow(attempt.toDouble())).toLong()
        )
        attempt = (attempt + 1).coerceAtMost(10)

        reconnectJob = scope.launch {
            delay(backoff)
            if (!manualDisconnect) {
                openSocket(host, port)
            }
        }
    }

    private fun handleIncoming(text: String) {
        try {
            val message = gson.fromJson(text, StateUpdateMessage::class.java) ?: return
            val artwork = resolveArtwork(message)
            _nowPlaying.value = NowPlaying(
                sourceApp = message.sourceApp.orEmpty(),
                status = PlaybackStatus.fromWire(message.state),
                title = message.title.orEmpty(),
                artist = message.artist.orEmpty(),
                album = message.album.orEmpty(),
                durationMs = message.durationMs.coerceAtLeast(0L),
                positionMs = message.positionMs.coerceAtLeast(0L),
                artworkHash = message.artworkHash,
                artwork = artwork,
                volumePercent = message.volumePercent?.coerceIn(0, 100),
                muted = message.muted == true
            )
        } catch (t: Throwable) {
            Log.w(TAG, "Cannot parse incoming frame: $text", t)
            _lastError.value = "Could not process the message from the PC."
        }
    }

    /**
     * The daemon only sends `artwork_base64` when the artwork hash changed, therefore the
     * previous bitmap has to be reused whenever the payload is absent but the hash matches.
     */
    private fun resolveArtwork(message: StateUpdateMessage): ByteArray? {
        val base64 = message.artworkBase64
        val hash = message.artworkHash

        if (!base64.isNullOrBlank()) {
            return try {
                val bytes = Base64.decode(base64, Base64.DEFAULT)
                cachedArtworkBytes = bytes
                cachedArtworkHash = hash
                bytes
            } catch (t: Throwable) {
                Log.w(TAG, "Artwork Base64 decoding failed", t)
                cachedArtworkBytes
            }
        }

        return when {
            hash == null -> null
            hash == cachedArtworkHash -> cachedArtworkBytes
            else -> null
        }
    }
}
