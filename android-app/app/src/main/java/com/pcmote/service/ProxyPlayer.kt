package com.pcmote.service

import android.os.Looper
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.common.SimpleBasePlayer
import androidx.media3.common.util.UnstableApi
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import com.pcmote.model.CommandMessage
import com.pcmote.model.NowPlaying
import com.pcmote.model.PlaybackStatus
import kotlin.math.roundToInt

/**
 * A [Player] that produces no audio at all: it mirrors the state reported by the PC daemon and
 * forwards every user intent (play/pause, next, previous, seek) back over the WebSocket.
 *
 * Because it is a real [Player] backed by a `MediaSessionService`, Android renders the system
 * media notification, the lock screen controls and Wear OS controls automatically.
 */
@androidx.annotation.OptIn(UnstableApi::class)
class ProxyPlayer(
    looper: Looper,
    private val sendCommand: (CommandMessage) -> Unit
) : SimpleBasePlayer(looper) {

    private companion object {
        const val MEDIA_ID = "pcmote:remote"
        const val SEEK_STEP_MS = 10_000L

        val AVAILABLE_COMMANDS: Player.Commands = Player.Commands.Builder()
            .addAll(
                Player.COMMAND_PLAY_PAUSE,
                Player.COMMAND_PREPARE,
                Player.COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM,
                Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM,
                Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM,
                Player.COMMAND_SEEK_TO_NEXT,
                Player.COMMAND_SEEK_TO_PREVIOUS,
                Player.COMMAND_SEEK_TO_DEFAULT_POSITION,
                Player.COMMAND_GET_CURRENT_MEDIA_ITEM,
                Player.COMMAND_GET_TIMELINE,
                Player.COMMAND_GET_METADATA,
                Player.COMMAND_GET_AUDIO_ATTRIBUTES,
                Player.COMMAND_GET_VOLUME,
                Player.COMMAND_SET_VOLUME,
                Player.COMMAND_GET_DEVICE_VOLUME,
                Player.COMMAND_SET_DEVICE_VOLUME,
                Player.COMMAND_ADJUST_DEVICE_VOLUME,
                Player.COMMAND_SET_DEVICE_VOLUME_WITH_FLAGS,
                Player.COMMAND_ADJUST_DEVICE_VOLUME_WITH_FLAGS,
                Player.COMMAND_SET_SPEED_AND_PITCH,
                Player.COMMAND_RELEASE
            )
            .build()
    }

    @Volatile
    private var snapshot: NowPlaying? = null

    /**
     * Last non-zero PC volume. Android may ask us to unmute, but the protocol has no mute command,
     * so the previous audible level is restored by sending `SET_VOLUME` again.
     */
    private var lastAudibleVolumePercent: Int = 100

    /** Called from the main thread whenever a fresh `state_update` frame has been decoded. */
    fun render(nowPlaying: NowPlaying?) {
        snapshot = nowPlaying
        invalidateState()
    }

    override fun getState(): State {
        val now = snapshot
        val hasItem = now != null && now.status != PlaybackStatus.STOPPED

        val metadata = MediaMetadata.Builder()
            .setTitle(now?.title?.takeIf { it.isNotBlank() } ?: "PCMote")
            .setArtist(now?.artist?.takeIf { it.isNotBlank() } ?: now?.sourceApp?.takeIf { it.isNotBlank() })
            .setAlbumTitle(now?.album?.takeIf { it.isNotBlank() })
            .setArtworkData(now?.artwork, MediaMetadata.PICTURE_TYPE_FRONT_COVER)
            .setIsPlayable(true)
            .setIsBrowsable(false)
            .build()

        val mediaItem = MediaItem.Builder()
            .setMediaId(MEDIA_ID)
            .setMediaMetadata(metadata)
            .build()

        val durationUs = (now?.durationMs ?: 0L) * 1_000L
        val itemData = MediaItemData.Builder(MEDIA_ID)
            .setMediaItem(mediaItem)
            .setMediaMetadata(metadata)
            .setDurationUs(if (durationUs > 0L) durationUs else C.TIME_UNSET)
            .setIsSeekable(durationUs > 0L)
            .setIsDynamic(false)
            .build()

        val positionMs = (now?.positionMs ?: 0L).coerceAtLeast(0L)
        val volumePercent = now?.volumePercent
        if (volumePercent != null && volumePercent > 0) {
            lastAudibleVolumePercent = volumePercent
        }

        val builder = State.Builder()
            .setAvailableCommands(AVAILABLE_COMMANDS)
            .setPlaylist(listOf(itemData))
            .setCurrentMediaItemIndex(0)
            .setPlaybackState(if (hasItem) Player.STATE_READY else Player.STATE_IDLE)
            .setPlayWhenReady(now?.isPlaying == true, Player.PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST)
            .setContentPositionMs(positionMs)
            .setIsLoading(false)
            .setSeekBackIncrementMs(SEEK_STEP_MS)
            .setSeekForwardIncrementMs(SEEK_STEP_MS)
            .setVolume(now?.volumeFraction ?: 1f)

        if (volumePercent != null) {
            // "Device volume" is the closest Media3 concept to the remote PC output level, so the
            // system knows the real value instead of a hardcoded maximum.
            builder
                .setDeviceVolume(volumePercent)
                .setIsDeviceMuted(now.muted)
        }

        return builder.build()
    }

    override fun handleSetVolume(volume: Float): ListenableFuture<*> {
        sendCommand(CommandMessage.setVolume((volume.coerceIn(0f, 1f) * 100f).roundToInt()))
        return Futures.immediateVoidFuture()
    }

    override fun handleSetDeviceVolume(volume: Int, flags: Int): ListenableFuture<*> {
        val percent = volume.coerceIn(0, 100)
        if (percent > 0) {
            lastAudibleVolumePercent = percent
        }
        sendCommand(CommandMessage.setVolume(percent))
        return Futures.immediateVoidFuture()
    }

    override fun handleIncreaseDeviceVolume(flags: Int): ListenableFuture<*> {
        sendCommand(CommandMessage.volumeUp())
        return Futures.immediateVoidFuture()
    }

    override fun handleDecreaseDeviceVolume(flags: Int): ListenableFuture<*> {
        sendCommand(CommandMessage.volumeDown())
        return Futures.immediateVoidFuture()
    }

    override fun handleSetDeviceMuted(muted: Boolean, flags: Int): ListenableFuture<*> {
        // The protocol only knows SET_VOLUME, therefore muting is emulated with volume 0.
        sendCommand(CommandMessage.setVolume(if (muted) 0 else lastAudibleVolumePercent))
        return Futures.immediateVoidFuture()
    }

    override fun handleSetPlayWhenReady(playWhenReady: Boolean): ListenableFuture<*> {
        sendCommand(CommandMessage.playPause())
        return Futures.immediateVoidFuture()
    }

    override fun handleSeek(
        mediaItemIndex: Int,
        positionMs: Long,
        command: Int
    ): ListenableFuture<*> {
        when (command) {
            Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM, Player.COMMAND_SEEK_TO_NEXT ->
                sendCommand(CommandMessage.next())

            Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM, Player.COMMAND_SEEK_TO_PREVIOUS ->
                sendCommand(CommandMessage.previous())

            else -> if (positionMs != C.TIME_UNSET) {
                sendCommand(CommandMessage.seek(positionMs))
            }
        }
        return Futures.immediateVoidFuture()
    }

    override fun handlePrepare(): ListenableFuture<*> = Futures.immediateVoidFuture()

    override fun handleRelease(): ListenableFuture<*> {
        snapshot = null
        return Futures.immediateVoidFuture()
    }
}
