"""Sestaví samostatný ``PCMote.exe`` (PyInstaller).

Použití::

    pip install -r requirements-build.txt
    python build_exe.py                 # jeden soubor dist/PCMote.exe
    python build_exe.py --onedir        # složka dist/PCMote/ – rychlejší start
    python build_exe.py --clean         # smazat build/ a dist/ před sestavením

Výsledek nepotřebuje Python ani nainstalované knihovny – stačí ho zkopírovat
na jiný Windows PC. WinRT projekce (``winrt.windows.*``) si PyInstaller najde
sám díky hooku, který ``winrt-runtime`` registruje; ručně se doplňují jen
knihovny, které se importují za běhu (``pystray`` backend, ``comtypes``,
``pycaw``).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"

#: Moduly, které PyInstaller nevidí v import grafu.
HIDDEN_IMPORTS = (
    "pystray._win32",  # backend se vybírá až za běhu podle platformy
    "pystray._util.win32",
    "winrt.windows.media.control",
    "winrt.windows.storage.streams",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
)

COLLECT_SUBMODULES = ("comtypes", "pycaw")

EXCLUDED_MODULES = ("tkinter", "unittest", "pydoc", "pdb")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sestaví PCMote.exe")
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Složka s EXE a knihovnami (rychlejší start) místo jednoho souboru",
    )
    parser.add_argument("--clean", action="store_true", help="Smazat build/ a dist/ před sestavením")
    parser.add_argument("--no-icon", action="store_true", help="Nepřidávat ikonu do EXE")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if sys.platform != "win32":
        print("PCMote.exe lze sestavit jen na Windows.", file=sys.stderr)
        return 1

    try:
        import PyInstaller.__main__
    except ImportError:
        print(
            "Chybí PyInstaller. Nainstalujte ho:\n    pip install -r requirements-build.txt",
            file=sys.stderr,
        )
        return 1

    if args.clean:
        for directory in (BUILD_DIR, DIST_DIR):
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)

    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    pyinstaller_args = [
        str(ROOT / "main.py"),
        "--name",
        "PCMote",
        "--onedir" if args.onedir else "--onefile",
        "--noconsole",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "pyinstaller"),
        "--specpath",
        str(BUILD_DIR),
        "--log-level",
        "WARN",
        "--noconfirm",
    ]

    if not args.no_icon:
        from icon import save_ico

        ico_path = save_ico(BUILD_DIR / "pcmote.ico")
        pyinstaller_args += ["--icon", str(ico_path)]

    for module in HIDDEN_IMPORTS:
        pyinstaller_args += ["--hidden-import", module]
    for package in COLLECT_SUBMODULES:
        pyinstaller_args += ["--collect-submodules", package]
    for module in EXCLUDED_MODULES:
        pyinstaller_args += ["--exclude-module", module]

    print("PyInstaller:", " ".join(pyinstaller_args))
    PyInstaller.__main__.run(pyinstaller_args)

    target = DIST_DIR / ("PCMote" if args.onedir else "PCMote.exe")
    if not target.exists():
        print("Sestavení nedoběhlo – cílový soubor neexistuje.", file=sys.stderr)
        return 1

    if target.is_file():
        size_mb = target.stat().st_size / (1024 * 1024)
        print(f"Hotovo: {target} ({size_mb:.1f} MB)")
    else:
        print(f"Hotovo: {target} (složka)")

    print("\nInstalace autostartu z EXE:")
    print(f'    "{target}" --install-autostart')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
