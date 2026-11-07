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
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO

from PIL import Image
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSession as MediaSession,
)
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)
from winrt.windows.media.control import (
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

#: WinRT volání občas visí (COM se zasekne) – po této době ho považujeme za mrtvé.
REFRESH_TIMEOUT = 5.0

#: Záchranný polling: i bez událostí z WinRT pravidelný refresh udrží stav a pozici živé.
SAFETY_POLL_INTERVAL = 2.0

#: Po transportním příkazu Windows chvíli trvá, než promítne nový stav do timeline.
COMMAND_SETTLE_MS = 250

#: Maximální rozměry artworku pro odeslání na telefon (downscale).
MAX_ARTWORK_DIMENSION = 512

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
    artwork_hash: str | None = None
    artwork: bytes | None = None

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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._manager: SessionManager | None = None
        self._session: MediaSession | None = None
        self._manager_token: int | None = None
        self._session_tokens: list[tuple[str, int]] = []
        self._refresh_lock: asyncio.Lock | None = None
        self._pending_task: asyncio.Task | None = None
        self._snapshot = MediaSnapshot()
        self._artwork_hash: str | None = None
        self._artwork_bytes: bytes | None = None
        self._artwork_title: str | None = None
        self._artwork_retries = 0
        self._artwork_task: asyncio.Task | None = None
        self._safety_task: asyncio.Task | None = None
        self._timeouts = 0
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
        self._safety_task = self._loop.create_task(self._safety_poll())

    async def _safety_poll(self) -> None:
        """Pravidelný refresh, který udrží stav a pozici živé i bez WinRT událostí.

        Windows některé přehrávače nehlásí spolehlivě (timeline se neaktualizuje,
        událost nedorazí). Polling je záchranná síť, ne hlavní cesta – proto je
        pomalý a používá stejný timeout jako událostní refresh.
        """
        try:
            while not self._stopped:
                await asyncio.sleep(SAFETY_POLL_INTERVAL)
                if self._stopped:
                    return
                await self._refresh_safely("safety-poll")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Záchranný polling selhal")

    async def stop(self) -> None:
        """Odregistruje všechny WinRT handlery a ukončí činnost."""
        self._stopped = True
        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
            self._pending_task = None
        if self._artwork_task is not None and not self._artwork_task.done():
            self._artwork_task.cancel()
            self._artwork_task = None
        if self._safety_task is not None and not self._safety_task.done():
            self._safety_task.cancel()
            self._safety_task = None

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
        task = self._pending_task
        if task is not None and not task.done():
            # Běžící refresh obvykle stačí. Když ale visí na WinRT volání, další
            # události by se už nikdy nezpracovaly – proto ho po timeoutu zabijeme.
            try:
                started = task._pcmote_started
            except AttributeError:
                started = None
            if started is None:
                started = self._loop.time() if self._loop else 0.0
                task._pcmote_started = started
            if self._loop is not None and self._loop.time() - started < REFRESH_TIMEOUT:
                return
            logger.warning(
                "Refresh visí >%.0f s (%s) – ruším a zkouším znovu", REFRESH_TIMEOUT, reason
            )
            task.cancel()
        created = asyncio.create_task(self._refresh_safely(reason))
        if self._loop is not None:
            created._pcmote_started = self._loop.time()
        self._pending_task = created

    async def _refresh_safely(self, reason: str) -> None:
        try:
            await asyncio.wait_for(
                self._attach_current_session(), timeout=REFRESH_TIMEOUT
            )
            await asyncio.wait_for(self.refresh(), timeout=REFRESH_TIMEOUT)
            self._timeouts = 0
        except TimeoutError:
            # WinRT/COM přestal odpovídat (typicky hned po transportním příkazu).
            # Zahodíme relaci a po druhém timeoutu v řadě i celý manager.
            self._timeouts += 1
            logger.warning(
                "Refresh vypršel (%.0f s, %s) – obnovuji relaci (timeout #%d)",
                REFRESH_TIMEOUT,
                reason,
                self._timeouts,
            )
            self._detach_session()
            if self._timeouts >= 2:
                self._manager = None
                self._timeouts = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Obnovení stavu přehrávače selhalo (%s)", reason)

    # ------------------------------------------------------------- relace

    @staticmethod
    def _session_status(session) -> int:
        """Vrátí WinRT playback status relace (4 = PLAYING) nebo -1."""
        try:
            info = session.get_playback_info()
            return int(info.playback_status) if info is not None else -1
        except (AttributeError, TypeError, OSError):
            # Relace mohla mezitím zmizet (přehrávač zavřen) – bereme jako neznámý stav.
            return -1

    def _pick_session(self):
        """Vybere relaci, kterou chceme ovládat.

        ``GetCurrentSession()`` vrací tu, kterou Windows naposledy považovaly za
        aktivní – a to se mění samo (prohlížeč přehraje video, jiná app zahraje
        zvuk). Slepé následování má za následek, že telefon ovládá cizí aplikaci
        a stav skutečného přehrávače se přestane aktualizovat.

        Pořadí priorit:
        1. relace, kterou jsme ovládali naposledy, pokud stále existuje,
        2. jakákoli relace, která právě hraje,
        3. ``GetCurrentSession()`` jako poslední záchrana.
        """
        manager = self._manager
        if manager is None:
            return None

        try:
            sessions = list(manager.get_sessions())
        except Exception:
            logger.exception("GetSessions selhalo")
            sessions = []

        if self._session is not None:
            current_id = self._source_app_id(self._session)
            for candidate in sessions:
                if self._source_app_id(candidate) == current_id:
                    if self._session_status(candidate) == 4:
                        # Stále hraje – držíme se ho, i když Windows vybral jinam.
                        return candidate
                    break

        for candidate in sessions:
            if self._session_status(candidate) == 4:
                return candidate

        try:
            return manager.get_current_session()
        except Exception:
            logger.exception("GetCurrentSession selhalo")
            return None

    async def _attach_current_session(self) -> None:
        """Přepne se na relaci, kterou chceme ovládat, a zaregistruje handlery."""
        if self._manager is None:
            await self._request_manager()
            if self._manager is None:
                return
        session = self._pick_session()

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

    async def _request_manager(self) -> None:
        """Znovu si vyžádá SessionManager.

        WinRT občas přestane odpovídat poté, co přehrávač dostal transportní
        příkaz – starý manager i jeho relace pak visí. Nový manager je jediná
        spolehlivá cesta, jak se z toho dostat.
        """
        try:
            self._manager = await asyncio.wait_for(
                SessionManager.request_async(), timeout=REFRESH_TIMEOUT
            )
            if self._manager_token is not None:
                try:
                    self._manager.remove_current_session_changed(self._manager_token)
                except Exception:
                    logger.debug("Odregistrace starého handleru selhala", exc_info=True)
                self._manager_token = None
            try:
                self._manager_token = self._manager.add_current_session_changed(
                    self._on_manager_event
                )
            except Exception:
                logger.exception("Registrace CurrentSessionChanged selhala")
            logger.info("SessionManager obnoven")
        except Exception:
            logger.warning("SessionManager se nepodařilo obnovit", exc_info=True)
            self._manager = None

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
        except (AttributeError, TypeError):
            return ""

    # ------------------------------------------------------------- čtení stavu

    async def refresh(self) -> None:
        """Přečte aktuální stav z WinRT a publikuje snapshot.

        WinRT (COM) volání jsou **synchronní a mohou blokovat** – `await` nad nimi
        jen zdánlivě předá řízení, ve skutečnosti drží GIL a s ním i celý event
        loop. Když se COM zasekne, zamrzne daemon i WebSocket. Proto se veškeré
        čtení provádí v jednom vyhrazeném vlákně, kde zaseknutí neškodí.
        """
        if self._stopped:
            return
        snapshot = await self._read_snapshot_in_thread()
        if snapshot is not None:
            self._publish(snapshot)

    async def _read_snapshot_in_thread(self) -> MediaSnapshot | None:
        """Spustí :meth:`_read_snapshot` ve vyhrazeném vlákně a čeká na výsledek."""
        session = self._session
        if session is None:
            if self._snapshot != MediaSnapshot():
                self._artwork_title = None
                self._artwork_hash = None
                self._artwork_bytes = None
            return MediaSnapshot()

        loop = asyncio.get_running_loop()
        future: asyncio.Future[MediaSnapshot | None] = loop.create_future()

        def _resolve(value: MediaSnapshot | None) -> None:
            if not future.done():
                future.set_result(value)

        def _reject(exc: BaseException) -> None:
            if not future.done():
                future.set_exception(exc)

        def _worker() -> None:
            try:
                result = self._read_snapshot(session)
            except BaseException as exc:  # noqa: BLE001 - předáváme do event loopu
                loop.call_soon_threadsafe(_reject, exc)
            else:
                loop.call_soon_threadsafe(_resolve, result)

        # Vlákno je daemon: pokud COM zamrzne, aplikace se i tak ukončí.
        threading.Thread(
            target=_worker, name="pcmote-winrt-read", daemon=True
        ).start()

        try:
            return await asyncio.wait_for(future, timeout=REFRESH_TIMEOUT)
        except TimeoutError:
            logger.warning("WinRT čtení neodpovědělo do %.0f s", REFRESH_TIMEOUT)
            return None

    def _read_snapshot(self, session) -> MediaSnapshot | None:
        """Synchronní čtení stavu z WinRT – běží ve vyhrazeném vlákně."""
        source_app = self._source_app_id(session)
        title = artist = album = ""
        artwork: bytes | None = None

        try:
            props = session.try_get_media_properties_async().get()
        except Exception:
            logger.exception("TryGetMediaPropertiesAsync selhalo")
            props = None

        if props is not None:
            title = props.title or ""
            artist = props.artist or ""
            album = props.album_title or ""
            if title != self._artwork_title:
                self._artwork_title = title
                self._artwork_hash = None
                self._artwork_bytes = None
                self._artwork_retries = 0
            artwork = self._read_artwork_sync(props.thumbnail)
            if artwork is None:
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

        return MediaSnapshot(
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

    def _publish(self, snapshot: MediaSnapshot) -> None:
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        try:
            self._on_update(snapshot)
        except Exception:
            logger.exception("Zpracování snapshotu selhalo")

    def _read_artwork_sync(self, thumbnail) -> bytes | None:
        """Přečte thumbnail, downscale na MAX_ARTWORK_DIMENSION (synchronně, ve vlákně)."""
        if thumbnail is None:
            return None

        stream = None
        reader = None
        try:
            stream = thumbnail.open_read_async().get()
            size = int(stream.size)
            if size <= 0 or size > MAX_ARTWORK_BYTES:
                logger.warning("Artwork má nepoužitelnou velikost: %s B", size)
                return None

            try:
                reader = DataReader(stream)
                reader.load_async(size).get()
                payload = reader.read_buffer(size)
            except TypeError:
                if reader is not None:
                    reader.close()
                reader = DataReader(stream.get_input_stream_at(0))
                reader.load_async(size).get()
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

        # Downscale, aby se na telefon neposílal 1,4MB obal.
        try:
            with Image.open(BytesIO(data)) as img:
                img.load()
                if img.width > MAX_ARTWORK_DIMENSION or img.height > MAX_ARTWORK_DIMENSION:
                    img.thumbnail((MAX_ARTWORK_DIMENSION, MAX_ARTWORK_DIMENSION), Image.LANCZOS)
                    buf = BytesIO()
                    fmt = img.format if img.format in ("JPEG", "PNG") else "PNG"
                    img.save(buf, format=fmt, optimize=True)
                    data = buf.getvalue()
                    logger.debug("Artwork downscaled na %dx%d (%d B)", img.width, img.height, len(data))
        except Exception:
            logger.warning("Downscale artworku selhalo, používám původní", exc_info=True)

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
        loop = self._loop
        if loop is None or loop.is_closed():
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
        # Timeline se v WinRT aktualizuje asynchronně – bez krátkého odkladu by
        # se do telefonu odeslala ještě stará pozice.
        await asyncio.sleep(COMMAND_SETTLE_MS / 1000)
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
        await asyncio.sleep(COMMAND_SETTLE_MS / 1000)
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


def _elapsed_since(last_updated: datetime | None) -> int:
    """Vrátí, kolik ms uběhlo od poslední aktualizace timeline."""
    if last_updated is None:
        return 0
    try:
        reference = last_updated
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - reference
        return max(0, int(delta.total_seconds() * 1000))
    except (AttributeError, TypeError, ValueError):
        return 0
