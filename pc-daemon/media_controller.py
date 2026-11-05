"""WinRT integrace pro ``GlobalSystemMediaTransportControlsSessionManager``.

Modul je záměrně oddělený od síťové vrstvy: neobsahuje žádné WebSocket ani
tray logiky. Ven vydává pouze neměnné :class:`MediaSnapshot` objekty přes
callback ``on_update`` a dovnitř přijímá příkazy (play/pause, next, prev, seek).

Všechny WinRT události (``MediaPropertiesChanged``, ``PlaybackInfoChanged``,
``TimelinePropertiesChanged``, ``CurrentSessionChanged``) přicházejí z vláken
WinRT threadpoolu, nikoli z asyncio event loopu. Proto se každá událost pouze
"probudí" přes ``loop.call_soon_threadsafe`` a skutečné čtení stavu proběhne
znovu v event loopu. Díky tomu není potřeba žádný zámek nad WinRT objekty.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSession as MediaSession,
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import DataReader

logger = logging.getLogger(__name__)

#: WinRT používá pro časové údaje jednotku 100 ns.
TICKS_PER_MS = 10_000

#: Obrázek alba větší než toto omezení se ignoruje (ochrana proti obřím PNG).
MAX_ARTWORK_BYTES = 8 * 1024 * 1024

#: Windows zpřístupní thumbnail často až později než metadata, proto se čtení opakuje.
ARTWORK_RETRY_DELAY = 1.0
MAX_ARTWORK_RETRIES = 5

#: Mapování WinRT stavu na stavy definované protokolem.
_WIRE_STATE = {
    PlaybackStatus.PLAYING: "PLAYING",
    PlaybackStatus.PAUSED: "PAUSED",
}


@dataclass(frozen=True)
class MediaSnapshot:
    """Neměnný snapshot toho, co právě hraje na PC."""

    source_app: str = ""
    state: str = "STOPPED"
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    position_ms: int = 0
    artwork_hash: Optional[str] = None
    artwork: Optional[bytes] = None

    def to_wire(self) -> dict:
        """Převede snapshot na JSON kompatibilní se specifikací protokolu."""
        return {
            "event": "state_update",
            "source_app": self.source_app,
            "state": self.state,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "position_ms": self.position_ms,
            "artwork_hash": self.artwork_hash,
            "artwork_base64": None,
        }


class MediaController:
    """Čte stav systémového přehrávače a posílá příkazy zpět do Windows."""

    def __init__(self, on_update: Callable[[MediaSnapshot], None]) -> None:
        self._on_update = on_update
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._manager: Optional[SessionManager] = None
        self._session: Optional[MediaSession] = None
        self._manager_token: Optional[int] = None
        self._session_tokens: list[tuple[str, int]] = []
        self._refresh_lock: Optional[asyncio.Lock] = None
        self._pending_task: Optional[asyncio.Task] = None
        self._snapshot = MediaSnapshot()
        self._artwork_hash: Optional[str] = None
        self._artwork_bytes: Optional[bytes] = None
        self._artwork_title: Optional[str] = None
        self._artwork_retries = 0
        self._artwork_task: Optional[asyncio.Task] = None
        self._stopped = False

    # ------------------------------------------------------------------ stav

    @property
    def snapshot(self) -> MediaSnapshot:
        return self._snapshot

    @property
    def has_session(self) -> bool:
        return self._session is not None

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Získá manager relací a začne sledovat změny."""
        self._loop = asyncio.get_running_loop()
        self._refresh_lock = asyncio.Lock()
        try:
            self._manager = await SessionManager.request_async()
        except Exception:
            logger.exception("Nepodařilo se získat SessionManager; daemon poběží bez metadat")
            self._manager = None
            self._publish(MediaSnapshot())
            return

        try:
            self._manager_token = self._manager.add_current_session_changed(
                self._on_manager_event
            )
        except Exception:
            logger.exception("Registrace CurrentSessionChanged selhala")

        await self._attach_current_session()
        await self.refresh()

    async def stop(self) -> None:
        """Odregistruje všechny WinRT handlery a ukončí činnost."""
        self._stopped = True
        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
            self._pending_task = None
        if self._artwork_task is not None and not self._artwork_task.done():
            self._artwork_task.cancel()
            self._artwork_task = None

        self._detach_session()

        if self._manager is not None and self._manager_token is not None:
            try:
                self._manager.remove_current_session_changed(self._manager_token)
            except Exception:
                logger.debug("Odregistrace CurrentSessionChanged selhala", exc_info=True)
        self._manager_token = None
        self._manager = None

    # ---------------------------------------------------- WinRT event handlery

    def _on_manager_event(self, _sender, _args) -> None:
        """Volá WinRT při změně aktivní relace (běží mimo event loop)."""
        self._schedule_refresh("CurrentSessionChanged")

    def _on_session_event(self, _sender, _args) -> None:
        """Volá WinRT při změně metadat / stavu / timeline (mimo event loop)."""
        self._schedule_refresh("SessionChanged")

    def _schedule_refresh(self, reason: str) -> None:
        loop = self._loop
        if loop is None or self._stopped:
            return
        try:
            loop.call_soon_threadsafe(self._request_refresh, reason)
        except RuntimeError:
            # Event loop už byl uzavřen (ukončování aplikace).
            logger.debug("Event loop uzavřen, přeskakuji refresh (%s)", reason)

    def _request_refresh(self, reason: str) -> None:
        if self._stopped:
            return
        if self._pending_task is not None and not self._pending_task.done():
            # Události přicházejí v dávkách; jeden běžící refresh stačí.
            return
        self._pending_task = asyncio.create_task(self._refresh_safely(reason))

    async def _refresh_safely(self, reason: str) -> None:
        try:
            await self._attach_current_session()
            await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Obnovení stavu přehrávače selhalo (%s)", reason)

    # ------------------------------------------------------------- relace

    async def _attach_current_session(self) -> None:
        """Přepne se na aktuální relaci a zaregistruje její handlery."""
        if self._manager is None:
            return
        try:
            session = self._manager.get_current_session()
        except Exception:
            logger.exception("GetCurrentSession selhalo")
            return

        if session is self._session:
            return

        self._detach_session()
        self._session = session
        if session is None:
            logger.info("Žádná aktivní mediální relace")
            return

        logger.info("Aktivní relace: %s", self._source_app_id(session))
        handlers = (
            ("media_properties", session.add_media_properties_changed, self._on_session_event),
            ("playback_info", session.add_playback_info_changed, self._on_session_event),
            ("timeline_properties", session.add_timeline_properties_changed, self._on_session_event),
        )
        for name, register, handler in handlers:
            try:
                self._session_tokens.append((name, register(handler)))
            except Exception:
                logger.exception("Registrace %s selhala", name)

    def _detach_session(self) -> None:
        session = self._session
        if session is None:
            self._session_tokens.clear()
            return
        removers = {
            "media_properties": session.remove_media_properties_changed,
            "playback_info": session.remove_playback_info_changed,
            "timeline_properties": session.remove_timeline_properties_changed,
        }
        for name, token in self._session_tokens:
            remover = removers.get(name)
            if remover is None:
                continue
            try:
                remover(token)
            except Exception:
                logger.debug("Odregistrace %s selhala", name, exc_info=True)
        self._session_tokens.clear()
        self._session = None
        # Nová relace může mít úplně jiný obal.
        self._artwork_title = None
        self._artwork_hash = None
        self._artwork_bytes = None
        self._artwork_retries = 0

    @staticmethod
    def _source_app_id(session: MediaSession) -> str:
        try:
            return session.source_app_user_model_id or ""
        except Exception:
            return ""

    # ------------------------------------------------------------- čtení stavu

    async def refresh(self) -> None:
        """Přečte aktuální stav z WinRT a publikuje snapshot."""
        if self._stopped:
            return
        session = self._session
        if session is None:
            if self._snapshot != MediaSnapshot():
                self._artwork_title = None
                self._artwork_hash = None
                self._artwork_bytes = None
            self._publish(MediaSnapshot())
            return

        source_app = self._source_app_id(session)
        title = artist = album = ""
        artwork: Optional[bytes] = None

        try:
            props = await session.try_get_media_properties_async()
        except Exception:
            logger.exception("TryGetMediaPropertiesAsync selhalo")
            props = None

        if props is not None:
            title = props.title or ""
            artist = props.artist or ""
            album = props.album_title or ""
            if title != self._artwork_title:
                # Jiná skladba – obal z mezipaměti už neplatí.
                self._artwork_title = title
                self._artwork_hash = None
                self._artwork_bytes = None
                self._artwork_retries = 0
            artwork = await self._read_artwork(props.thumbnail)
            if artwork is None:
                # Thumbnail nemusí být v tuhle chvíli ještě k dispozici.
                self._schedule_artwork_retry()

        state = "STOPPED"
        try:
            playback = session.get_playback_info()
            if playback is not None:
                state = _WIRE_STATE.get(playback.playback_status, "STOPPED")
        except Exception:
            logger.exception("GetPlaybackInfo selhalo")

        duration_ms = 0
        position_ms = 0
        try:
            timeline = session.get_timeline_properties()
            if timeline is not None:
                duration_ms = _timedelta_ms(timeline.end_time - timeline.start_time)
                position_ms = _timedelta_ms(timeline.position)
                if state == "PLAYING":
                    position_ms += _elapsed_since(timeline.last_updated_time)
                position_ms = max(0, position_ms)
                if duration_ms > 0:
                    position_ms = min(position_ms, duration_ms)
        except Exception:
            logger.exception("GetTimelineProperties selhalo")

        self._publish(
            MediaSnapshot(
                source_app=source_app,
                state=state,
                title=title,
                artist=artist,
                album=album,
                duration_ms=duration_ms,
                position_ms=position_ms,
                artwork_hash=self._artwork_hash,
                artwork=artwork if artwork is not None else self._artwork_bytes,
            )
        )

    def _publish(self, snapshot: MediaSnapshot) -> None:
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        try:
            self._on_update(snapshot)
        except Exception:
            logger.exception("Zpracování snapshotu selhalo")

    async def _read_artwork(self, thumbnail) -> Optional[bytes]:
        """Přečte thumbnail stream a vrátí jeho bajty (nebo ``None``)."""
        if thumbnail is None:
            # Mezipaměť nemažeme – o novou skladbu se stará :meth:`refresh`.
            return None

        stream = None
        reader = None
        try:
            stream = await thumbnail.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > MAX_ARTWORK_BYTES:
                logger.warning("Artwork má nepoužitelnou velikost: %s B", size)
                return None

            try:
                reader = DataReader(stream)
                await reader.load_async(size)
                payload = reader.read_buffer(size)
            except TypeError:
                # Některé verze pywinrt vyžadují explicitní input stream.
                if reader is not None:
                    reader.close()
                reader = DataReader(stream.get_input_stream_at(0))
                await reader.load_async(size)
                payload = reader.read_buffer(size)

            data = bytes(payload)
        except Exception:
            logger.exception("Čtení artworku selhalo")
            return None
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    logger.debug("Zavření DataReaderu selhalo", exc_info=True)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    logger.debug("Zavření streamu selhalo", exc_info=True)

        if not data:
            return None

        digest = hashlib.sha256(data).hexdigest()
        self._artwork_hash = digest
        self._artwork_bytes = data
        self._artwork_retries = 0
        logger.debug("Artwork načten: %s (%d B)", digest[:12], len(data))
        return data

    def _schedule_artwork_retry(self) -> None:
        """Naplánuje opakované čtení obalu, dokud ho Windows nezpřístupní."""
        if self._stopped or self._artwork_retries >= MAX_ARTWORK_RETRIES:
            return
        if self._artwork_task is not None and not self._artwork_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._artwork_retries += 1
        logger.debug("Artwork zatím není k dispozici, pokus %d/%d", self._artwork_retries, MAX_ARTWORK_RETRIES)
        self._artwork_task = loop.create_task(self._retry_artwork())

    async def _retry_artwork(self) -> None:
        try:
            await asyncio.sleep(ARTWORK_RETRY_DELAY)
            await self._refresh_safely("artwork-retry")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Opakované čtení artworku selhalo")

    # ---------------------------------------------------------------- příkazy

    async def toggle_play_pause(self) -> bool:
        return await self._run_session_action("try_toggle_play_pause_async", "PLAY_PAUSE")

    async def skip_next(self) -> bool:
        return await self._run_session_action("try_skip_next_async", "NEXT")

    async def skip_previous(self) -> bool:
        return await self._run_session_action("try_skip_previous_async", "PREV")

    async def seek_to(self, position_ms: int) -> bool:
        session = self._session
        if session is None:
            logger.warning("SEEK ignorován: žádná aktivní relace")
            return False
        position_ms = max(0, int(position_ms))
        try:
            accepted = await session.try_change_playback_position_async(position_ms * TICKS_PER_MS)
        except Exception:
            logger.exception("TryChangePlaybackPositionAsync selhalo")
            return False
        logger.info("SEEK na %s ms -> %s", position_ms, bool(accepted))
        await self.refresh()
        return bool(accepted)

    async def _run_session_action(self, method_name: str, label: str) -> bool:
        session = self._session
        if session is None:
            logger.warning("%s ignorováno: žádná aktivní relace", label)
            return False
        action = getattr(session, method_name, None)
        if action is None:
            logger.warning("%s není na této verzi Windows dostupné", label)
            return False
        try:
            accepted = await action()
        except Exception:
            logger.exception("%s selhalo", label)
            return False
        logger.info("%s -> %s", label, bool(accepted))
        await self.refresh()
        return bool(accepted)


def _timedelta_ms(value) -> int:
    """Převede ``datetime.timedelta`` (WinRT TimeSpan) na milisekundy."""
    if value is None:
        return 0
    try:
        return int(value.total_seconds() * 1000)
    except AttributeError:
        return 0


def _elapsed_since(last_updated: Optional[datetime]) -> int:
    """Vrátí, kolik ms uběhlo od poslední aktualizace timeline."""
    if last_updated is None:
        return 0
    try:
        reference = last_updated
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - reference
        return max(0, int(delta.total_seconds() * 1000))
    except Exception:
        return 0
