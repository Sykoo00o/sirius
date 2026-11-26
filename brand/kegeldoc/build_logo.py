"""kegeldoc "Lift k" — final masters (run: python build_logo.py; writes ./logo and ./web/favicon.svg), every stroke outlined (no strokes, no text, no filters).

Geometry is built from four primitives, each emitted as a closed, clockwise sub-path so a single
`<path>` with the default nonzero rule renders their union seamlessly:
  stadium  a round-capped straight stroke        ring  a circle stroke (outer CW, hole CCW)
  arcband  a round-capped circular-arc stroke     dot   a filled circle

Wordmark: lowercase monoline, stroke W=28, x-height 120, ascender 176, round letters overshoot by
1.2 units top and bottom. Symbol: the same k with its upper arm stopped short and its tip floating
one 16-unit gap further up the 45-degree line.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "logo"
WEB = OUT.parent / "web"
OUT.mkdir(parents=True, exist_ok=True)

W = 28
XH = 120
ASC = 176
OVER = 1.2
R = (XH - W) / 2 + OVER          # round letters overshoot baseline and x-height

TANGERINE = "#FF6B2C"
PINK = "#FF3D7F"
PLUM = "#2B1145"
WHITE = "#FFFFFF"
DEEP_T = "#E8551A"
DEEP_P = "#E6266A"


def f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


# ---- primitives -> clockwise sub-paths -------------------------------------------------------

def stadium(x0, y0, x1, y1, h=W / 2):
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy) or 1
    ux, uy = dx / n, dy / n
    nx, ny = uy, -ux                              # left-hand normal in screen space
    a = (x0 + nx * h, y0 + ny * h)
    b = (x1 + nx * h, y1 + ny * h)
    c = (x1 - nx * h, y1 - ny * h)
    d = (x0 - nx * h, y0 - ny * h)
    return (f"M{f(a[0])} {f(a[1])} L{f(b[0])} {f(b[1])} A{f(h)} {f(h)} 0 0 1 {f(c[0])} {f(c[1])} "
            f"L{f(d[0])} {f(d[1])} A{f(h)} {f(h)} 0 0 1 {f(a[0])} {f(a[1])} Z")


def ring(cx, cy, r, h=W / 2):
    ro, ri = r + h, r - h
    return (f"M{f(cx + ro)} {f(cy)} A{f(ro)} {f(ro)} 0 1 1 {f(cx - ro)} {f(cy)} "
            f"A{f(ro)} {f(ro)} 0 1 1 {f(cx + ro)} {f(cy)} Z "
            f"M{f(cx + ri)} {f(cy)} A{f(ri)} {f(ri)} 0 1 0 {f(cx - ri)} {f(cy)} "
            f"A{f(ri)} {f(ri)} 0 1 0 {f(cx + ri)} {f(cy)} Z")


def arcband(cx, cy, r, a0, a1, h=W / 2):
    """Round-capped band along the circle, clockwise on screen from angle a0 to a1."""
    ro, ri = r + h, r - h
    def pt(rad, a):
        return cx + rad * math.cos(math.radians(a)), cy + rad * math.sin(math.radians(a))
    span = (a1 - a0) % 360
    large = 1 if span > 180 else 0
    o0, o1, i0, i1 = pt(ro, a0), pt(ro, a1), pt(ri, a0), pt(ri, a1)
    return (f"M{f(o0[0])} {f(o0[1])} A{f(ro)} {f(ro)} 0 {large} 1 {f(o1[0])} {f(o1[1])} "
            f"A{f(h)} {f(h)} 0 0 1 {f(i1[0])} {f(i1[1])} "
            f"A{f(ri)} {f(ri)} 0 {large} 0 {f(i0[0])} {f(i0[1])} "
            f"A{f(h)} {f(h)} 0 0 1 {f(o0[0])} {f(o0[1])} Z")


def dot(cx, cy, r):
    return (f"M{f(cx + r)} {f(cy)} A{f(r)} {f(r)} 0 1 1 {f(cx - r)} {f(cy)} "
            f"A{f(r)} {f(r)} 0 1 1 {f(cx + r)} {f(cy)} Z")


# ---- glyphs: list of sub-paths, advance -------------------------------------------------------

def centre(x, B):
    return x + XH / 2, B - XH / 2


def g_o(x, B):
    cx, cy = centre(x, B)
    return [ring(cx, cy, R)], XH


def g_c(x, B):
    cx, cy = centre(x, B)
    return [arcband(cx, cy, R, 40, 320)], XH - 8


def g_e(x, B):
    cx, cy = centre(x, B)
    return [stadium(cx - R, cy, cx + R, cy), arcband(cx, cy, R, 40, 360)], XH


def g_d(x, B):
    cx, cy = centre(x, B)
    sx = cx + R - OVER
    return [ring(cx, cy, R), stadium(sx, B - ASC + W / 2, sx, B - W / 2)], XH


def g_g(x, B):
    cx, cy = centre(x, B)
    sx = cx + R - OVER
    return [ring(cx, cy, R), stadium(sx, B - XH + W / 2, sx, B), arcband(cx, B, R - OVER, 0, 150)], XH


def g_l(x, B):
    sx = x + W / 2
    return [stadium(sx, B - ASC + W / 2, sx, B - W / 2)], W


def k_parts(x, B, lifted, h=W / 2):
    """(body sub-paths, dot sub-paths, advance) for the k. Geometry is fixed on the W=28 grid;
    `h` only sets the stroke half-width, so a heavier small-size cut keeps every angle at 45°."""
    g = 14                                     # half of the design stroke: anchors stay put
    sx = x + g
    body = [stadium(sx, B - ASC + g, sx, B - g, h),
            stadium(sx + 22, B - 68, sx + 76, B - g, h)]               # 45-degree leg
    dots = []
    if lifted:
        body.append(stadium(sx, B - 46, sx + 40, B - 86, h))           # arm stops short
        dots.append(dot(sx + 73, B - 119, h + 3))                       # its tip, floated
    else:
        body.append(stadium(sx, B - 46, sx + 60, B - 106, h))
    return body, dots, 76 + 2 * g


def g_k(x, B):
    body, _, adv = k_parts(x, B, lifted=False)
    return body, adv


GLYPHS = {"k": g_k, "e": g_e, "g": g_g, "l": g_l, "d": g_d, "o": g_o, "c": g_c}
LEFT = {"k": "s", "e": "r", "g": "r", "l": "s", "d": "r", "o": "r", "c": "r"}
RIGHT = {"k": "d", "e": "r", "g": "s", "l": "s", "d": "s", "o": "r", "c": "r"}
SPACE = {("s", "s"): 30, ("s", "r"): 24, ("r", "s"): 24, ("r", "r"): 18, ("d", "r"): 12, ("d", "s"): 18}


def wordmark_d(x0=0.0, B=0.0, word="kegeldoc"):
    subs, x, prev = [], x0, None
    for ch in word:
        if prev is not None:
            x += SPACE[(RIGHT[prev], LEFT[ch])]
        p, adv = GLYPHS[ch](x, B)
        subs += p
        x += adv
        prev = ch
    return " ".join(subs), x - x0


WORD_W = wordmark_d()[1]
WORD_TOP, WORD_BOTTOM = -ASC, R + W / 2 - OVER   # g hook reaches ~60 below baseline


def symbol_parts(stroke_boost=0.0):
    """The Lift k at glyph scale, origin at the k's left edge / baseline. `stroke_boost` thickens
    every stroke (and the dot) for the small-size cut without moving any anchor."""
    body, dots, _ = k_parts(0, 0, lifted=True, h=W / 2 + stroke_boost / 2)
    return " ".join(body), " ".join(dots)


# ---- documents ---------------------------------------------------------------------------------

def svg(w, h, inner, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(w)} {f(h)}" width="{f(w)}" '
            f'height="{f(h)}" role="img" aria-labelledby="title"><title id="title">{title}</title>'
            f'{inner}</svg>\n')


def k_inner(body_fill, dot_fill, sc, tx, ty, boost=0.0):
    body, dots = symbol_parts(boost)
    return (f'<g id="symbol" transform="translate({f(tx)} {f(ty)}) scale({f(sc)})">'
            f'<path fill="{body_fill}" d="{body}"/><path fill="{dot_fill}" d="{dots}"/></g>')


# ink box of the lifted k at scale 1: x from 0 to dot right edge, y from -ASC to 0
K_LEFT, K_RIGHT = 0.0, 14 + 73 + 17
K_TOP, K_BOTTOM = -ASC, 0.0


def symbol_svg(body=TANGERINE, dotc=PINK, boost=0.0, sc=1.22, title="kegeldoc symbol"):
    tx = 128 - sc * (K_LEFT + K_RIGHT) / 2
    ty = 124 - sc * (K_TOP + K_BOTTOM) / 2
    return svg(256, 256, k_inner(body, dotc, sc, tx, ty, boost), title)


def horizontal_svg(body=TANGERINE, dotc=PINK, word=PLUM, title="kegeldoc logo"):
    sc_k, sc_w, gap = 1.3, 0.62, 52
    k_w = sc_k * (K_RIGHT - K_LEFT)
    tx_k = 24
    ty_k = 124 - sc_k * (K_TOP + K_BOTTOM) / 2
    wx = tx_k + k_w + gap
    wy = 124 + sc_w * XH / 2 + 4                  # x-height centred on the symbol's optical centre
    total = wx + WORD_W * sc_w + 24
    d, _ = wordmark_d()
    inner = (k_inner(body, dotc, sc_k, tx_k, ty_k) +
             f'<g id="wordmark" transform="translate({f(wx)} {f(wy)}) scale({sc_w})"><path fill="{word}" d="{d}"/></g>')
    return svg(total, 256, inner, title)


def stacked_svg(body=TANGERINE, dotc=PINK, word=PLUM, title="kegeldoc logo, stacked"):
    sc_k, sc_w = 1.3, 0.5
    width = WORD_W * sc_w + 64
    k_w = sc_k * (K_RIGHT - K_LEFT)
    tx_k = (width - k_w) / 2 - 6
    ty_k = 32 + sc_k * ASC
    wy = ty_k + 40 + sc_w * ASC
    d, _ = wordmark_d()
    height = wy + sc_w * WORD_BOTTOM + 32
    inner = (k_inner(body, dotc, sc_k, tx_k, ty_k) +
             f'<g id="wordmark" transform="translate(32 {f(wy)}) scale({sc_w})"><path fill="{word}" d="{d}"/></g>')
    return svg(width, height, inner, title)


def wordmark_svg(word=PLUM, title="kegeldoc wordmark"):
    d, w = wordmark_d(24, ASC + 24)
    return svg(w + 48, ASC + 24 + WORD_BOTTOM + 24, f'<path fill="{word}" d="{d}"/>', title)


def app_icon_svg(title="kegeldoc app icon", boost=0.0, sc=1.05):
    grad = (f'<defs><linearGradient id="tile" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="256" y2="256">'
            f'<stop offset="0" stop-color="{TANGERINE}"/><stop offset="1" stop-color="{PINK}"/></linearGradient></defs>'
            f'<rect width="256" height="256" rx="56" fill="url(#tile)"/>')
    tx = 128 - sc * (K_LEFT + K_RIGHT) / 2
    ty = 124 - sc * (K_TOP + K_BOTTOM) / 2
    return svg(256, 256, grad + k_inner(WHITE, WHITE, sc, tx, ty, boost), title)


def app_icon_flat_svg(title="kegeldoc app icon, flat"):
    sc = 1.05
    tx = 128 - sc * (K_LEFT + K_RIGHT) / 2
    ty = 124 - sc * (K_TOP + K_BOTTOM) / 2
    return svg(256, 256, f'<rect width="256" height="256" rx="56" fill="{PLUM}"/>' +
               k_inner(TANGERINE, PINK, sc, tx, ty), title)


if __name__ == "__main__":
    files = {
        "kegeldoc-symbol.svg": symbol_svg(),
        "kegeldoc-symbol-small.svg": symbol_svg(boost=10, sc=1.22, title="kegeldoc symbol, small sizes"),
        "kegeldoc-symbol-black.svg": symbol_svg("#000000", "#000000"),
        "kegeldoc-horizontal.svg": horizontal_svg(),
        "kegeldoc-horizontal-reversed.svg": horizontal_svg(word=WHITE, title="kegeldoc logo, reversed"),
        "kegeldoc-horizontal-black.svg": horizontal_svg("#000000", "#000000", "#000000"),
        "kegeldoc-stacked.svg": stacked_svg(),
        "kegeldoc-wordmark.svg": wordmark_svg(),
        "kegeldoc-app-icon.svg": app_icon_svg(),
        "kegeldoc-app-icon-flat.svg": app_icon_flat_svg(),
    }
    for name, body in files.items():
        (OUT / name).write_text(body)
    # browser-tab icon: the tile with the heavier small-size k
    WEB.mkdir(exist_ok=True)
    (WEB / "favicon.svg").write_text(app_icon_svg("kegeldoc", boost=12, sc=1.12))
    print("wrote", len(files), "files to", OUT)
