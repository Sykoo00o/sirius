# kegeldoc — brand guidelines

One page for anyone placing the kegeldoc logo or building a kegeldoc screen.

## The idea

**Lift k.** The k's upper arm lets go and its tip floats one gap further up the same 45° line: the
"squeeze, then lift" of a kegel, carried by the brand's own initial. The rest of the identity is
built from the same parts: one stroke weight, round ends, 45° diagonals and a floating dot.

## Logo files (`logo/`)

| File | Use |
|---|---|
| `kegeldoc-horizontal.svg` | **Primary.** Site header, documents, anywhere with width. |
| `kegeldoc-horizontal-reversed.svg` | On deep plum or photos (white wordmark, colour symbol). |
| `kegeldoc-stacked.svg` | Square-ish spaces: social cards, packaging fronts. |
| `kegeldoc-symbol.svg` | The mark alone, 32 px and up. |
| `kegeldoc-symbol-small.svg` | **Below 32 px**: heavier strokes, same geometry. |
| `kegeldoc-wordmark.svg` | Name only, where the symbol already appears nearby. |
| `kegeldoc-app-icon.svg` / `-flat.svg` | App and social avatar (gradient tile / flat plum tile). |
| `*-black`, `*-white`, `*-mono-<hex>` | One-colour versions for print, stamps, embroidery, foil. |

Web icons are in `web/` (favicon.svg/.ico, PNG sizes, apple-touch, maskable, `site.webmanifest`,
`head-snippet.html`). Regenerate everything with `python build_logo.py` (SVG) and
`python build_icons.py` (PNG, needs Pillow).

All masters are outlined paths: no live text, strokes, filters or embedded images. The wordmark is
custom-drawn lettering, so no font licence is involved.

## Colour — "Pulse"

| Name | HEX | RGB | CMYK (approx.) | Role |
|---|---|---|---|---|
| Tangerine | `#FF6B2C` | 255 107 44 | 0 58 83 0 | The k. Highlights, illustrations. |
| Hot pink | `#FF3D7F` | 255 61 127 | 0 76 50 0 | The floating dot. Accents, sparingly. |
| Deep plum | `#2B1145` | 43 17 69 | 38 75 0 73 | Wordmark, body text, dark backgrounds. |
| Lilac | `#CDBBFF` | 205 187 255 | 20 27 0 0 | Secondary: charts, tags, soft panels. |
| Cream | `#FFF4EC` | 255 244 236 | 0 4 7 0 | Page bands and cards. |

Interface-only shades (never in the logo):

| Name | HEX | Use | Contrast |
|---|---|---|---|
| Button gradient | `#D2430A` → `#DB2463` | Primary buttons, white label | 4.6–4.7 : 1 at either end |
| Bright gradient | `#FF6B2C` → `#FF3D7F` | App tile, progress rings, decoration only | not for text |
| Ember | `#C2410C` | Small accent text, links, eyebrows, selected states | 5.2 : 1 on white, 4.8 : 1 on cream |
| Ember dark | `#9A3412` | Hover on Ember | 7.3 : 1 on white |
| Tint | `#FFEEE4` | Pills and selected rows behind Ember text | Ember on tint 4.6 : 1 |
| Plum 60 | `#6B5A78` | Secondary text | 6.3 : 1 on white |
| Success green | `#0E7A57` | Confirmations only (not a brand colour) | 5.4 : 1 on white |

Rules: tangerine and hot pink are **too light for small text on white** (2.8 : 1 and 3.4 : 1); use
Ember or Deep plum for text. Keep the bright colours to about 10–20 % of any screen so they stay
loud. CMYK values are straight conversions; proof them with the printer, and match a spot colour
against a physical Pantone guide if tangerine must stay vivid on paper.

## Clear space and minimum size

- **Clear space:** the diameter of the floating dot on every side, measured at the size used.
- **Minimum size:** horizontal logo 96 px / 25 mm wide; symbol 16 px (use the small cut below 32 px).

## Backgrounds

- Full colour on white, cream or lilac.
- Reversed lockup on deep plum.
- On tangerine or the gradient: the symbol and wordmark go **all white**.
- On photos: only where the area behind the logo is calm; otherwise use a plum or white panel.

## Typography

Interface type is **Inter** (already used across the site); headings in 700, body in 400. The
wordmark is lettering, not a font: never retype "kegeldoc" in Inter as a logo.

## Don't

- Re-colour the dot to match the k, or the k to match the dot (except one-colour versions).
- Close the gap between the arm and the dot, or move the dot off the 45° line.
- Stretch, rotate, outline, add shadows or glow, or put the logo in a box that isn't the app tile.
- Set the wordmark in another typeface or change its letter spacing.
- Use tangerine text for small copy.

## Notes

- No trademark search has been done. Run one (e.g. USPTO, EUIPO, WIPO) before registering or
  printing at scale.
- Concept rationale and the alternatives considered are in `concepts/`.
