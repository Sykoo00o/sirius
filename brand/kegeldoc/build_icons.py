"""Raster icons for kegeldoc, drawn straight from the Lift k geometry with Pillow.

Why not render the SVG: this machine's only SVG renderer (macOS Quick Look) mis-scales small and
non-square output. The mark is three round-capped bars, a dot and a rounded tile, so it is drawn
here from the same numbers as `build_logo.py`, supersampled 4x and downsampled for smooth edges.

Usage: python build_icons.py [out_dir]     (needs Pillow; default out_dir: ./web)
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "web"
OUT.mkdir(parents=True, exist_ok=True)

TILE_TOP, TILE_BOTTOM = (0xFF, 0x6B, 0x2C), (0xFF, 0x3D, 0x7F)   # tangerine -> hot pink
WHITE = (255, 255, 255, 255)
SS = 4

# Lift k on its own grid (origin: k's left outer edge, baseline). Same numbers as build_logo.py.
ASC = 176
BARS = [((14, -ASC + 14), (14, -14)),        # stem
        ((36, -68), (90, -14)),               # 45-degree leg
        ((14, -46), (54, -86))]               # arm, stopped short
DOT = ((87, -119), 3)                         # centre, extra radius over the half-stroke
INK_W, INK_TOP = 14 + 73 + 17, -ASC


def tile(size: int, radius_frac=56 / 256, full_bleed=False) -> Image.Image:
    s = size * SS
    grad = Image.new("RGBA", (s, s))
    px = grad.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2 * (s - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TILE_TOP, TILE_BOTTOM)) + (255,)
    if full_bleed:
        return grad
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=round(s * radius_frac), fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def draw_k(img: Image.Image, mark_h_frac: float, half_stroke: float = 14, y_bias: float = -0.016):
    """Draw the white Lift k centred on `img`, its height `mark_h_frac` of the canvas."""
    s = img.size[0]
    sc = s * mark_h_frac / ASC
    ox = s / 2 - sc * INK_W / 2
    oy = s / 2 + sc * ASC / 2 + s * y_bias              # optical: a touch above centre
    d = ImageDraw.Draw(img)
    h = half_stroke * sc
    for (x0, y0), (x1, y1) in BARS:
        p0 = (ox + x0 * sc, oy + y0 * sc)
        p1 = (ox + x1 * sc, oy + y1 * sc)
        d.line([p0, p1], fill=WHITE, width=round(2 * h))
        for cx, cy in (p0, p1):
            d.ellipse((cx - h, cy - h, cx + h, cy + h), fill=WHITE)
    (cx, cy), extra = DOT
    cx, cy, r = ox + cx * sc, oy + cy * sc, (half_stroke + extra) * sc
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WHITE)


def icon(size: int, mark=0.62, half_stroke=14, full_bleed=False, opaque=False) -> Image.Image:
    img = tile(size, full_bleed=full_bleed or opaque)
    draw_k(img, mark, half_stroke)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    icon(512).save(OUT / "icon-512.png")
    icon(192).save(OUT / "icon-192.png")
    icon(512, mark=0.46, full_bleed=True).save(OUT / "maskable-512.png")     # safe zone: inner 80%
    icon(180, opaque=True).save(OUT / "apple-touch-icon.png")                  # iOS rounds it itself
    small = {n: icon(n, mark=0.68, half_stroke=19) for n in (16, 32, 48)}      # heavier small cut
    for n, im in small.items():
        im.save(OUT / f"favicon-{n}.png")
    small[48].save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("wrote icons to", OUT)
