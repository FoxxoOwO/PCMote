"""Automatické spuštění PCMote po přihlášení do Windows.

Používá se klíč ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``:

* nepotřebuje administrátorská práva (na rozdíl od naplánované úlohy),
* položka je vidět a vypnutelná ve Správci úloh → Po spuštění i v Nastavení,
* funguje stejně pro ``python main.py`` i pro samostatné ``PCMote.exe``.

Modul se záměrně neopírá o nic, co by v PyInstalleru chybělo – jen ``winreg``
a ``sys``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

try:  # jen Windows; import chráníme, aby modul šlo načíst i jinde (testy, CI)
    import winreg
except ImportError:  # pragma: no cover - mimo Windows
    winreg = None  # type: ignore[assignment]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "PCMote"

#: Příznak, kterým se daemon pozná, že ho spustil Windows po přihlášení.
AUTOSTART_FLAG = "--autostart"

#: Výchozí hodnoty – do příkazové řádky se zapisují jen když se liší.
DEFAULT_PORT = 8765
DEFAULT_HOST = "0.0.0.0"


class AutostartError(RuntimeError):
    """Registr se nepodařilo přečíst nebo upravit."""


def is_supported() -> bool:
    """Autostart umíme jen na Windows a jen když je dostupný ``winreg``."""
    return sys.platform == "win32" and winreg is not None


def executable_command() -> str:
    """Příkaz spouštějící tento daemon – bez parametrů.

    * Ze samostatného EXE (PyInstaller nastaví ``sys.frozen``) se použije cesta
      k EXE.
    * Ze zdrojáku se použije ``pythonw.exe`` (žádné konzolové okno při startu)
      a absolutní cesta k ``main.py``. Když ``pythonw.exe`` chybí, použije se
      běžný ``python.exe``.
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'

    script = Path(__file__).resolve().with_name("main.py")
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.exists():
        interpreter = pythonw
    return f'"{interpreter}" "{script}"'


def build_command(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    extra_args: Optional[list[str]] = None,
) -> str:
    """Sestaví celý příkaz pro registr včetně ``--autostart`` a portu."""
    parts = [executable_command(), AUTOSTART_FLAG]
    if host and host != DEFAULT_HOST:
        parts.append(f"--host {host}")
    if port and port != DEFAULT_PORT:
        parts.append(f"--port {int(port)}")
    if extra_args:
        parts.extend(extra_args)
    return " ".join(parts)


def _normalize(command: str) -> str:
    """Porovnání cest bez ohledu na velikost písmen a oddělovače."""
    return command.replace("/", "\\").strip().strip('"').casefold()


def read_value() -> Optional[str]:
    """Vrátí příkaz uložený v registru, nebo ``None`` když autostart není."""
    if not is_supported():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:  # type: ignore[union-attr]
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)  # type: ignore[union-attr]
            return str(value)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AutostartError(f"Klíč {RUN_KEY} nelze přečíst: {exc}") from exc


def is_enabled() -> bool:
    """Je položka v registru vůbec zapsaná?"""
    try:
        return read_value() is not None
    except AutostartError:
        return False


def is_current(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Odpovídá zapsaná položka právě běžícímu daemonu (cesta i port)?"""
    try:
        stored = read_value()
    except AutostartError:
        return False
    if stored is None:
        return False
    return _normalize(stored) == _normalize(build_command(host=host, port=port))


def enable(command: Optional[str] = None) -> str:
    """Zapíše položku do registru a vrátí uložený příkaz."""
    if not is_supported():
        raise AutostartError("Automatické spuštění podporuje jen Windows.")
    target = command or build_command()
    try:
        with winreg.CreateKeyEx(  # type: ignore[union-attr]
            winreg.HKEY_CURRENT_USER,  # type: ignore[union-attr]
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,  # type: ignore[union-attr]
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, target)  # type: ignore[union-attr]
    except OSError as exc:
        raise AutostartError(f"Položku autostartu nelze zapsat: {exc}") from exc
    return target


def disable() -> bool:
    """Smaže položku z registru. Vrací ``True``, když něco smazala."""
    if not is_supported():
        raise AutostartError("Automatické spuštění podporuje jen Windows.")
    try:
        with winreg.OpenKey(  # type: ignore[union-attr]
            winreg.HKEY_CURRENT_USER,  # type: ignore[union-attr]
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,  # type: ignore[union-attr]
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)  # type: ignore[union-attr]
            return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AutostartError(f"Položku autostartu nelze smazat: {exc}") from exc


def toggle(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Přepne autostart a vrátí nový stav (``True`` = zapnuto)."""
    if is_enabled():
        disable()
        return False
    enable(build_command(host=host, port=port))
    return True


def describe(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """Krátký popis pro tray dialog a ``--autostart-status``."""
    if not is_supported():
        return "unavailable (Windows only)"
    try:
        stored = read_value()
    except AutostartError as exc:
        return f"error ({exc})"
    if stored is None:
        return "off"
    if _normalize(stored) == _normalize(build_command(host=host, port=port)):
        return "on"
    return f"on, but for a different command: {stored}"
