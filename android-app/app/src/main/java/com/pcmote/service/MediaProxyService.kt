package com.pcmote.service

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.os.Looper
import android.util.Log
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.DefaultMediaNotificationProvider
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import com.pcmote.R
import com.pcmote.data.PcConnection
import com.pcmote.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

/**
 * Foreground media service that owns the [MediaSession] and the [ProxyPlayer].
 *
 * It renders every `state_update` received over the WebSocket into the session, so Android shows
 * the media notification, the lock screen controls and Wear OS controls without any extra work.
 */
@androidx.annotation.OptIn(UnstableApi::class)
class MediaProxyService : MediaSessionService() {

    private companion object {
        const val TAG = "MediaProxyService"
        const val CHANNEL_ID = "pcmote_playback"
        const val NOTIFICATION_ID = 1001
    }

    private var session: MediaSession? = null

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "onCreate")

        val proxyPlayer = ProxyPlayer(Looper.getMainLooper()) { command ->
            PcConnection.send(command)
        }

        // Create the channel ourselves first: Android never renames a channel that already exists and
        // media3 would create it with whatever name the previous install used.
        try {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply { setShowBadge(false) }
            getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)
        } catch (t: Throwable) {
            Log.e(TAG, "Cannot create the notification channel", t)
        }

        // Own channel name and stable notification id, otherwise media3 labels the channel
        // "default_channel_id" in the system settings.
        try {
            setMediaNotificationProvider(
                DefaultMediaNotificationProvider.Builder(this)
                    .setChannelId(CHANNEL_ID)
                    .setChannelName(R.string.notification_channel_name)
                    .setNotificationId(NOTIFICATION_ID)
                    .build()
            )
        } catch (t: Throwable) {
            Log.e(TAG, "Cannot configure the media notification provider", t)
        }

        val sessionActivity = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            },
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        session = try {
            MediaSession.Builder(this, proxyPlayer)
                .setSessionActivity(sessionActivity)
                .build()
        } catch (t: Throwable) {
            Log.e(TAG, "Cannot build MediaSession", t)
            null
        }

        // Without this the service never treats the session as its own: no media notification,
        // no foreground service and no lock screen controls would appear.
        session?.let { activeSession ->
            try {
                addSession(activeSession)
                Log.i(
                    TAG,
                    "Session registered: timelineEmpty=${
                        activeSession.player.currentTimeline.isEmpty
                    } playbackState=${activeSession.player.playbackState}"
                )
            } catch (t: Throwable) {
                Log.e(TAG, "Cannot register session with the service", t)
            }
        }

        scope.launch {
            PcConnection.nowPlaying.collectLatest { nowPlaying ->
                proxyPlayer.render(nowPlaying)
            }
        }
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onUpdateNotification(session: MediaSession, startInForegroundRequired: Boolean) {
        Log.d(
            TAG,
            "onUpdateNotification startInForeground=$startInForegroundRequired " +
                "isPlaybackOngoing=${isPlaybackOngoing()} " +
                "timelineEmpty=${session.player.currentTimeline.isEmpty} " +
                "playbackState=${session.player.playbackState} " +
                "playWhenReady=${session.player.playWhenReady}"
        )
        try {
            super.onUpdateNotification(session, startInForegroundRequired)
        } catch (t: Throwable) {
            Log.e(TAG, "Cannot update the media notification", t)
        }
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        Log.i(TAG, "onTaskRemoved")
        // Keep the session (and the WebSocket) alive so the notification keeps working.
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        Log.i(TAG, "onDestroy")
        scope.cancel()
        try {
            session?.let { activeSession ->
                activeSession.player.release()
                activeSession.release()
            }
        } catch (t: Throwable) {
            Log.w(TAG, "Error while releasing session", t)
        }
        session = null
        super.onDestroy()
    }
}
