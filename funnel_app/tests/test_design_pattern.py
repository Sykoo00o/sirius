"""The simple.life reference pattern, and the accessibility floor that came with it.

Three things a redesign loses first and notices last:

* The **contrast arithmetic**. Four pairs failed WCAG in the pattern pass and were fixed by
  picking the value off the ratio rather than off the eye: the CTA fill (a white label on the
  former amber end of its gradient measured 3.7:1 at 16px), the accent as small text on a tint
  (4.31:1), the moss accent on paper (4.02:1), and the middle stop of the headline gradient
  (2.26:1, under the large-text floor too). An eye cannot re-check a hex four months later.
* The limit of what a palette test can see, which is why a browser pass exists alongside it.
  `CONTRACT_PAIRS` below compares declared token values. It passed a CTA that painted
  `#cf4a07` to `#f0700a` while its *token* read compliant, because the token asserted was not
  the value the button was filled with, and it reports gradient-clipped headline text as white
  because `-webkit-text-fill-color: transparent` genuinely makes the computed `color`
  meaningless. `capture/audit.js` is the counterpart that reads what is actually painted,
  pseudo-element layers and gradient stops included. A colour change wants both.
* The **pattern contract itself**. The quiz option row, the pill CTA and the progress bar are
  the three elements the whole funnel is judged on. Each is asserted structurally here, so
  "tidied up the markup" cannot quietly remove the tap target or the bar.

No browser is involved below: these read CSS text and rendered HTML.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

# The cookie-jar wrapper and the 12-answer walk live in `conftest.py`, shared by every suite
# that drives the funnel. Imported by path rather than relied on as a pytest-injected `conftest`
# name, because this module also needs them at import time. The traps they document
# (`client.cookies` reading empty while the transport holds its own jar; httpx binding a
# positional `data=` onto `files=`) are the kind that, once re-typed, produce a new
# plausible-looking app theory instead of the same solved one.
_spec = importlib.util.spec_from_file_location(
    "funnel_conftest", FUNNEL_ROOT / "tests" / "conftest.py")
assert _spec and _spec.loader
_flow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_flow)

TEMPLATE_DIR = FUNNEL_ROOT / "templates"
CSS_PATH = FUNNEL_ROOT / "static" / "app.css"


def css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def base_html() -> str:
    return (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")


def all_templates() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(TEMPLATE_DIR.glob("*.html"))}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Same isolated app as the flow suite: a shared dirty database makes the progress guard
    redirect, and a redirected quiz step looks like a missing progress bar."""
    monkeypatch.setenv("FUNNEL_DB_PATH", str(tmp_path / "design.sqlite3"))
    monkeypatch.setenv("FUNNEL_BILLING_DB_PATH", str(tmp_path / "design_billing.sqlite3"))
    monkeypatch.setenv("FUNNEL_SESSION_SECRET", "test-secret-value")
    monkeypatch.setenv("FUNNEL_PREVIEW_MODE", "builder")
    # Cleared as a set, and it must include the Phase 2 modules: `main` reads its config at
    # import, so a module left in `sys.modules` keeps the *previous* test's database path, and
    # a design test then renders a page built from a file in somebody else's tmp directory.
    for name in list(sys.modules):
        if name in {"main", "config", "storage", "session", "billing", "billing_store",
                    "mailer", "funnel_copy", "stripe_billing", "save_offer"}:
            del sys.modules[name]
    import main

    return _flow.attach(TestClient(main.app))


# ---------------------------------------------------------------------------
# Contrast: every pair the palette actually produces, computed, not remembered
# ---------------------------------------------------------------------------

def _luminance(hex_colour: str) -> float:
    channels = []
    for index in (0, 2, 4):
        value = int(hex_colour[1 + index:3 + index], 16) / 255
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


#: Foreground, background, minimum ratio, why it matters.
#:
#: 4.5 is WCAG 2.2 AA for text below 24px (or 18.66px bold). Where a pair is only ever large
#: text, 3.0 is the honest bar and is stated as such rather than quietly passed at 4.5.
CONTRACT_PAIRS = [
    # Body copy on every surface the funnel renders.
    ("#2b2622", "#fdfbf7", 4.5, "ink on paper"),
    ("#6f665c", "#fdfbf7", 4.5, "muted on paper"),
    ("#6f665c", "#f7f2e9", 4.5, "muted on the quiz panel"),
    ("#6f665c", "#fbf3ea", 4.5, "muted on band-tint"),
    ("#6f665c", "#f6f1e7", 4.5, "muted on band-deep"),
    ("#6f665c", "#fffdfa", 4.5, "muted on band-panel"),
    # The quiz option row, which is read for four minutes.
    ("#2b2622", "#fffdfa", 4.5, "option label at rest"),
    ("#2b2622", "#fdf1e7", 4.5, "option label when selected"),
    # Pills and card titles.
    ("#a8480a", "#fdece0", 4.5, "eyebrow text on its pill"),
    ("#3d6849", "#eef4ef", 4.5, "moss eyebrow on its pill"),
    ("#6f665c", "#ffffff", 4.5, "card title text on white"),
    ("#3d6849", "#ffffff", 4.5, "moss card title text on white"),
    # The two the pattern pass actually failed and fixed.
    ("#ffffff", "#cf4a07", 4.5, "white label on the ember CTA"),
    ("#bf4708", "#fff1e8", 4.5, "ember-dark small text on the ember tint"),
    ("#bf4708", "#ffffff", 4.5, "ember-dark small text on white"),
    ("#bf4708", "#f6f1e7", 4.5, "ember-dark small text on cream"),
    ("#4a7c59", "#fdfbf7", 4.5, "moss accent on paper"),
]

#: The CTA fill, asserted as the value the button is painted with rather than as a token alias.
#: White on `--grad-cta` is the single ratio the whole funnel's primary action depends on, and
#: it is the one a token-only test waved through once already.
CTA_TOKENS = ("--grad-cta", "--grad-cta-hover")


@pytest.mark.parametrize("token", CTA_TOKENS)
def test_cta_fill_is_a_flat_colour_that_clears_aa(token: str) -> None:
    source = css()
    match = re.search(rf"{re.escape(token)}:\s*([^;]+);", source)
    assert match, f"{token} is no longer defined"
    value = match.group(1).strip()
    assert "#" in value, (
        f"{token} is not a flat colour ({value!r}); a gradient makes the ratio depend on where "
        "the label's pixels land, which is the ambiguity that hid the 3.7:1 CTA")
    hexes = re.findall(r"#[0-9a-fA-F]{6}", value)
    assert hexes, f"{token} declares no hex: {value!r}"
    for colour in hexes:
        ratio = contrast("#ffffff", colour)
        assert ratio >= 4.5, (
            f"{token} = {colour}: a white CTA label measures {ratio:.2f}:1 and the labels are "
            "16px semibold in the quiz and the summary, so 4.5:1 is the bar")


def test_gradient_is_confined_to_large_text() -> None:
    """Decorative colour is cheap at 38px and expensive at 14px.

    `--grad-accent` is legitimate because it only ever clips a display headline (WCAG's
    large-text floor is 3:1, and its worst stop measures 3.13:1 on its band). `.text-gradient`
    additionally keeps a legible `color` as the fallback: a headline is content, and content
    that survives CSS-off, image-off and a print stylesheet at 13:1 is content that cannot be
    silently lost, which is worth more here than a slightly more interesting headline.
    """
    source = css()
    gradient_block = re.search(r"--grad-accent:\s*([^;]+);", source)
    assert gradient_block, "--grad-accent missing"
    for colour in re.findall(r"#[0-9a-fA-F]{6}", gradient_block.group(1)):
        assert contrast(colour, "#fbf3ea") >= 3.0, (
            f"gradient stop {colour} is under 3:1 against its band, and only large text is "
            "allowed to be that quiet")
    rule = re.search(r"\.text-gradient\s*\{([^}]*)\}", source, re.S)
    assert rule and re.search(r"(^|\n)\s*color:\s*#", rule.group(1)), (
        ".text-gradient lost its fallback colour")
    # The utility must not have leaked onto body copy.
    for name, text in all_templates().items():
        for tag in re.findall(r'class="([^"]*text-gradient[^"]*)"', text):
            assert "text-xs" not in tag and "text-sm" not in tag, (
                f"{name}: gradient on small text ({tag!r})")


@pytest.mark.parametrize("foreground,background,minimum,label", CONTRACT_PAIRS)
def test_palette_pairs_clear_wcag_aa(foreground: str, background: str, minimum: float,
                                     label: str) -> None:
    ratio = contrast(foreground, background)
    assert ratio >= minimum, (
        f"{label}: {foreground} on {background} measures {ratio:.2f}:1, needs {minimum}:1. "
        "Pick the colour off this arithmetic, not off how it looks.")


def test_accent_value_is_consistent_across_the_two_files() -> None:
    """A brand colour defined twice is two brand colours.

    Tailwind's `ember` and app.css's `--shadow-cta` / `.option` border are read by different
    renderers (a utility class and a hand-written rule), so they can disagree without anything
    looking obviously wrong: the CTA goes one shade and the selected option border another.
    """
    base = base_html()
    tailwind = re.search(r"ember:\s*\{\s*DEFAULT:\s*'(#\w{6})'", base)
    assert tailwind, "ember.DEFAULT is not defined in the tailwind config block"
    accent = tailwind.group(1)
    assert accent in css(), (
        f"app.css never mentions the accent {accent}, so a component is drawing a different "
        "orange than the utility classes are")


def test_sub_accent_never_becomes_text_again() -> None:
    """#e8590c is the pre-AA ember: 3.58:1 under a white CTA label, so it is illegal as a
    button fill or a text colour. It is *not* illegal everywhere, and the first version of this
    test over-reached by banning it outright -- a 6px decorative bullet is not text, reads fine
    bright, and is the one place the punchier value earns its keep.

    So the rule is scoped to where the value would be read: no text utility, no `color:`, and
    not painted behind a label. Anything that clears that bar is decoration and gets to stay.
    """
    offenders = []
    for name, text in list(all_templates().items()) + [("app.css", css())]:
        for line in text.splitlines():
            stripped = line.strip()
            if "e8590c" not in stripped.lower():
                continue
            if stripped.startswith(("/*", "*", "{#", "<!--")) or "replaces" in stripped:
                continue          # the comment that explains why it was replaced
            reads_as_text = (
                re.search(r"\btext-\[#e8590c\]", stripped)
                or re.search(r"color:\s*#e8590c", stripped, re.I)
                or re.search(r"fill=#e8590c|fill=%23e8590c", stripped, re.I)
                or "--grad-cta" in stripped)
            if reads_as_text:
                offenders.append(f"{name}: {stripped[:76]}")
    assert not offenders, "sub-4.5:1 ember used as text again: " + "; ".join(offenders)


def test_decorative_accent_is_named_not_typed_inline() -> None:
    """The mid-tone ember survives as decoration (a tile bullet), which is fine, but it must
    come from a token whose comment says why it escapes the text rules. An inline
    `bg-[#e8590c]` at a call site is the shape that creeps into a text context unnoticed."""
    for name, text in all_templates().items():
        hits = re.findall(r'(?:bg|text)-\[#(?:e8590c|f08c00)\]', text)
        assert not hits, f"{name}: raw bright-ember utility {hits}, use var(--ember-bright*)"
    source = css()
    for token in ("--ember-bright", "--ember-brighter"):
        assert token in source, f"{token} missing; decorative accents need their own names"


# ---------------------------------------------------------------------------
# The pattern contract, asserted on rendered HTML
# ---------------------------------------------------------------------------

def test_quiz_option_rows_use_the_pattern_class(client) -> None:
    """Every option must be a `.option`, which is the one place the tap-target height, the
    16px radius and the hover/selected progression live. A row that loses the class keeps
    working and just becomes a 20px-high target on a phone."""
    client.get("/")
    for index in (0, 7):        # a single-choice step and a multi-choice one
        page = client.get(f"/onboarding/{index}")
        assert page.status_code == 200
        options = re.findall(r'class="option"', page.text)
        inputs = re.findall(r'<input[^>]+type="(?:radio|checkbox)"', page.text)
        assert options, f"step {index} has no .option rows at all"
        assert len(options) == len(inputs), (
            f"step {index}: {len(inputs)} inputs but {len(options)} .option rows, so some "
            "answers are outside the pattern")


def test_option_rows_never_render_blank(client) -> None:
    """The silent-blank bug class this funnel already met once, aimed at the new markup:
    a label nested inside a wrapper the template forgot to close renders a 200 with an empty
    list, and there is no traceback to find."""
    client.get("/")
    for index in range(12):
        page = client.get(f"/onboarding/{index}")
        assert 'class="option"' in page.text, f"question {index} renders no options"
        assert "option-label" in page.text, f"question {index} renders options with no labels"


def _value_now(html: str) -> int | None:
    match = re.search(r'aria-valuenow="(\d+)"', html)
    return int(match.group(1)) if match else None


def test_progress_bar_renders_on_every_step_as_a_real_progressbar(client) -> None:
    """The reference shows a percentage only on its loading screen. Ours keeps a bar on every
    step by deliberate decision (`capture/plan.md`), so the bar is a contract, not decoration."""
    client.get("/")
    for index in range(12):
        page = client.get(f"/onboarding/{index}")
        assert 'role="progressbar"' in page.text, f"question {index} has no progress bar"
        assert _value_now(page.text) is not None, (
            f"question {index} has a progressbar element with no value to announce")


def test_progress_bar_tracks_answers_and_reaches_the_end(client) -> None:
    """Reading the bar at twelve unanswered GETs proves nothing (it correctly says 0 every
    time), so this answers the quiz and reads the bar as it climbs. The number is the answered
    percentage, which is why the assertion is monotonic and ends at 100 rather than at
    some fixed sequence: a bar that lies about progress is the specific dishonesty the Phase 0
    spec rules out, and a fake progress bar is the thing this funnel is not allowed to be."""
    client.get("/")
    seen = [_value_now(client.get("/onboarding/0").text)]
    for index, pairs in enumerate(_flow.ANSWERS):
        _flow.post(client, f"/onboarding/{index}", pairs)
        nxt = index + 1
        if nxt < 12:
            seen.append(_value_now(client.get(f"/onboarding/{nxt}").text))
    assert seen == sorted(seen), f"progress went backwards: {seen}"
    assert len(set(seen)) == len(seen), f"progress stalled on some step: {seen}"
    summary = client.get("/summary").text
    assert 'role="progressbar"' in summary, "the checkpoint page dropped its progress bar"
    assert _value_now(summary) == 100, f"completed quiz reports {_value_now(summary)}%"


def test_cta_uses_the_pill_component(client) -> None:
    """`.btn-primary` carries `border-radius: 999px`. Per-site `rounded-2xl` overrides crept in
    once already, which turned the pill back into a rectangle on two pages."""
    css_source = css()
    rule = re.search(r"\.btn-primary\s*\{([^}]*)\}", css_source, re.S)
    assert rule and "999px" in rule.group(1), ".btn-primary lost its pill radius"
    for path in ("/", "/onboarding/0"):
        page = client.get(path)
        for anchor in re.findall(r'class="([^"]*btn-primary[^"]*)"', page.text):
            assert "rounded-" not in anchor, (
                f"{path}: CTA carries {anchor!r}, a per-call radius that fights the component")


def test_band_classes_exist_where_landing_uses_them() -> None:
    """`.band` owns radius + padding + tint together. A landing band that keeps `band-tint` but
    loses `band` renders as a flush rectangle with no air, which is the whole look."""
    landing = (TEMPLATE_DIR / "landing.html").read_text(encoding="utf-8")
    for tag in re.findall(r'<section[^>]*class="([^"]*)"', landing):
        if "band-" in tag:
            assert re.search(r"\bband\b", tag), f"tinted band without the `.band` rule: {tag!r}"
    for token in (".band {", ".band-tint", ".band-deep", ".band-panel",
                  ".band-heading", ".eyebrow", ".eyebrow-chip", ".eyebrow-title",
                  ".quiz-shell", ".quiz-progress", ".option ", ".option:has(input:checked)"):
        assert token in css(), f"app.css no longer defines {token.strip()}"


def test_eyebrow_headings_inside_cards_have_a_foreground_colour() -> None:
    """`.eyebrow` sets a background, so an `<h3>` given that class inside a white card is
    white-ish text on a tint that is nearly white. This happened during the pattern pass: the
    card titles needed the pill's *typography*, not its fill, and `background: none` on a
    dedicated variant is the fix. Asserted because the symptom reads as a subtle rendering
    bug, not as a missing rule."""
    for name, text in all_templates().items():
        for tag in re.findall(r'<h[23][^>]*class="([^"]*)"', text):
            if "eyebrow" in tag:
                assert "eyebrow-title" in tag or "text-" in tag, (
                    f"{name}: heading with class {tag!r} has no declared foreground")


def test_reduced_motion_policy_keeps_content_visible() -> None:
    """The trap this replaced: `@media (prefers-reduced-motion: reduce) { * { transition: none
    !important } }` cancels the one-shot entrance reveals, and a cancelled reveal leaves the
    element at `opacity: 0` for good. Half of a landing page invisible, and only on a machine
    with reduce on, which is nobody's laptop and everybody's grandmother."""
    source = css()
    block = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", source, re.S)
    assert block, "no reduced-motion block in app.css"
    body = block.group(1)
    assert re.search(r"\.reveal\s*\{[^}]*opacity:\s*1\s*!important", body), (
        "reduce must force .reveal visible, or content disappears")
    assert "transition-duration" in body or "transition:" in body, (
        "reduce must still neutralise transition durations")
    base = base_html()
    assert "transition: none !important" not in base, (
        "base.html carries a blanket transition kill again; move the policy into app.css "
        "where the .reveal escape hatch can see it")


def test_stylesheet_links_carry_a_content_version(client):
    """A deploy's CSS must not wait out a CDN cache: the link changes when the file does."""
    import hashlib
    import re
    css = Path(__file__).resolve().parents[1] / "static" / "app.css"
    version = hashlib.sha256(css.read_bytes()).hexdigest()[:10]
    for page in ("/", "/health"):
        links = re.findall(r'href="(/static/[\w.-]+\.css[^"]*)"', client.get(page).text)
        assert links and all(f"?v={version}" in link for link in links), (page, links)
