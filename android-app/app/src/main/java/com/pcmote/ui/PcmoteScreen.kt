package com.pcmote.ui

import android.graphics.BitmapFactory
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.pcmote.R
import com.pcmote.data.ConnectionState
import com.pcmote.model.NowPlaying
import com.pcmote.model.PlaybackStatus
import com.pcmote.network.NsdDiscovery
import kotlinx.coroutines.delay
import kotlin.math.roundToInt

@Composable
fun PcmoteScreen(
    connectionState: ConnectionState,
    nowPlaying: NowPlaying?,
    servers: List<NsdDiscovery.Server>,
    discoveryActive: Boolean,
    discoveryMessage: String?,
    lastError: String?,
    lastHost: String,
    lastPort: Int,
    onDiscover: () -> Unit,
    onConnect: (String, Int) -> Unit,
    onDisconnect: () -> Unit,
    onPlayPause: () -> Unit,
    onNext: () -> Unit,
    onPrevious: () -> Unit,
    onSeek: (Long) -> Unit,
    onVolumeChange: (Int) -> Unit,
    onVolumeUp: () -> Unit,
    onVolumeDown: () -> Unit,
    modifier: Modifier = Modifier
) {
    var showManualDialog by remember { mutableStateOf(false) }

    Scaffold(modifier = modifier.fillMaxSize()) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            Header()

            ConnectionCard(
                connectionState = connectionState,
                lastError = lastError,
                onConnectClick = { showManualDialog = true },
                onDisconnectClick = onDisconnect
            )

            NowPlayingCard(
                nowPlaying = nowPlaying,
                connected = connectionState is ConnectionState.Connected,
                onPlayPause = onPlayPause,
                onNext = onNext,
                onPrevious = onPrevious,
                onSeek = onSeek,
                onVolumeChange = onVolumeChange,
                onVolumeUp = onVolumeUp,
                onVolumeDown = onVolumeDown
            )

            DiscoveryCard(
                servers = servers,
                discoveryActive = discoveryActive,
                discoveryMessage = discoveryMessage,
                onDiscover = onDiscover,
                onSelect = { server -> onConnect(server.host, server.port) }
            )
        }
    }

    if (showManualDialog) {
        ManualConnectDialog(
            initialHost = lastHost,
            initialPort = lastPort,
            onDismiss = { showManualDialog = false },
            onConfirm = { host, port ->
                showManualDialog = false
                onConnect(host, port)
            }
        )
    }
}

@Composable
private fun Header() {
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(
            text = stringResource(R.string.app_name),
            style = MaterialTheme.typography.displaySmall,
            fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface
        )
        Text(
            text = stringResource(R.string.subtitle),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

@Composable
private fun ConnectionCard(
    connectionState: ConnectionState,
    lastError: String?,
    onConnectClick: () -> Unit,
    onDisconnectClick: () -> Unit
) {
    val connected = connectionState is ConnectionState.Connected
    val connecting = connectionState is ConnectionState.Connecting

    val statusText = when (connectionState) {
        is ConnectionState.Connected ->
            stringResource(R.string.status_connected) + " · ${connectionState.host}:${connectionState.port}"

        is ConnectionState.Connecting -> stringResource(R.string.status_connecting)
        is ConnectionState.Failed -> stringResource(R.string.status_error)
        ConnectionState.Disconnected -> stringResource(R.string.status_disconnected)
    }

    val statusColor = when (connectionState) {
        is ConnectionState.Connected -> Color(0xFF2E7D32)
        is ConnectionState.Connecting -> Color(0xFFF9A825)
        is ConnectionState.Failed -> MaterialTheme.colorScheme.error
        ConnectionState.Disconnected -> MaterialTheme.colorScheme.outline
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(28.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerHigh
        )
    ) {
        Column(
            modifier = Modifier.padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Box(
                    modifier = Modifier
                        .size(10.dp)
                        .clip(CircleShape)
                        .background(statusColor)
                )
                Text(
                    text = statusText,
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onSurface,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )
                if (connecting) {
                    Spacer(modifier = Modifier.width(4.dp))
                    CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                }
            }

            val failureText = when (connectionState) {
                is ConnectionState.Failed -> connectionState.message
                else -> null
            }
            val errorText = failureText ?: lastError
            if (errorText != null) {
                Text(
                    text = errorText,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error
                )
            }

            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                FilledTonalButton(onClick = onConnectClick) {
                    Icon(
                        painter = painterResource(R.drawable.ic_link),
                        contentDescription = null,
                        modifier = Modifier.size(18.dp)
                    )
                    Spacer(modifier = Modifier.width(8.dp))
                    Text(
                        stringResource(
                            if (connected) R.string.action_change_pc else R.string.action_connect
                        )
                    )
                }
                if (connected || connecting) {
                    TextButton(onClick = onDisconnectClick) {
                        Text(stringResource(R.string.action_disconnect))
                    }
                }
            }
        }
    }
}

@Composable
private fun NowPlayingCard(
    nowPlaying: NowPlaying?,
    connected: Boolean,
    onPlayPause: () -> Unit,
    onNext: () -> Unit,
    onPrevious: () -> Unit,
    onSeek: (Long) -> Unit,
    onVolumeChange: (Int) -> Unit,
    onVolumeUp: () -> Unit,
    onVolumeDown: () -> Unit
) {
    val hasMedia = nowPlaying != null && nowPlaying.status != PlaybackStatus.STOPPED
    val durationMs = nowPlaying?.durationMs?.coerceAtLeast(0L) ?: 0L

    var displayPosition by remember { mutableStateOf(0L) }
    LaunchedEffect(nowPlaying?.title, nowPlaying?.positionMs, nowPlaying?.isPlaying) {
        displayPosition = nowPlaying?.positionMs ?: 0L
        if (nowPlaying?.isPlaying == true && durationMs > 0L) {
            while (true) {
                delay(1_000L)
                displayPosition = (displayPosition + 1_000L).coerceAtMost(durationMs)
            }
        }
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(32.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainer
        )
    ) {
        Column(
            modifier = Modifier.padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            Artwork(
                artwork = nowPlaying?.artwork,
                hasMedia = hasMedia
            )

            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(
                    text = nowPlaying?.title?.takeIf { it.isNotBlank() }
                        ?: stringResource(R.string.now_playing_placeholder),
                    style = MaterialTheme.typography.headlineSmall,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    color = MaterialTheme.colorScheme.onSurface
                )
                Text(
                    text = nowPlaying?.artist?.takeIf { it.isNotBlank() }
                        ?: stringResource(R.string.unknown_artist),
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Text(
                    text = nowPlaying?.album?.takeIf { it.isNotBlank() }
                        ?: stringResource(R.string.unknown_album),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }

            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                if (durationMs > 0L) {
                    Slider(
                        value = displayPosition.coerceIn(0L, durationMs).toFloat(),
                        onValueChange = { value -> displayPosition = value.toLong() },
                        onValueChangeFinished = { onSeek(displayPosition) },
                        valueRange = 0f..durationMs.toFloat(),
                        enabled = hasMedia
                    )
                } else {
                    LinearProgressIndicator(
                        progress = { 0f },
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(4.dp)
                            .clip(RoundedCornerShape(2.dp))
                    )
                }
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween
                ) {
                    Text(
                        text = formatTime(displayPosition),
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Text(
                        text = formatTime(durationMs),
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.Center,
                verticalAlignment = Alignment.CenterVertically
            ) {
                FilledTonalIconButton(
                    onClick = onPrevious,
                    enabled = hasMedia,
                    modifier = Modifier.size(56.dp)
                ) {
                    Icon(
                        painter = painterResource(R.drawable.ic_prev),
                        contentDescription = stringResource(R.string.cd_previous)
                    )
                }
                Spacer(modifier = Modifier.width(20.dp))
                FilledTonalIconButton(
                    onClick = onPlayPause,
                    enabled = hasMedia || connected,
                    modifier = Modifier.size(76.dp)
                ) {
                    Icon(
                        painter = painterResource(
                            if (nowPlaying?.isPlaying == true) R.drawable.ic_pause else R.drawable.ic_play
                        ),
                        contentDescription = stringResource(
                            if (nowPlaying?.isPlaying == true) R.string.cd_pause else R.string.cd_play
                        ),
                        modifier = Modifier.size(34.dp)
                    )
                }
                Spacer(modifier = Modifier.width(20.dp))
                FilledTonalIconButton(
                    onClick = onNext,
                    enabled = hasMedia,
                    modifier = Modifier.size(56.dp)
                ) {
                    Icon(
                        painter = painterResource(R.drawable.ic_next),
                        contentDescription = stringResource(R.string.cd_next)
                    )
                }
            }

            VolumeRow(
                percent = nowPlaying?.volumePercent,
                muted = nowPlaying?.muted == true,
                enabled = connected,
                onChange = onVolumeChange,
                onStepUp = onVolumeUp,
                onStepDown = onVolumeDown
            )

            if (nowPlaying != null && nowPlaying.sourceApp.isNotBlank()) {
                Text(
                    text = stringResource(R.string.source_prefix) + ": " + nowPlaying.sourceApp,
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}

/**
 * Volume of the PC output. The slider sends `SET_VOLUME`, the two buttons send
 * `VOLUME_DOWN`/`VOLUME_UP` so both protocol paths are reachable from the UI.
 */
@Composable
private fun VolumeRow(
    percent: Int?,
    muted: Boolean,
    enabled: Boolean,
    onChange: (Int) -> Unit,
    onStepUp: () -> Unit,
    onStepDown: () -> Unit
) {
    val available = percent != null
    var dragging by remember { mutableStateOf(false) }
    var sliderValue by remember { mutableStateOf(0f) }

    // The PC is the source of truth: follow its value unless the user is dragging right now.
    LaunchedEffect(percent) {
        if (!dragging && percent != null) {
            sliderValue = percent.toFloat()
        }
    }

    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically
    ) {
        FilledTonalIconButton(
            onClick = onStepDown,
            enabled = enabled && available,
            modifier = Modifier.size(40.dp)
        ) {
            Icon(
                painter = painterResource(R.drawable.ic_volume_down),
                contentDescription = stringResource(R.string.cd_volume_down),
                modifier = Modifier.size(20.dp)
            )
        }
        Slider(
            value = sliderValue,
            onValueChange = { value ->
                dragging = true
                sliderValue = value
            },
            onValueChangeFinished = {
                dragging = false
                onChange(sliderValue.roundToInt())
            },
            valueRange = 0f..100f,
            enabled = enabled && available,
            modifier = Modifier
                .weight(1f)
                .padding(horizontal = 8.dp)
        )
        FilledTonalIconButton(
            onClick = onStepUp,
            enabled = enabled && available,
            modifier = Modifier.size(40.dp)
        ) {
            Icon(
                painter = painterResource(R.drawable.ic_volume_up),
                contentDescription = stringResource(R.string.cd_volume_up),
                modifier = Modifier.size(20.dp)
            )
        }
        Text(
            text = when {
                !available -> stringResource(R.string.volume_unknown)
                muted -> stringResource(R.string.volume_muted)
                else -> stringResource(R.string.volume_percent, percent ?: 0)
            },
            style = MaterialTheme.typography.labelMedium,
            textAlign = TextAlign.End,
            color = if (muted) {
                MaterialTheme.colorScheme.primary
            } else {
                MaterialTheme.colorScheme.onSurfaceVariant
            },
            maxLines = 1,
            modifier = Modifier.width(64.dp)
        )
    }
}

@Composable
private fun Artwork(artwork: ByteArray?, hasMedia: Boolean) {
    val bitmap = remember(artwork) {
        artwork?.let { bytes ->
            runCatching { BitmapFactory.decodeByteArray(bytes, 0, bytes.size) }.getOrNull()
        }
    }

    Surface(
        modifier = Modifier
            .fillMaxWidth()
            .aspectRatio(1f),
        shape = RoundedCornerShape(24.dp),
        color = MaterialTheme.colorScheme.surfaceContainerHighest
    ) {
        if (bitmap != null) {
            Image(
                bitmap = bitmap.asImageBitmap(),
                contentDescription = stringResource(R.string.cd_artwork),
                modifier = Modifier.fillMaxSize(),
                contentScale = ContentScale.Crop
            )
        } else {
            Box(contentAlignment = Alignment.Center, modifier = Modifier.fillMaxSize()) {
                Icon(
                    painter = painterResource(R.drawable.ic_music_note),
                    contentDescription = null,
                    modifier = Modifier.size(72.dp),
                    tint = if (hasMedia) {
                        MaterialTheme.colorScheme.onSurfaceVariant
                    } else {
                        MaterialTheme.colorScheme.outlineVariant
                    }
                )
            }
        }
    }
}

@Composable
private fun DiscoveryCard(
    servers: List<NsdDiscovery.Server>,
    discoveryActive: Boolean,
    discoveryMessage: String?,
    onDiscover: () -> Unit,
    onSelect: (NsdDiscovery.Server) -> Unit
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(28.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerHigh
        )
    ) {
        Column(
            modifier = Modifier.padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = stringResource(R.string.discovery_title),
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onSurface
                )
                IconButton(onClick = onDiscover, enabled = !discoveryActive) {
                    Icon(
                        painter = painterResource(R.drawable.ic_refresh),
                        contentDescription = stringResource(R.string.action_refresh)
                    )
                }
            }

            when {
                discoveryMessage != null -> Text(
                    text = discoveryMessage,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )

                servers.isEmpty() -> Text(
                    text = stringResource(
                        if (discoveryActive) R.string.discovery_scanning else R.string.discovery_empty
                    ),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )

                else -> servers.forEach { server ->
                    FilledTonalButton(
                        onClick = { onSelect(server) },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Column(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(vertical = 6.dp),
                            verticalArrangement = Arrangement.spacedBy(2.dp)
                        ) {
                            Text(text = server.name, style = MaterialTheme.typography.titleSmall)
                            Text(
                                text = "${server.host}:${server.port}",
                                style = MaterialTheme.typography.bodySmall
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun ManualConnectDialog(
    initialHost: String,
    initialPort: Int,
    onDismiss: () -> Unit,
    onConfirm: (String, Int) -> Unit
) {
    var host by remember { mutableStateOf(initialHost) }
    var port by remember { mutableStateOf(initialPort.toString()) }
    val parsedPort = port.toIntOrNull()
    val valid = host.isNotBlank() && parsedPort != null && parsedPort in 1..65_535

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.dialog_manual_title)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedTextField(
                    value = host,
                    onValueChange = { host = it },
                    label = { Text(stringResource(R.string.dialog_ip_label)) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                OutlinedTextField(
                    value = port,
                    onValueChange = { port = it.filter(Char::isDigit).take(5) },
                    label = { Text(stringResource(R.string.dialog_port_label)) },
                    singleLine = true,
                    isError = port.isNotEmpty() && parsedPort == null,
                    modifier = Modifier.fillMaxWidth()
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { if (parsedPort != null) onConfirm(host.trim(), parsedPort) },
                enabled = valid
            ) {
                Text(stringResource(R.string.action_connect))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.action_cancel))
            }
        }
    )
}

private fun formatTime(millis: Long): String {
    val totalSeconds = (millis.coerceAtLeast(0L) / 1000L)
    val minutes = totalSeconds / 60
    val seconds = totalSeconds % 60
    return "%d:%02d".format(minutes, seconds)
}
