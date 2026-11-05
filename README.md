# PCMote

Control the media player on a Windows PC from an Android phone on the local network.
The repository contains two cooperating parts:

| Folder | What it is |
|---|---|
| [`pc-daemon/`](pc-daemon/README.md) | Python daemon for Windows – reads the playback state through WinRT, runs in the notification area, exposes a WebSocket and registers itself over mDNS |
| [`android-app/`](android-app/README.md) | Native Android app (Kotlin, Compose, AndroidX Media3) – proxy player, system media notification |

## How it works

```
  Windows PC                                   Android phone
┌──────────────────────────┐                ┌────────────────────────────┐
│ player (Spotify, …)      │                │  MediaSessionService       │
│        │ SMTC            │                │        │                   │
│ ┌──────▼───────────────┐ │   WebSocket    │ ┌──────▼───────────────┐   │
│ │ media_controller.py  │ │   port 8765    │ │ ProxyPlayer          │   │
│ │ (WinRT)              │ │◄──────────────►│ │ (SimpleBasePlayer)   │   │
│ └──────┬───────────────┘ │  state_update  │ └──────┬───────────────┘   │
│ ┌──────▼───────────────┐ │   / command    │ ┌──────▼───────────────┐   │
│ │ main.py              │ │                │ │ PcConnection (OkHttp)│   │
│ │ server + mDNS + tray │ │                │ │ + NsdDiscovery       │   │
│ └──────────────────────┘ │                │ └──────────────────────┘   │
└──────────────────────────┘                └────────────────────────────┘
             ▲                                            │
             └───────── mDNS `_pcmote._tcp.local.` ────────┘
```

The phone plays no audio at all. It consumes the `state_update` messages from the PC and mirrors
them into its own media session, so Android shows the notification, the lock-screen controls and
the controls on a smartwatch. A tap on play/pause/skip/seek is sent back to the PC as a `command`;
the PC is always the source of truth.

## Quick start

1. **PC** (Windows 10/11, Python 3.11+):

   ```bat
   cd pc-daemon
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   python main.py
   ```

   Without Python installed, build the standalone executable instead:

   ```bat
   pip install -r requirements-build.txt
   python build_exe.py --clean
   dist\PCMote.exe
   ```

   To start it automatically at login, right-click the tray icon → *Start with Windows*, or run
   `PCMote.exe --install-autostart` (`--remove-autostart` undoes it).

2. **Phone** (Android 8.0+): install the APK and open the app.

   ```bash
   cd android-app
   ./gradlew assembleDebug
   adb install -r app/build/outputs/apk/debug/app-debug.apk
   ```

3. In the app, allow notifications, pick the discovered PC (or enter the IP address and port
   `8765`) and start controlling playback.

Details, the protocol and the limitations are in the README of each part.

## Protocol at a glance

```json
{ "event": "state_update", "source_app": "Spotify.exe", "state": "PLAYING",
  "title": "…", "artist": "…", "album": "…", "duration_ms": 235000, "position_ms": 42000,
  "artwork_hash": "…", "artwork_base64": null, "volume_percent": 42, "muted": false }
```

```json
{ "command": "PLAY_PAUSE" }
{ "command": "SEEK", "position_ms": 60000 }
{ "command": "SET_VOLUME", "volume": 42 }
{ "command": "VOLUME_UP" }
{ "command": "VOLUME_DOWN" }
```

`state` is `PLAYING` / `PAUSED` / `STOPPED`; the commands are `PLAY_PAUSE`, `NEXT`, `PREV`, `SEEK`,
`SET_VOLUME` (0–100), `VOLUME_UP` and `VOLUME_DOWN` (5 % step).
`artwork_base64` is `null` whenever `artwork_hash` did not change – the artwork is transferred only
once. `volume_percent` / `muted` are `null` when the daemon cannot read the volume (`pycaw`
missing).

## Requirements for building the Android part

Gradle 8.14.3, AGP 8.13.2, Kotlin 2.1.0, JDK 17 (the path is set in
`android-app/gradle.properties`), `compileSdk`/`targetSdk` 35, `minSdk` 26. The Android SDK has to
be installed (`ANDROID_HOME`).
