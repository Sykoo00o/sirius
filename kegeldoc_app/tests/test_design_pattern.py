"""The simple.life reference pattern as ported for kegeldoc, and its accessibility floor.

Same three concerns as the routine funnel's design suite, rewritten for this palette and markup:

* **Contrast arithmetic** on the token values in `static/kegeldoc.css`, because an eye cannot
  re-check a hex months later and a CTA label is the text that most needs to be readable.
* **The pattern contract**: quiz rows are real inputs inside `.option` labels (so the quiz works
  without JavaScript), the progress bar is a real progressbar that moves, CTAs are the pill.
* **Stylesheet scoping**: the kegel pages and the account pages kept from the routine funnel use
  two stylesheets whose class names overlap, and only the `.kd` scope keeps them apart.

No browser is involved: these read CSS text and rendered HTML.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

_spec = importlib.util.spec_from_file_location(
    "kegeldoc_conftest", FUNNEL_ROOT / "tests" / "conftest.py")
assert _spec and _spec.loader
_flow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_flow)

CSS = (FUNNEL_ROOT / "static" / "kegeldoc.css").read_text(encoding="utf-8")


def token(name: str) -> str:
    match = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert match, f"token --{name} missing from kegeldoc.css"
    return match.group(1)


def luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


#: (foreground token or hex, background token or hex, minimum, what it is)
CONTRACT_PAIRS = [
    ("#ffffff", "teal", 4.5, "white CTA label on the gradient's teal end"),
    ("#ffffff", "green", 4.5, "white CTA label on the gradient's green end"),
    ("#ffffff", "ink", 4.5, "white label on the dark pill and price card"),
    ("ink-60", "paper", 4.5, "secondary text on white"),
    ("ink-60", "band", 4.5, "secondary text on a tinted band"),
    ("ink-60", "canvas", 4.5, "secondary text on the quiz canvas"),
    ("teal", "tint", 4.5, "eyebrow pill text and selected-row accent on its tint"),
    ("teal", "paper", 4.5, "teal links and labels on white"),
    ("ink", "tint", 4.5, "option label on the hovered/selected row"),
]


@pytest.mark.parametrize("fg,bg,minimum,label", CONTRACT_PAIRS)
def test_palette_pairs_clear_wcag_aa(fg: str, bg: str, minimum: float, label: str) -> None:
    fg_hex = fg if fg.startswith("#") else token(fg)
    bg_hex = bg if bg.startswith("#") else token(bg)
    ratio = contrast(fg_hex, bg_hex)
    assert ratio >= minimum, f"{label}: {fg_hex} on {bg_hex} is {ratio:.2f}:1"


def test_coral_is_decoration_only() -> None:
    """Coral fails as text on white; it may appear only as a glow or illustration fill."""
    assert contrast(token("coral"), token("paper")) < 4.5
    assert not re.search(r"color:\s*var\(--coral\)", CSS)


def test_clashing_class_names_are_scoped_to_kegel_pages() -> None:
    rules = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)
    for name in ("band", "display", "eyebrow", "option"):
        bare = re.findall(rf"^\s*\.{name}\b[^{{]*{{", rules, flags=re.MULTILINE)
        assert not bare, f".{name} is unscoped in kegeldoc.css and would leak into app.css pages"


def test_reduced_motion_keeps_reveals_visible() -> None:
    block = CSS[CSS.index("prefers-reduced-motion"):]
    assert "html.js .reveal { opacity: 1" in block


# ---------------------------------------------------------------------------
# Rendered structure
# ---------------------------------------------------------------------------

def test_kegel_pages_skip_the_routine_stylesheet_and_account_pages_keep_it(client) -> None:
    client.get("/")
    landing = client.get("/").text
    assert 'class="kd"' in landing and "/static/app.css" not in landing
    dashboard = client.get("/dashboard").text
    assert "/static/app.css" in dashboard and 'class="kd' not in dashboard


def test_quiz_rows_are_real_inputs_inside_option_labels(client) -> None:
    client.get("/")
    page = client.get("/onboarding/0").text
    rows = re.findall(r'<label class="option[^"]*"[^>]*>(.*?)</label>', page, flags=re.DOTALL)
    assert len(rows) == 2
    for row in rows:
        flat = " ".join(row.split())
        assert '<input type="radio" name="answer"' in flat, flat
        text = re.sub(r"<[^>]+>", " ", row)
        assert text.split(), "an option row rendered blank"


def test_progress_bar_is_a_real_progressbar_that_moves(client) -> None:
    client.get("/")
    seen = []
    for index, pairs in enumerate(_flow.ANSWERS[:6]):
        page = client.get(f"/onboarding/{index}").text
        match = re.search(r'role="progressbar"[^>]*aria-valuenow="(\d+)"', page)
        assert match, f"no progressbar on step {index}"
        seen.append(int(match.group(1)))
        _flow.post(client, f"/onboarding/{index}", pairs)
    assert seen == sorted(seen) and seen[0] == 0 and seen[-1] > 0


def test_information_screens_acknowledge_with_a_hidden_answer(client) -> None:
    client.get("/")
    _flow.walk(client, upto=7)
    page = client.get("/onboarding/7").text
    assert "Study fact" in page and "Source:" in page
    assert '<input type="hidden" name="answer" value="seen">' in page


def test_ctas_use_the_pill_component(client) -> None:
    client.get("/")
    landing = client.get("/").text
    assert landing.count('class="btn-cta"') >= 2
