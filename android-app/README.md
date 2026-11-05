# PCMote – Android app

Controls media playback on a Windows PC from a phone on the local network. The app itself plays no
audio – it keeps a WebSocket connection to the PC daemon and acts as a system media player, so the
controls work from the notification, the lock screen, headphones and a Wear OS watch.

## Features

- Automatic discovery of the PC on the LAN over mDNS (`_pcmote._tcp.`) or manual entry of an IP
  address and port
- Automatic reconnect with exponential backoff (1 s → 30 s) when the connection drops
- `MediaSessionService` as a foreground service of type `mediaPlayback`
- A custom `SimpleBasePlayer` that translates commands into JSON messages for the PC
- PC volume control: a slider (`SET_VOLUME`) plus buttons in 5 % steps (`VOLUME_UP`/`VOLUME_DOWN`),
  mirrored into the media session (`handleSetVolume`, device volume)
- System media notification with album artwork, controls on the lock screen
- Material 3 Expressive (Material3 1.5.0-alpha08 + Compose 1.8.2)

## Structure

```
app/src/main/java/com/pcmote/
├── data/PcConnection.kt      # WebSocket client singleton + connection/metadata state (StateFlow)
├── model/Models.kt           # StateUpdateMessage, NowPlaying, CommandMessage
├── network/NsdDiscovery.kt   # NSD/mDNS discovery of the PC
├── service/MediaProxyService.kt  # MediaSessionService (foreground, mediaPlayback)
├── service/ProxyPlayer.kt    # SimpleBasePlayer – proxies commands to the PC
├── ui/MainActivity.kt        # Single activity, edge-to-edge, notification permission
├── ui/MainViewModel.kt       # UI state, discovery, persistence of the last host
├── ui/PcmoteScreen.kt        # Compose UI
└── ui/theme/Theme.kt         # MaterialExpressiveTheme
```

### How it works

1. `MainActivity` starts discovery in `onStart()` and stops it in `onStop()`.
2. `MainViewModel` creates `NsdDiscovery`; the discovered servers are offered in a list, otherwise
   an IP address and port can be entered manually (the last values are remembered in
   `SharedPreferences`).
3. `PcConnection` opens the WebSocket (OkHttp), receives the `state_update` messages and publishes
   `nowPlaying` / `connectionState` as `StateFlow`.
4. `MediaProxyService` collects `nowPlaying` and re-renders `ProxyPlayer.render(...)`, which calls
   `invalidateState()`. Media3 then updates the notification and the system session by itself.
5. A tap on play/pause/skip/seek in the notification, on the lock screen or in the UI calls
   `handleSetPlayWhenReady()` / `handleSeek()` in `ProxyPlayer`, which sends the JSON command to the
   PC. The real state comes back to the phone as another `state_update` – the PC is the source of
   truth.
6. The volume slider sends `SET_VOLUME`, the `−`/`+` buttons send `VOLUME_DOWN`/`VOLUME_UP`.
   `ProxyPlayer` also reports the PC volume as its own (`setVolume`, `setDeviceVolume`), so system
   media-session controllers see it too; conversely `handleSetVolume()` / `handleSetDeviceVolume()`
   change the PC volume.

## Protocol

WebSocket on port `8765`, JSON messages (see `pc-daemon/README.md`).

Incoming:

```json
{
  "event": "state_update",
  "source_app": "Spotify.exe",
  "state": "PLAYING",
  "title": "Track title",
  "artist": "Artist",
  "album": "Album title",
  "duration_ms": 235000,
  "position_ms": 42000,
  "artwork_hash": "sha256_hash",
  "artwork_base64": null,
  "volume_percent": 42,
  "muted": false
}
```

`artwork_base64` is `null` when the hash did not change since the last message – the app keeps the
artwork in a cache (`PcConnection`) and reuses it. `volume_percent` / `muted` may be `null` (older
daemon or no audio output) – the UI then disables the slider and shows `—`.

Outgoing:

```json
{ "command": "PLAY_PAUSE" }
{ "command": "NEXT" }
{ "command": "PREV" }
{ "command": "SEEK", "position_ms": 60000 }
{ "command": "SET_VOLUME", "volume": 42 }
{ "command": "VOLUME_UP" }
{ "command": "VOLUME_DOWN" }
```

## Building

```bash
cd android-app
./gradlew assembleDebug        # APK: app/build/outputs/apk/debug/app-debug.apk
./gradlew installDebug         # install onto a connected device
```

Gradle 8.14.3, AGP 8.13.2, Kotlin 2.1.0, `compileSdk`/`targetSdk` 35, `minSdk` 26 are used.

Gradle must run on **JDK 17-21**. Kotlin 2.1 cannot parse a newer major version and fails with
`IllegalArgumentException: 25.0.5`, so a machine whose default JDK is newer has to say which one to
use:

```bash
# for one build
JAVA_HOME=/path/to/jdk-17 ./gradlew assembleDebug
```

```properties
# or permanently, in %USERPROFILE%\.gradle\gradle.properties (or ~/.gradle/gradle.properties)
org.gradle.java.home=C\:\\Program Files\\Eclipse Adoptium\\jdk-17
```

Android Studio does the same in *Settings → Build, Execution, Deployment → Build Tools → Gradle →
Gradle JDK*. The path is deliberately **not** in the project's `gradle.properties` – it differs on
every machine.

## Running

1. On the PC, run `python main.py` inside `pc-daemon`.
2. The phone and the PC must be on the same network (without Wi-Fi client isolation).
3. Open the app, allow notifications and pick the discovered PC (or enter the IP and port).
4. A green dot and the text `Connected · <ip>:8765` confirm the connection.
5. Control playback in the app, in the notification or on the lock screen.

## Requirements

- Android 8.0 (API 26) or newer
- A Windows PC running the PCMote daemon on the same network
- Notification permission (Android 13+), network access; mDNS requires location on some devices,
  which is why `ACCESS_FINE_LOCATION` is declared in the manifest
