package com.pcmote.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.pcmote.service.MediaProxyService
import com.pcmote.ui.theme.PcmoteTheme

class MainActivity : ComponentActivity() {

    private val viewModel: MainViewModel by viewModels()

    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* result unused */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        startMediaSessionService()
        requestNotificationPermissionIfNeeded()

        setContent {
            PcmoteTheme {
                val connectionState by viewModel.connectionState.collectAsStateWithLifecycle()
                val nowPlaying by viewModel.nowPlaying.collectAsStateWithLifecycle()
                val servers by viewModel.servers.collectAsStateWithLifecycle()
                val discoveryActive by viewModel.discoveryActive.collectAsStateWithLifecycle()
                val discoveryMessage by viewModel.discoveryMessage.collectAsStateWithLifecycle()
                val lastError by viewModel.lastError.collectAsStateWithLifecycle()

                PcmoteScreen(
                    connectionState = connectionState,
                    nowPlaying = nowPlaying,
                    servers = servers,
                    discoveryActive = discoveryActive,
                    discoveryMessage = discoveryMessage,
                    lastError = lastError,
                    lastHost = viewModel.lastHost,
                    lastPort = viewModel.lastPort,
                    onDiscover = viewModel::startDiscovery,
                    onConnect = viewModel::connect,
                    onDisconnect = viewModel::disconnect,
                    onPlayPause = viewModel::togglePlayPause,
                    onNext = viewModel::skipNext,
                    onPrevious = viewModel::skipPrevious,
                    onSeek = viewModel::seekTo,
                    onVolumeChange = viewModel::setVolume,
                    onVolumeUp = viewModel::volumeUp,
                    onVolumeDown = viewModel::volumeDown
                )
            }
        }
    }

    override fun onStart() {
        super.onStart()
        viewModel.startDiscovery()
    }

    override fun onStop() {
        super.onStop()
        viewModel.stopDiscovery()
    }

    private fun startMediaSessionService() {
        try {
            startService(Intent(this, MediaProxyService::class.java))
        } catch (t: Throwable) {
            // Starting the service is best effort; the UI still works without the media session.
            android.util.Log.w("MainActivity", "Cannot start MediaProxyService", t)
        }
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
}
