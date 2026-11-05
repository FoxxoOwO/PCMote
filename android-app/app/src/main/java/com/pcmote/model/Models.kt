package com.pcmote.model

import com.google.gson.annotations.SerializedName

/**
 * Wire model of the `state_update` event sent by the PC daemon.
 */
data class StateUpdateMessage(
    @SerializedName("event") val event: String? = null,
    @SerializedName("source_app") val sourceApp: String? = null,
    @SerializedName("state") val state: String? = null,
    @SerializedName("title") val title: String? = null,
    @SerializedName("artist") val artist: String? = null,
    @SerializedName("album") val album: String? = null,
    @SerializedName("duration_ms") val durationMs: Long = 0L,
    @SerializedName("position_ms") val positionMs: Long = 0L,
    @SerializedName("artwork_hash") val artworkHash: String? = null,
    @SerializedName("artwork_base64") val artworkBase64: String? = null,
    @SerializedName("volume_percent") val volumePercent: Int? = null,
    @SerializedName("muted") val muted: Boolean? = null
)

enum class PlaybackStatus {
    PLAYING,
    PAUSED,
    STOPPED;

    companion object {
        fun fromWire(raw: String?): PlaybackStatus = when (raw?.trim()?.uppercase()) {
            "PLAYING" -> PLAYING
            "PAUSED" -> PAUSED
            else -> STOPPED
        }
    }
}

/**
 * Fully resolved "now playing" snapshot used by both the proxy player and the UI.
 * [artwork] already holds the decoded album art bytes (or null when unknown).
 */
data class NowPlaying(
    val sourceApp: String = "",
    val status: PlaybackStatus = PlaybackStatus.STOPPED,
    val title: String = "",
    val artist: String = "",
    val album: String = "",
    val durationMs: Long = 0L,
    val positionMs: Long = 0L,
    val artworkHash: String? = null,
    val artwork: ByteArray? = null,
    /** Volume of the PC output in percent (0–100) or null when unknown. */
    val volumePercent: Int? = null,
    /** Whether the PC output is muted; null when the daemon cannot report it. */
    val muted: Boolean = false
) {
    val isPlaying: Boolean get() = status == PlaybackStatus.PLAYING
    val isIdle: Boolean get() = status == PlaybackStatus.STOPPED

    /** Volume usable by Media3 (0.0–1.0), falls back to full volume when unknown. */
    val volumeFraction: Float
        get() = (volumePercent ?: 100).coerceIn(0, 100) / 100f
}

/**
 * Wire model of the `command` message sent from Android to the PC daemon.
 */
data class CommandMessage(
    @SerializedName("command") val command: String,
    @SerializedName("position_ms") val positionMs: Long? = null,
    @SerializedName("volume") val volume: Int? = null
) {
    companion object {
        const val PLAY_PAUSE = "PLAY_PAUSE"
        const val NEXT = "NEXT"
        const val PREV = "PREV"
        const val SEEK = "SEEK"
        const val SET_VOLUME = "SET_VOLUME"
        const val VOLUME_UP = "VOLUME_UP"
        const val VOLUME_DOWN = "VOLUME_DOWN"

        fun playPause(): CommandMessage = CommandMessage(PLAY_PAUSE)
        fun next(): CommandMessage = CommandMessage(NEXT)
        fun previous(): CommandMessage = CommandMessage(PREV)
        fun seek(positionMs: Long): CommandMessage =
            CommandMessage(SEEK, positionMs.coerceAtLeast(0L))

        /** Sets the PC volume; [percent] is clamped into 0–100. */
        fun setVolume(percent: Int): CommandMessage =
            CommandMessage(SET_VOLUME, volume = percent.coerceIn(0, 100))

        fun volumeUp(): CommandMessage = CommandMessage(VOLUME_UP)
        fun volumeDown(): CommandMessage = CommandMessage(VOLUME_DOWN)
    }
}
