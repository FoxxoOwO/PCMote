"""Ikona PCMote – kreslí se v kódu, aby EXE nepotřeboval žádný obrázek.

Stejná kresba se používá pro ikonu v System Tray i pro ikonu výsledného
``PCMote.exe`` (``build_exe.py`` z ní vygeneruje ``.ico``).
"""

from __future__ import annotations

from pathlib import Path

BASE_SIZE = 64

#: Velikosti, které se ukládají do Windows .ico (16 px = lišta, 256 px = Alt+Tab).
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_icon(size: int = BASE_SIZE):
    """Vykreslí ikonu (bílá nota na tmavém kruhu) a vrátí ``PIL.Image``."""
    from PIL import Image, ImageDraw

    # Kreslíme 4× větší a zmenšujeme – hrany jsou pak hladké i v 16 px.
    scale = 4
    canvas = BASE_SIZE * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def box(x1: float, y1: float, x2: float, y2: float) -> list[float]:
        return [x1 * scale, y1 * scale, x2 * scale, y2 * scale]

    white = (255, 255, 255, 255)
    draw.ellipse(box(2, 2, 62, 62), fill=(27, 27, 31, 255))
    draw.ellipse(box(26, 40, 40, 54), fill=white)
    draw.rectangle(box(36, 14, 40, 48), fill=white)
    draw.polygon(
        [
            (40 * scale, 14 * scale),
            (54 * scale, 20 * scale),
            (54 * scale, 28 * scale),
            (40 * scale, 22 * scale),
        ],
        fill=white,
    )
    return image.resize((size, size), Image.LANCZOS)


def save_ico(path: Path) -> Path:
    """Uloží vícenásobnou Windows ikonu (pro PyInstaller) a vrátí cestu."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = draw_icon(max(ICO_SIZES))
    image.save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    return path
