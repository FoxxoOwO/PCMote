package com.pcmote.ui

import android.app.Application
import android.content.Context
import android.util.Log
import androidx.core.content.edit
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.pcmote.R
import com.pcmote.data.ConnectionState
import com.pcmote.data.PcConnection
import com.pcmote.model.CommandMessage
import com.pcmote.model.NowPlaying
import com.pcmote.network.NsdDiscovery
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * UI-facing state holder. All networking lives in [PcConnection] so that the media session
 * (which runs independently of the Activity) keeps working when the UI goes away.
 */
class MainViewModel(application: Application) : AndroidViewModel(application) {

    private companion object {
        const val TAG = "MainViewModel"
        const val PREFS = "pcmote_prefs"
        const val KEY_HOST = "last_host"
        const val KEY_PORT = "last_port"

        /** How long a single discovery run listens before it gives up and tells the user. */
        const val DISCOVERY_WINDOW_MS = 8_000L
    }

    private var discoveryWindow: Job? = null

    private val prefs = application.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private val discovery = NsdDiscovery(application)

    val connectionState: StateFlow<ConnectionState> = PcConnection.connectionState
    val nowPlaying: StateFlow<NowPlaying?> = PcConnection.nowPlaying
    val lastError: StateFlow<String?> = PcConnection.lastError

    private val _servers = MutableStateFlow<List<NsdDiscovery.Server>>(emptyList())
    val servers: StateFlow<List<NsdDiscovery.Server>> = _servers.asStateFlow()

    private val _discoveryActive = MutableStateFlow(false)
    val discoveryActive: StateFlow<Boolean> = _discoveryActive.asStateFlow()

    private val _discoveryMessage = MutableStateFlow<String?>(null)
    val discoveryMessage: StateFlow<String?> = _discoveryMessage.asStateFlow()

    var lastHost: String = prefs.getString(KEY_HOST, "").orEmpty()
        private set

    var lastPort: Int = prefs.getInt(KEY_PORT, PcConnection.DEFAULT_PORT)
        private set

    fun startDiscovery() {
        if (_discoveryActive.value) return
        _discoveryActive.value = true
        _discoveryMessage.value = null
        _servers.value = emptyList()

        discovery.start(
            onFound = { server ->
                _servers.value = (_servers.value.filterNot { it.id == server.id } + server)
                    .sortedBy { it.name.lowercase() }
                cancelDiscoveryWindow()
            },
            onLost = { name ->
                _servers.value = _servers.value.filterNot { it.name == name }
            },
            onError = { message ->
                Log.w(TAG, "Discovery error: $message")
                _discoveryMessage.value = message
                _discoveryActive.value = false
            }
        )

        // Discovery alone never finishes on its own – stop it after a while so the radio can idle
        // and the user gets an actionable hint instead of a spinner that runs forever.
        discoveryWindow?.cancel()
        discoveryWindow = viewModelScope.launch {
            delay(DISCOVERY_WINDOW_MS)
            if (_discoveryActive.value && _servers.value.isEmpty()) {
                discovery.stop()
                _discoveryActive.value = false
                _discoveryMessage.value = getApplication<Application>()
                    .getString(R.string.discovery_not_found)
            }
        }
    }

    private fun cancelDiscoveryWindow() {
        discoveryWindow?.cancel()
        discoveryWindow = null
    }

    fun stopDiscovery() {
        cancelDiscoveryWindow()
        discovery.stop()
        _discoveryActive.value = false
    }

    fun connect(host: String, port: Int) {
        val normalizedPort = if (port in 1..65_535) port else PcConnection.DEFAULT_PORT
        lastHost = host.trim()
        lastPort = normalizedPort
        prefs.edit {
            putString(KEY_HOST, lastHost)
            putInt(KEY_PORT, normalizedPort)
        }
        PcConnection.connect(lastHost, normalizedPort)
    }

    fun disconnect() {
        PcConnection.disconnect()
    }

    fun togglePlayPause() {
        PcConnection.send(CommandMessage.playPause())
    }

    fun skipNext() {
        PcConnection.send(CommandMessage.next())
    }

    fun skipPrevious() {
        PcConnection.send(CommandMessage.previous())
    }

    fun seekTo(positionMs: Long) {
        PcConnection.send(CommandMessage.seek(positionMs))
    }

    fun setVolume(percent: Int) {
        PcConnection.send(CommandMessage.setVolume(percent))
    }

    fun volumeUp() {
        PcConnection.send(CommandMessage.volumeUp())
    }

    fun volumeDown() {
        PcConnection.send(CommandMessage.volumeDown())
    }

    fun clearDiscoveryMessage() {
        _discoveryMessage.value = null
    }

    override fun onCleared() {
        super.onCleared()
        cancelDiscoveryWindow()
        discovery.stop()
    }
}
