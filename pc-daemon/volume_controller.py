"""Ovládání hlasitosti výchozího zvukového výstupu Windows.

Windows Media Transport Controls (WinRT) hlasitost neexponují, proto se používá
WASAPI přes knihovnu :mod:`pycaw` (obdoba ``IAudioEndpointVolume``). Modul je
záměrně izolovaný od zbytku daemonu:

* pokud ``pycaw``/``comtypes`` nejsou nainstalované, daemon dál funguje a jen
  hlásí hlasitost jako nedostupnou (``null`` v JSONu),
* všechny COM operace jsou chráněné zámkem, protože objekt COM není vláknově
  bezpečný a daemon ho čte z event loopu i z tray vlákna.
* polling fallback (2 s) – callback registrace je v Pythonu problematická.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: O kolik procent se změní hlasitost u příkazů ``VOLUME_UP``/``VOLUME_DOWN``.
STEP_PERCENT = 5

#: Minimální a maximální hodnota, kterou umíme nastavit.
MIN_PERCENT = 0
MAX_PERCENT = 100

#: Interval pollingu v sekundách.
POLL_INTERVAL = 2.0


@dataclass(frozen=True)
class VolumeState:
    """Aktuální hlasitost výchozího výstupu."""

    percent: int
    muted: bool

    def to_wire(self) -> dict[str, object]:
        """Převede stav na pole zprávy ``state_update``."""
        return {"volume_percent": self.percent, "muted": self.muted}


class VolumeController:
    """Čte a mění hlavní hlasitost výchozího reproduktoru."""

    def __init__(
        self,
        step_percent: int = STEP_PERCENT,
        on_external_change: Callable[[], None] | None = None
    ) -> None:
        self.step_percent = max(1, min(int(step_percent), 25))
        self._lock = threading.RLock()
        self._interface = None
        self._device_name: str | None = None
        self._warning_logged = False
        self._on_external_change = on_external_change
        self._poll_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Nastaví event loop pro plánování callbacků."""
        self._loop = loop

    # ------------------------------------------------------------------ čtení

    def read(self) -> VolumeState | None:
        """Vrátí aktuální hlasitost, nebo ``None`` když ji nelze zjistit."""
        with self._lock:
            interface = self._ensure_interface()
            if interface is None:
                return None
            try:
                scalar = float(interface.GetMasterVolumeLevelScalar())
                muted = bool(interface.GetMute())
            except (AttributeError, TypeError, OSError):
                self._invalidate("čtení hlasitosti selhalo")
                return None
            return VolumeState(percent=_scalar_to_percent(scalar), muted=muted)

    # ----------------------------------------------------------------- zápis

    def set_percent(self, percent: int) -> VolumeState | None:
        """Nastaví hlasitost na ``percent`` (0–100) a vrátí nový stav."""
        try:
            target = int(percent)
        except (TypeError, ValueError):
            logger.warning("Neplatná hlasitost: %r", percent)
            return None
        target = max(MIN_PERCENT, min(MAX_PERCENT, target))

        with self._lock:
            interface = self._ensure_interface()
            if interface is None:
                return None
            try:
                interface.SetMasterVolumeLevelScalar(target / 100.0, None)
                if target > 0 and interface.GetMute():
                    interface.SetMute(0, None)
                    logger.debug("Výstup byl ztlumený – odtlumeno")
                scalar = float(interface.GetMasterVolumeLevelScalar())
                muted = bool(interface.GetMute())
            except (AttributeError, TypeError, OSError):
                self._invalidate("nastavení hlasitosti selhalo")
                return None

            state = VolumeState(percent=_scalar_to_percent(scalar), muted=muted)
            logger.info("Hlasitost nastavena na %d %%%s", state.percent, " (ztlumeno)" if state.muted else "")
            return state

    def step(self, delta_percent: int | None = None) -> VolumeState | None:
        """Přičte/odečte krok hlasitosti (výchozí :data:`STEP_PERCENT`)."""
        delta = self.step_percent if delta_percent is None else int(delta_percent)
        current = self.read()
        if current is None:
            return None
        return self.set_percent(current.percent + delta)

    # ------------------------------------------------------------- COM vrstva

    def _ensure_interface(self):
        """Lazurovaně aktivuje ``IAudioEndpointVolume`` (volá se pod zámkem)."""
        if self._interface is not None:
            return self._interface

        try:
            import warnings

            warnings.filterwarnings("ignore", category=FutureWarning, message=r".*comtypes.*")

            import comtypes
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except (ImportError, AttributeError, OSError) as exc:
            self._log_unavailable("pycaw/comtypes nejsou dostupné", exc)
            return None

        try:
            comtypes.CoInitialize()
        except Exception:
            logger.debug("CoInitialize už bylo hotové", exc_info=True)

        try:
            speakers = AudioUtilities.GetSpeakers()
            if speakers is None:
                self._log_unavailable("nebyl nalezen výchozí reproduktor", None)
                return None
            interface = getattr(speakers, "EndpointVolume", None)
            if interface is None:
                from ctypes import POINTER, cast

                from comtypes import CLSCTX_ALL
                interface = cast(
                    speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                    POINTER(IAudioEndpointVolume),
                )
            self._interface = interface
            self._device_name = getattr(speakers, "FriendlyName", None) or "neznámé zařízení"
            logger.info("Hlasitost: ovládám \"%s\"", self._device_name)
            return self._interface
        except (AttributeError, TypeError, OSError, ImportError) as exc:
            self._log_unavailable("aktivace zvukového výstupu selhala", exc)
            return None

    def start_polling(self, loop: asyncio.AbstractEventLoop) -> None:
        """Spustí polling task."""
        self.set_loop(loop)
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = loop.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Polling smyčka pro hlasitost."""
        last_state = self.read()
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if self._loop and self._loop.is_closed():
                break
            state = self.read()
            if state != last_state:
                last_state = state
                if self._on_external_change:
                    try:
                        self._on_external_change()
                    except Exception:
                        logger.debug("Polling volume change error", exc_info=True)

    def stop_polling(self) -> None:
        """Zastaví polling task."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None

    def _invalidate(self, reason: str) -> None:
        """Zahodí COM ukazatel, aby příští pokus vytvořil nový."""
        logger.warning("%s – zkouším obnovit spojení se zvukovým výstupem", reason)
        self._interface = None

    def _log_unavailable(self, reason: str, exc: BaseException | None) -> None:
        """Zaloguje problém jen jednou, aby log nezaplavily opakované chyby."""
        if self._warning_logged:
            logger.debug("%s (%s)", reason, exc)
            return
        self._warning_logged = True
        if exc is None:
            logger.warning("%s – hlasitost nebude dostupná", reason)
        else:
            logger.warning("%s – hlasitost nebude dostupná: %s", reason, exc)

    def cleanup(self) -> None:
        """Uvolní zdroje při ukončení."""
        self.stop_polling()
        self._interface = None


def _scalar_to_percent(scalar: float) -> int:
    """Převede skalár 0.0–1.0 na procenta 0–100."""
    return max(MIN_PERCENT, min(MAX_PERCENT, round(scalar * 100)))