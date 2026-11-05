# PCMote – Windows Media Daemon

A daemon for Windows that reads the playback state from
**GlobalSystemMediaTransportControlsSessionManager** (WinRT) and offers it to the phone over a
WebSocket. It runs without a console window as an icon in the notification area and registers
itself over mDNS on the LAN, so the Android app finds it automatically.

## Requirements

- Windows 10 (1809+) / Windows 11
- Python 3.11+ (tested on 3.15)
- A LAN connection, free TCP port `8765`

## Installation

```bat
cd pc-daemon
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` contains `winrt-runtime` and the modular packages
`winrt-Windows.Media.Control`, `winrt-Windows.Storage.Streams`, `winrt-Windows.Foundation` and
`winrt-Windows.Foundation.Collections`. The monolithic `winsdk` package no longer exists on PyPI
for newer Python versions, which is why it is not used.

## Running

```bat
python main.py
```

After start-up an icon appears in the notification area. Its context menu contains:

- **Show IP and status** – a dialog with the local IP addresses, the port, whether autostart is on,
  the connected devices, the currently playing track and the volume,
- **Start with Windows** – a checkbox that turns autostart on or off,
- **Quit** – unregisters the mDNS service cleanly and terminates the daemon.

The tray menu and the status dialog are in English; the log and the console output are in Czech.

### Command-line options

| Option | Meaning |
|---|---|
| `--host ADDRESS` | Bind address of the WebSocket server (default `0.0.0.0`) |
| `--port PORT` | Port of the WebSocket server (default `8765`) |
| `--no-tray` | Run without the notification-area icon (handy for debugging) |
| `--keep-console` | Keeps the console open even without `--debug` |
| `--debug` | Verbose logging to the console and to the file |
| `--install-autostart` | Registers PCMote in `HKCU\...\Run` and exits |
| `--remove-autostart` | Removes that registration and exits |
| `--autostart-status` | Prints whether autostart is on, and the stored command, then exits |
| `--autostart` | Internal flag the autostart entry uses; waits 3 s before start-up |

The log is written to `%LOCALAPPDATA%\PCMote\daemon.log` (rotation 1 MB, 3 files).

## Starting with Windows

Autostart is **opt-in** and can be switched three ways, all writing the same value:

1. **Tray menu** – right-click the icon → *Start with Windows* (the checkbox reflects the real
   registry state).
2. **Command line** – `python main.py --install-autostart` (or `--remove-autostart`,
   `--autostart-status`). The port and host currently used are stored with the command, so a daemon
   listening on a non-default port also starts on that port.
3. **Task Manager** – the entry appears under *Startup apps* as `PCMote` and can be disabled there
   like any other program.

Implementation: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value `PCMote` (see
`autostart.py`). HKCU needs no administrator rights, unlike a scheduled task. When the stored
command has to be built for the source tree, `pythonw.exe` is used so no console window flashes at
login; from a frozen EXE the path to the EXE is used. Windows starts the entry with `--autostart`,
which makes the daemon wait 3 s before it touches the network and the audio endpoint (both may
still be initialising right after login).

## Building a standalone EXE

`PCMote.exe` does not need Python or any installed package on the target PC.

```bat
cd pc-daemon
pip install -r requirements-build.txt
python build_exe.py --clean
```

The result is `dist\PCMote.exe` (~22 MB). It is a windowless build (`--noconsole`), so all
messages go to the log; when the EXE is started from a terminal or a script, errors are also
printed there, and when it is started by double-click an error dialog is shown instead.

| Command | Result |
|---|---|
| `python build_exe.py` | single file `dist\PCMote.exe` |
| `python build_exe.py --onedir` | folder `dist\PCMote\` – starts faster, no unpacking to `%TEMP%` |
| `python build_exe.py --clean` | removes `build/` and `dist/` first |
| `python build_exe.py --no-icon` | skips the generated `.ico` |

Notes on the build:

- The icon is drawn in code (`icon.py`) and `build_exe.py` writes a multi-size `build\pcmote.ico`
  for it, so the repository needs no binary assets.
- WinRT projections are collected by the hook that `winrt-runtime` ships; `build_exe.py`
  additionally declares the runtime-imported modules (`pystray._win32`, `comtypes`, `pycaw`, the
  `winrt.windows.*` namespaces).
- `--clean` is worth running after changing dependencies, otherwise PyInstaller may reuse stale
  analysis.
- Build on Windows only – PyInstaller cannot cross-compile, and WinRT plus `pystray` are
  Windows-specific.
- Autostart works from the EXE too: `dist\PCMote.exe --install-autostart`. Keep the EXE in a
  stable folder – if you move or delete it, the startup entry points nowhere and should be
  re-created (`--install-autostart` rewrites it) or removed (`--remove-autostart`).

## Communication protocol

WebSocket on port `8765`, messages are JSON. The mDNS service is `_pcmote._tcp.local.`
(instance `PCMote._pcmote._tcp.local.`, TXT records `version=1`, `platform=windows`).

### PC → Android: `state_update`

Sent immediately after a client connects and then on every change of metadata, state or position.

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
  "artwork_hash": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "artwork_base64": "iVBORw0KGgoAAAANSUhEUg...",
  "volume_percent": 42,
  "muted": false
}
```

- `state` is `PLAYING`, `PAUSED` or `STOPPED`.
- `volume_percent` is the volume of the default Windows audio output in percent (0–100) and
  `muted` tells whether the output is muted. When the volume cannot be determined (`pycaw`
  missing, no audio device), `null` is sent.
- `artwork_base64` is `null` when `artwork_hash` did not change since the previous message for that
  client. The client is expected to remember the artwork and reuse it. This way the image (with
  Windows Media Player usually ~1.4 MB) is transferred only once per track.

### Android → PC: `command`

```json
{ "command": "PLAY_PAUSE" }
{ "command": "NEXT" }
{ "command": "PREV" }
{ "command": "SEEK", "position_ms": 60000 }
{ "command": "SET_VOLUME", "volume": 42 }
{ "command": "VOLUME_UP" }
{ "command": "VOLUME_DOWN" }
```

- `volume` for `SET_VOLUME` is 0–100; the value is clamped to that range. Setting a non-zero
  volume also unmutes the output.
- `VOLUME_UP` / `VOLUME_DOWN` change the volume in 5 % steps.

No reply is sent – the confirmation is the new `state_update` message triggered by the player
itself (for volume the daemon broadcasts it immediately after a successful change).
When no media session exists at the moment, the command is ignored and
`<COMMAND> ignorováno: žádná aktivní relace` is written to the log; for volume it is ignored only
when no audio output is available.

## Architecture

| File | Contents |
|---|---|
| `main.py` | WebSocket server, mDNS registration, tray icon, broadcasting state to clients, CLI |
| `media_controller.py` | WinRT layer – session management, reading metadata/timeline/artwork, sending commands |
| `volume_controller.py` | Volume of the default output through WASAPI (`pycaw`), both read and write |
| `autostart.py` | Registration in `HKCU\...\Run`, detection of the current EXE vs. source tree |
| `icon.py` | Tray icon and `.ico` drawn in code, no image files in the repository |
| `build_exe.py` | PyInstaller wrapper producing `dist\PCMote.exe` |
| `requirements.txt` | Runtime dependencies |
| `requirements-build.txt` | Build-only dependency (PyInstaller) |

Key implementation details:

- **Threads.** WinRT events (`MediaPropertiesChanged`, `PlaybackInfoChanged`,
  `TimelinePropertiesChanged`, `CurrentSessionChanged`) arrive on a different thread than the
  asyncio loop. The callback therefore only forwards them through `loop.call_soon_threadsafe(...)`
  and coalesces them into a single scheduled task, so a storm of events does not flood the clients.
- **Artwork.** `thumbnail.open_read_async()` → `DataReader.load_async()` → `read_buffer()`.
  A SHA-256 of the bytes serves as the cache key; Base64 is sent only when the hash changes, and
  per client. Windows often makes the thumbnail available later than the metadata, so reading is
  retried (1 s, at most 5 attempts) and the cache is cleared only when the track title or the
  session changes.
- **Volume.** Windows Media Transport Controls do not expose the volume, so WASAPI is used
  (`pycaw` → `IAudioEndpointVolume`). The daemon keeps the COM object on a single thread (the
  event loop) and compares the current value every 2 s, so changes made from the keyboard or the
  mixer reach the phone. If `pycaw` is missing or there is no audio device, `volume_percent: null`
  is sent and the daemon keeps working.
- **Status dialog.** `pystray` invokes the menu callback inside the window procedure of the tray
  thread, and Windows does not allow a background process to steal the foreground – the dialog
  used to open *behind* the window the user had in front, so it could not be clicked. The dialog
  therefore runs in its own thread and opens with `MB_TOPMOST | MB_SETFOREGROUND`; a helper thread
  additionally pulls it on top and tries to activate it. A second click on the menu item only
  brings an already open dialog to the front.
- **mDNS in asyncio.** Registration uses `zeroconf.asyncio.AsyncZeroconf`; the synchronous
  `Zeroconf.register_service()` inside a running loop raises `EventLoopBlocked`.
- **Position.** `position_ms` is computed from `TimelineProperties.position` + the time since
  `last_updated_time` while playing, and clamped to `0..duration_ms`.
- **Shutdown.** Both `Ctrl+C` and the *Quit* entry take the same path: unregister mDNS, close the
  WebSocket server, stop the tray icon.
- **Autostart.** A single registry value under `HKCU`, toggled from the tray menu (the checkbox
  reads the live state) or from the command line. The stored command contains `--autostart` and the
  non-default port, so the entry keeps working with a custom port and never opens a console window
  at login.

## Limitations

- With no active media session (no player running) the daemon sends `state: "STOPPED"` with empty
  metadata and ignores commands.
- Artwork is not sent downscaled; the client (Android) is expected to decode and display it.
- The daemon always controls only the session Windows marks as current (`GetCurrentSession()`).
- Volume applies to the default output device; per-application volumes (volume mixer) and
  switching the output are not handled.
- When another application occupies the port, the daemon exits with an explanatory message
  (exit code 2) instead of a traceback. Windows lets `0.0.0.0:8765` be bound even when somebody
  else listens on `127.0.0.1:8765` – in that case the daemon runs and works from the LAN, but
  `http://localhost:8765` on that PC leads to the foreign application; a warning is written to the
  log.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Port 8765 je už obsazený.` | Either PCMote is already running (possibly hidden in the tray), or another application holds the port. Close it, or run `python main.py --port 8766` and enter the same port in the phone. |
| `mDNS zaregistrováno` never appears | No network interface, or the firewall blocks UDP 5353. Connecting by manual IP address always works. |
| `Hlasitost: nedostupná` | `pycaw` or the audio device is missing; playback keeps working, just without the slider. |
| Artwork missing while audio plays | The Windows thumbnail was not available yet; the daemon retries 5 × 1 s and the cache is reset when the track changes. |
| The status dialog is "behind" another window | The daemon opens it as `MB_TOPMOST` and switches it to the foreground. If another application still covers it, click it in the taskbar – it is a separate window. || PCMote does not start after login | Check *Task Manager → Startup apps* for `PCMote`. If it is missing, run `PCMote.exe --install-autostart` (or `python main.py --install-autostart`). If it is there but points to a moved EXE, run `--install-autostart` again. The log records the exact stored command. |
| Nothing happens after double-clicking `PCMote.exe` | Expected – it is a windowless build. Right-click the tray icon → *Show IP and status*, or open `%LOCALAPPDATA%\PCMote\daemon.log`. |
