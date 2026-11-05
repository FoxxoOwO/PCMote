"""PCMote – Windows daemon pro vzdálené ovládání přehrávače médií.

Architektura:
    * :mod:`media_controller` čte stav systémového přehrávače přes WinRT.
    * Tento modul obsluhuje WebSocket server (port 8765), mDNS registraci
      (``_pcmote._tcp.local.``) a ikonu v System Tray.

Spuštění::

    python main.py                # tray ikona, konzole skrytá
    python main.py --no-tray      # bez tray ikony (např. pro ladění)
    python main.py --debug        # konzole zůstane viditelná, podrobné logy
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import websockets
from websockets.asyncio.server import serve

import autostart
import icon as icon_module
from media_controller import MediaController, MediaSnapshot
from volume_controller import VolumeController, VolumeState

try:
    import pystray
    from PIL import Image
except ImportError:  # pragma: no cover - tray je volitelný
    pystray = None
    Image = None

try:
    from zeroconf import ServiceInfo
    from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

except ImportError:  # pragma: no cover - mDNS je volitelné
    ServiceInfo = None
    AsyncServiceInfo = None
    AsyncZeroconf = None

logger = logging.getLogger("pcmote")

DEFAULT_PORT = 8765
SERVICE_TYPE = "_pcmote._tcp.local."
SERVICE_NAME = "PCMote._pcmote._tcp.local."
APP_NAME = "PCMote"
STATUS_DIALOG_TITLE = f"{APP_NAME} – status"

# Příznaky pro MessageBoxW (winuser.h)
MB_ICONINFORMATION = 0x00000040
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PCMote"

#: Jak často se kontroluje změna hlasitosti provedená mimo telefon (klávesnice, mixér).
VOLUME_POLL_INTERVAL = 2.0

#: Jak dlouho počkat po přihlášení, než se daemon připojí k sítí a zvukovému zařízení.
AUTOSTART_DELAY = 3.0

# --------------------------------------------------------------------- klient


@dataclass
class _ClientState:
    """Stav jednoho připojeného telefonu."""

    websocket: object
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    artwork_hash: Optional[str] = None

    @property
    def peer(self) -> str:
        try:
            address = self.websocket.remote_address
            return f"{address[0]}:{address[1]}" if address else "neznámý"
        except Exception:
            return "neznámý"


# -------------------------------------------------------------------- daemon


class DaemonStartupError(RuntimeError):
    """Chyba, kterou má uživatel vidět jako srozumitelnou zprávu, ne jako traceback."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def user_text(self) -> str:
        if not self.hint:
            return self.message
        return self.message + "\n\n" + self.hint


class PCMoteDaemon:
    """WebSocket server + mDNS + System Tray nad :class:`MediaController`."""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT, use_tray: bool = True) -> None:
        self.host = host
        self.port = port
        self.use_tray = use_tray

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown = asyncio.Event()
        self._server = None
        self._zeroconf = None
        self._service_info = None
        self._tray_icon = None
        self._tray_thread: Optional[threading.Thread] = None
        self._status_lock = threading.Lock()
        self._status_dialog_open = False

        self._clients: dict[object, _ClientState] = {}
        self._controller = MediaController(self._on_media_update)
        self._volume = VolumeController()
        self._volume_state: Optional[VolumeState] = None
        self._volume_task: Optional[asyncio.Task] = None
        self._snapshot = MediaSnapshot()
        self._broadcast_task: Optional[asyncio.Task] = None
        self._broadcast_dirty = False

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Spustí daemon a běží až do požadavku na ukončení."""
        self._loop = asyncio.get_running_loop()

        await self._start_websocket_server()
        await self._register_mdns()
        await self._controller.start()
        self._snapshot = self._controller.snapshot
        self._volume_state = self._volume.read()
        self._volume_task = asyncio.get_running_loop().create_task(self._poll_volume())
        if self._volume_state is not None:
            logger.info("Hlasitost: %d %%%s", self._volume_state.percent, " (ztlumeno)" if self._volume_state.muted else "")
        if self.use_tray:
            self._start_tray()

        logger.info("PCMote běží na %s:%s (%d připravených rozhraní)", self.host, self.port, len(self._local_addresses()))

        try:
            await self._shutdown.wait()
        finally:
            await self._stop_everything()

    def request_shutdown(self) -> None:
        """Bezpečně (i z jiného vlákna) požádá o ukončení."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._shutdown.set)
        except RuntimeError:
            logger.debug("Event loop už neběží, ukončení ignorováno")

    async def _stop_everything(self) -> None:
        logger.info("Ukončuji PCMote…")

        if self._broadcast_task is not None and not self._broadcast_task.done():
            self._broadcast_task.cancel()
        if self._volume_task is not None and not self._volume_task.done():
            self._volume_task.cancel()

        for state in list(self._clients.values()):
            try:
                await state.websocket.close(code=1001, reason="Server se vypíná")
            except Exception:
                logger.debug("Zavření klientského socketu selhalo", exc_info=True)
        self._clients.clear()

        await self._controller.stop()

        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                logger.debug("wait_closed selhalo", exc_info=True)
            self._server = None

        await self._unregister_mdns()
        self._stop_tray()

    # ------------------------------------------------------------ WebSocket

    async def _start_websocket_server(self) -> None:
        await self._warn_about_loopback_conflict()
        try:
            self._server = await serve(
                self._handle_client,
                self.host,
                self.port,
                ping_interval=20,
                ping_timeout=20,
                max_size=4 * 1024 * 1024,
            )
        except OSError as exc:
            if getattr(exc, "errno", None) in (10048, 98):
                raise DaemonStartupError(
                    f"Port {self.port} je už obsazený.",
                    "Nejčastěji to znamená, že PCMote už běží (třeba schovaný v System Tray). "
                    "Ukončete ho přes ikonu → Ukončit, nebo spusťte tento kus na jiném portu: "
                    f"python main.py --port {self.port + 1}",
                ) from None
            raise DaemonStartupError(
                f"Nepodařilo se otevřít WebSocket server na {self.host}:{self.port} ({exc}).",
                "Zkontrolujte, že adresa patří tomuto PC, a zkuste jiný port přes --port.",
            ) from None
        logger.info("WebSocket server naslouchá na %s:%s", self.host, self.port)

    async def _warn_about_loopback_conflict(self) -> None:
        """Upozorní, když port na 127.0.0.1 drží jiná aplikace.

        Windows dovolí navázat 0.0.0.0:port i tehdy, když už někdo poslouchá na
        127.0.0.1:port. Připojení z LAN (a z telefonu) pak chodí do našeho daemonu,
        ale ``localhost`` na tomhle PC skončí v cizí aplikaci – to mate při ladění.
        """
        loop = asyncio.get_running_loop()
        transport = None
        try:
            transport, _ = await asyncio.wait_for(
                loop.create_connection(asyncio.Protocol, "127.0.0.1", self.port), timeout=0.5
            )
        except (OSError, asyncio.TimeoutError):
            return
        finally:
            if transport is not None:
                transport.abort()
        logger.warning(
            "Port %s na 127.0.0.1 už používá jiná aplikace – odkaz http://localhost:%s "
            "nepovede do PCMote, z LAN (a z telefonu) ale ano. Pokud to vadí, spusťte "
            "daemon s jiným portem: python main.py --port %s",
            self.port,
            self.port,
            self.port + 1,
        )

    async def _handle_client(self, websocket) -> None:
        state = _ClientState(websocket=websocket)
        self._clients[websocket] = state
        logger.info("Klient připojen: %s", state.peer)

        try:
            # Okamžitě po připojení pošleme kompletní stav včetně artworku.
            await self._send_state(state, self._snapshot)

            async for raw in websocket:
                await self._handle_command(raw)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Klient odpojen: %s", state.peer)
        except Exception:
            logger.exception("Chyba při obsluze klienta %s", state.peer)
        finally:
            self._clients.pop(websocket, None)
            logger.info("Klient odstraněn: %s (zbývá %d)", state.peer, len(self._clients))

    async def _handle_command(self, raw) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Neplatný JSON od klienta: %r", raw[:200] if isinstance(raw, str) else raw)
            return

        if not isinstance(message, dict):
            logger.warning("Příkaz není objekt: %r", message)
            return

        command = str(message.get("command", "")).upper()
        logger.info("Příkaz z telefonu: %s", message)

        if command == "PLAY_PAUSE":
            accepted = await self._controller.toggle_play_pause()
        elif command == "NEXT":
            accepted = await self._controller.skip_next()
        elif command == "PREV":
            accepted = await self._controller.skip_previous()
        elif command == "SEEK":
            position = message.get("position_ms")
            if not isinstance(position, (int, float)) or isinstance(position, bool):
                logger.warning("SEEK bez platného position_ms: %r", position)
                return
            try:
                target = int(float(position))
            except (OverflowError, ValueError):
                logger.warning("SEEK s nepoužitelnou hodnotou position_ms: %r", position)
                return
            accepted = await self._controller.seek_to(target)
        elif command == "SET_VOLUME":
            volume = message.get("volume", message.get("volume_percent"))
            if not isinstance(volume, (int, float)) or isinstance(volume, bool):
                logger.warning("SET_VOLUME bez platné hodnoty volume: %r", volume)
                return
            try:
                requested = int(round(float(volume)))
            except (OverflowError, ValueError):
                # NaN nebo nekonečno z ručně psaného JSONu.
                logger.warning("SET_VOLUME s nepoužitelnou hodnotou volume: %r", volume)
                return
            new_volume = self._volume.set_percent(requested)
            accepted = new_volume is not None
            if new_volume is not None:
                self._apply_volume(new_volume)
        elif command == "VOLUME_UP":
            new_volume = self._volume.step(self._volume.step_percent)
            accepted = new_volume is not None
            if new_volume is not None:
                self._apply_volume(new_volume)
        elif command == "VOLUME_DOWN":
            new_volume = self._volume.step(-self._volume.step_percent)
            accepted = new_volume is not None
            if new_volume is not None:
                self._apply_volume(new_volume)
        else:
            logger.warning("Neznámý příkaz: %r", command)
            return

        if not accepted:
            logger.info("Příkaz %s nebyl PC přijat", command)

    # ------------------------------------------------------------ odesílání

    def _on_media_update(self, snapshot: MediaSnapshot) -> None:
        """Callback z :class:`MediaController` – běží v event loopu."""
        self._snapshot = snapshot
        logger.info(
            "Stav: %s | %s – %s (%s/%s ms)",
            snapshot.state,
            snapshot.artist or "?",
            snapshot.title or "?",
            snapshot.position_ms,
            snapshot.duration_ms,
        )
        self._request_broadcast()

    def _apply_volume(self, state: VolumeState) -> None:
        """Zapamatuje novou hlasitost a hned ji rozešle do telefonů."""
        self._volume_state = state
        self._request_broadcast()

    def _request_broadcast(self) -> None:
        """Naplánuje rozeslání stavu (funguje i při souběhu více změn)."""
        if not self._clients:
            return
        if self._broadcast_task is None or self._broadcast_task.done():
            self._broadcast_task = asyncio.get_running_loop().create_task(self._broadcast())
        else:
            # Během rozesílání přišla další změna – po dokončení pošleme znovu.
            self._broadcast_dirty = True

    async def _poll_volume(self) -> None:
        """Sleduje hlasitost změněnou mimo telefon a rozesílá ji."""
        try:
            while True:
                await asyncio.sleep(VOLUME_POLL_INTERVAL)
                # Voláme přímo v event loopu: COM objekt je vázaný na jedno vlákno,
                # práce je to krátká (jedno WASAPI volání).
                current = self._volume.read()
                if current is not None and current != self._volume_state:
                    logger.debug("Hlasitost změněna mimo telefon: %s %%", current.percent)
                    self._apply_volume(current)
                elif current is None and self._volume_state is not None:
                    # Zařízení zmizelo (odpojený reproduktor) – přestaneme hlásit hodnotu.
                    logger.debug("Hlasitost už není dostupná")
                    self._volume_state = None
                    self._request_broadcast()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Sledování hlasitosti selhalo")

    async def _broadcast(self) -> None:
        try:
            while True:
                self._broadcast_dirty = False
                snapshot = self._snapshot
                clients = list(self._clients.values())
                if clients:
                    await asyncio.gather(
                        *(self._send_state(client, snapshot) for client in clients),
                        return_exceptions=True,
                    )
                if not self._broadcast_dirty:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Rozeslání stavu selhalo")

    async def _send_state(self, state: _ClientState, snapshot: MediaSnapshot) -> None:
        try:
            async with state.lock:
                payload = snapshot.to_wire()
                volume_state = self._volume_state
                if volume_state is None:
                    payload["volume_percent"] = None
                    payload["muted"] = None
                else:
                    payload.update(volume_state.to_wire())
                new_hash = snapshot.artwork_hash
                if snapshot.artwork and new_hash and new_hash != state.artwork_hash:
                    payload["artwork_base64"] = base64.b64encode(snapshot.artwork).decode("ascii")
                    logger.debug("Posílám artwork %s (%d B)", new_hash[:12], len(snapshot.artwork))
                else:
                    # Hash se nezměnil – šetříme přenosové pásmo.
                    payload["artwork_base64"] = None
                state.artwork_hash = new_hash
                await state.websocket.send(json.dumps(payload, ensure_ascii=False))
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Klient %s už je odpojen", state.peer)
        except Exception:
            logger.exception("Odeslání stavu klientovi %s selhalo", state.peer)

    # ----------------------------------------------------------------- mDNS

    async def _register_mdns(self) -> None:
        if AsyncZeroconf is None or AsyncServiceInfo is None:
            logger.warning("Zeroconf není nainstalován – mDNS je vypnuté")
            return
        addresses = self._local_addresses()
        if not addresses:
            logger.warning("Nepodařilo se zjistit lokální IP – mDNS se neregistruje")
            return

        hostname = socket.gethostname()
        try:
            info = AsyncServiceInfo(
                SERVICE_TYPE,
                SERVICE_NAME,
                addresses=[socket.inet_aton(addresses[0])],
                port=self.port,
                properties={"version": "1.0", "host": hostname, "app": APP_NAME},
                server=f"{hostname}.local.",
            )
            zeroconf = AsyncZeroconf()
            await zeroconf.async_register_service(info)
            self._zeroconf = zeroconf
            self._service_info = info
            logger.info("mDNS zaregistrováno: %s -> %s:%s", SERVICE_NAME, addresses[0], self.port)
        except Exception:
            logger.exception("Registrace mDNS selhala")
            await self._unregister_mdns()

    async def _unregister_mdns(self) -> None:
        zeroconf = self._zeroconf
        info = self._service_info
        self._zeroconf = None
        self._service_info = None
        if zeroconf is None:
            return
        try:
            if info is not None:
                await zeroconf.async_unregister_service(info)
            await zeroconf.async_close()
            logger.info("mDNS služba odregistrována")
        except Exception:
            logger.exception("Odregistrace mDNS selhala")

    @staticmethod
    def _local_addresses() -> list[str]:
        """Zjistí IPv4 adresy tohoto PC (bez loopbacku), primární první."""
        addresses: list[str] = []
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.settimeout(0.2)
                probe.connect(("8.8.8.8", 80))
                addresses.append(probe.getsockname()[0])
        except OSError:
            pass

        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = info[4][0]
                if address not in addresses and not address.startswith("127."):
                    addresses.append(address)
        except OSError:
            pass

        return addresses

    # ------------------------------------------------------------ System Tray

    def _start_tray(self) -> None:
        if pystray is None or Image is None:
            logger.warning("pystray/Pillow nejsou dostupné – tray ikona se nespustí")
            return

        menu = pystray.Menu(
            pystray.MenuItem("Show IP and status", self._show_status, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start with Windows",
                self._toggle_autostart,
                checked=self._autostart_checked,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit_from_tray),
        )
        self._tray_icon = pystray.Icon(APP_NAME, self._build_icon(), APP_NAME, menu)
        self._tray_thread = threading.Thread(
            target=self._run_tray, name="pcmote-tray", daemon=True
        )
        self._tray_thread.start()
        logger.info("Tray ikona spuštěna")

    def _run_tray(self) -> None:
        try:
            self._tray_icon.run()
        except Exception:
            logger.exception("Tray ikona spadla")

    @staticmethod
    def _build_icon():
        """Ikona tray – kreslí se v kódu, daemon s sebou nenese žádný obrázek."""
        return icon_module.draw_icon(64)

    def _autostart_checked(self, _item=None) -> bool:
        """Zaškrtnutí položky menu podle skutečného stavu registru."""
        try:
            return autostart.is_enabled()
        except Exception:
            logger.debug("Stav autostartu nelze zjistit", exc_info=True)
            return False

    def _toggle_autostart(self, _icon=None, _item=None) -> None:
        """Zapne nebo vypne spouštění po přihlášení přímo z tray menu."""
        try:
            enabled = autostart.toggle(host=self.host, port=self.port)
        except autostart.AutostartError as exc:
            logger.warning("Autostart se nepodařilo přepnout: %s", exc)
            self._notify(str(exc), "Automatic startup could not be changed")
            return

        logger.info("Autostart %s", "zapnut" if enabled else "vypnut")
        tray = self._tray_icon
        if tray is not None:
            try:
                tray.update_menu()
            except Exception:
                logger.debug("update_menu() selhal", exc_info=True)
        self._notify(
            "PCMote will start after you sign in"
            if enabled
            else "PCMote will no longer start after sign-in",
            "Automatic startup",
        )

    def _notify(self, message: str, title: str) -> None:
        """Bublina u tray ikony; když ji systém nepodporuje, jen se zaloguje."""
        tray = self._tray_icon
        if tray is None:
            return
        try:
            tray.notify(message, title)
        except Exception:
            logger.debug("notify() selhal", exc_info=True)

    def _status_text(self) -> str:
        addresses = self._local_addresses() or ["-"]
        snapshot = self._snapshot
        if snapshot.title:
            media = f"{snapshot.artist or '?'} – {snapshot.title} [{snapshot.state}]"
        else:
            media = "nothing is playing"
        volume_state = self._volume_state
        if volume_state is None:
            volume = "unavailable"
        elif volume_state.muted:
            volume = f"{volume_state.percent} % (muted)"
        else:
            volume = f"{volume_state.percent} %"
        return (
            f"{APP_NAME} {socket.gethostname()}\n\n"
            f"IP address: {', '.join(addresses)}\n"
            f"Port: {self.port}\n"
            f"mDNS: {SERVICE_TYPE}\n"
            f"Start with Windows: {autostart.describe(self.host, self.port)}\n"
            f"Connected devices: {len(self._clients)}\n"
            f"Playing: {media}\n"
            f"Volume: {volume}"
        )

    def _show_status(self, *_args) -> None:
        """Zobrazí stav v systémovém dialogu.

        Dvě věci, které u dialogu z tray menu kazí klikání:

        1. ``pystray`` volá callback položky menu **uvnitř okenní procedury** tray
           vlákna (``_win32.py``: ``descriptors[index - 1](self)`` hned po
           ``TrackPopupMenuEx``). Modální dialog spuštěný odtud visí na vlákně, které
           právě obsluhovalo menu.
        2. Windows nedovolí procesu na pozadí ukrást popředí, takže se dialog otevře
           *za* oknem, které má uživatel zrovna vepředu; kliknutí pak míří na to cizí
           okno a vypadá to, že dialog nereaguje na myš ani na křížek.

        Proto dialog otevíráme ve vlastním vlákně (vlastní fronta zpráv) a hned ho
        vytáhneme nad ostatní okna.
        """
        text = self._status_text()
        logger.info("Stav z tray: %s", text.replace("\n", " | "))

        with self._status_lock:
            if self._status_dialog_open:
                logger.debug("Dialog se stavem už je otevřený, přepínám do popředí")
                self._focus_status_dialog()
                return
            self._status_dialog_open = True

        threading.Thread(
            target=self._run_status_dialog,
            args=(text,),
            name="pcmote-status",
            daemon=True,
        ).start()

    def _run_status_dialog(self, text: str) -> None:
        """Tělo dialogu – běží ve vlastním vlákně, takže reaguje na myš i klávesnici."""
        if sys.platform != "win32":
            print(text)
            with self._status_lock:
                self._status_dialog_open = False
            return

        try:
            import ctypes

            threading.Thread(
                target=self._bring_status_dialog_to_front,
                args=(STATUS_DIALOG_TITLE,),
                name="pcmote-status-front",
                daemon=True,
            ).start()
            # MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
            ctypes.windll.user32.MessageBoxW(
                None, text, STATUS_DIALOG_TITLE, MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
            )
        except Exception:
            logger.exception("Zobrazení dialogu se stavem selhalo")
            print(text)
        finally:
            with self._status_lock:
                self._status_dialog_open = False

    def _bring_status_dialog_to_front(self, title: str) -> None:
        """Vytáhne dialog nad ostatní okna (Windows mu popředí často odmítne dát)."""
        import ctypes

        user32 = ctypes.windll.user32
        for _ in range(10):
            time.sleep(0.15)
            hwnd = user32.FindWindowW(None, title)
            if not hwnd:
                continue
            try:
                self._force_to_front(hwnd)
            except Exception:
                logger.debug("Vytáhnutí dialogu do popředí selhalo", exc_info=True)
                return
            if user32.GetForegroundWindow() == hwnd:
                return

    @staticmethod
    def _force_to_front(hwnd: int) -> None:
        """Posune okno navrch a zkusí aktivovat (bez ohledu na foreground lock)."""
        import ctypes

        HWND_TOPMOST = -1
        SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
        user32 = ctypes.windll.user32
        user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        )
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)

    @classmethod
    def _focus_status_dialog(cls) -> None:
        """Přepne už otevřený dialog do popředí (druhé kliknutí ho nezahodí)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.FindWindowW(None, STATUS_DIALOG_TITLE)
            if hwnd:
                cls._force_to_front(hwnd)
        except Exception:
            logger.debug("Přepnutí dialogu do popředí selhalo", exc_info=True)

    def _quit_from_tray(self, *_args) -> None:
        logger.info("Ukončení z tray menu")
        self.request_shutdown()
        self._stop_tray()

    def _stop_tray(self) -> None:
        icon = self._tray_icon
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            logger.debug("Zastavení tray ikony selhalo", exc_info=True)
        self._tray_icon = None


# ---------------------------------------------------------------------- main

def _setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if debug:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_DIR / "daemon.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # Bez zapisovatelného logu se nezhroutíme, jen o tom napíšeme na stderr.
        print(f"Logovací soubor nelze vytvořit v {LOG_DIR}", file=sys.stderr)

    if not root.handlers:
        root.addHandler(logging.NullHandler())

    # WinRT a websockets jsou v INFO režimu příliš upovídané.
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _hide_console() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)
    except Exception:
        pass


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PCMote – Windows daemon pro vzdálené ovládání přehrávače")
    parser.add_argument("--host", default="0.0.0.0", help="Adresa pro WebSocket server (výchozí 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (výchozí {DEFAULT_PORT})")
    parser.add_argument("--no-tray", action="store_true", help="Nespouštět ikonu v System Tray")
    parser.add_argument("--debug", action="store_true", help="Podrobné logy a viditelná konzole")
    parser.add_argument("--keep-console", action="store_true", help="Neskrývat konzolové okno")
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="Spuštění po přihlášení (tento příznak vkládá instalace autostartu)",
    )
    parser.add_argument(
        "--install-autostart",
        action="store_true",
        help="Zaregistrovat spuštění po přihlášení do Windows a skončit",
    )
    parser.add_argument(
        "--remove-autostart",
        action="store_true",
        help="Odregistrovat spuštění po přihlášení a skončit",
    )
    parser.add_argument(
        "--autostart-status",
        action="store_true",
        help="Vypsat stav automatického spuštění a skončit",
    )
    return parser.parse_args(argv)


def _handle_autostart_action(args: argparse.Namespace) -> int:
    """Obslouží ``--install-autostart`` / ``--remove-autostart`` / ``--autostart-status``."""
    console_visible = args.debug or args.keep_console
    lines: list[str] = []

    try:
        if args.install_autostart:
            command = autostart.enable(autostart.build_command(host=args.host, port=args.port))
            lines.append("Automatic startup is on.")
            lines.append(f"Command: {command}")
            lines.append("PCMote will start after you sign in to Windows.")
        elif args.remove_autostart:
            removed = autostart.disable()
            lines.append(
                "Automatic startup is off."
                if removed
                else "Automatic startup was not on – nothing to remove."
            )
        else:  # --autostart-status
            lines.append(autostart.describe(args.host, args.port))
            stored = autostart.read_value()
            if stored:
                lines.append(f"Command: {stored}")
    except autostart.AutostartError as exc:
        logger.error("Autostart: %s", exc)
        _show_message(f"{APP_NAME} – automatic startup", str(exc), console_visible)
        return 1

    text = "\n".join(lines)
    logger.info("Autostart: %s", text.replace("\n", " | "))
    _show_message(f"{APP_NAME} – automatic startup", text, console_visible, error=False)
    return 0


def _has_output_stream() -> bool:
    """Má proces kam psát?

    Samostatné ``PCMote.exe`` sestavené s ``--noconsole`` nemá při spuštění
    dvojklikem ``sys.stdout`` ani ``sys.stderr`` – tam je potřeba dialog.
    Když ho ale spustí skript nebo konzole, proudy existují a je lepší jen
    vypsat text (skript tak nikdy nečeká na kliknutí).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.fileno() >= 0:
                return True
        except (OSError, ValueError):
            continue
    return False


def _show_message(title: str, text: str, console_visible: bool, error: bool = True) -> None:
    """Zobrazí zprávu; dialog jen tehdy, když uživatel nemá kam číst výstup."""
    if console_visible or sys.platform != "win32" or _has_output_stream():
        print(f"{title}: {text}", file=sys.stderr if error else sys.stdout)
        return
    if sys.platform == "win32":
        try:
            import ctypes

            # 0x10 = MB_ICONERROR, 0x40 = MB_ICONINFORMATION, 0x0 = MB_OK;
            # majitel None → dialog se zobrazí i bez konzole.
            icon = 0x10 if error else MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, text, title, icon)
            return
        except Exception:
            logger.debug("MessageBox selhal", exc_info=True)
        print(f"{title}: {text}", file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    if not args.debug and not args.keep_console:
        _hide_console()

    _setup_logging(args.debug)

    if args.install_autostart or args.remove_autostart or args.autostart_status:
        return _handle_autostart_action(args)

    logger.info("Spouštím %s (Python %s)", APP_NAME, sys.version.split()[0])

    if args.autostart:
        # Po přihlášení ještě nemusí být hotová síť ani zvukové zařízení.
        logger.info("Spuštěno po přihlášení, čekám %.0f s", AUTOSTART_DELAY)
        time.sleep(AUTOSTART_DELAY)

    if sys.platform != "win32":
        logger.warning("Tento daemon je určen pro Windows; WinRT média nebudou dostupná")

    daemon = PCMoteDaemon(host=args.host, port=args.port, use_tray=not args.no_tray)
    console_visible = args.debug or args.keep_console
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Ukončeno klávesnicí")
    except DaemonStartupError as exc:
        logger.error("Start selhal: %s", exc.user_text().replace("\n\n", " | "))
        _show_message(f"{APP_NAME} se nepodařilo spustit", exc.user_text(), console_visible)
        return 2
    except Exception as exc:
        logger.exception("Neočekávaná chyba")
        _show_message(
            f"{APP_NAME} se neočekávaně ukončil",
            f"{exc}\n\nPodrobnosti jsou v logu:\n{LOG_DIR / 'daemon.log'}",
            console_visible,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
