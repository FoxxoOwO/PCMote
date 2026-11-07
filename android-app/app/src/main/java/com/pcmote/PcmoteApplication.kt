package com.pcmote

import android.app.Application

/**
 * Application class referenced by `android:name=".PcmoteApplication"` in the manifest.
 *
 * Currently a plain [Application] – nothing has to be initialised at startup, because the
 * WebSocket connection lives in [com.pcmote.data.PcConnection] and the media session is created
 * lazily by [com.pcmote.service.MediaProxyService].
 */
class PcmoteApplication : Application()
