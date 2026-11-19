# UI polish plan: simple.life patterns, applied to Routine Lab

Source: a read-only study of simple.life (19 Nov 2026) using the `clone-website` recon phase
and the `make-interfaces-feel-better` checklist. We take **patterns**, not the palette, the
assets or the copy. Simple's headline copy ("lose weight", "-73lbs", "Lasting results") would
fail `claims.violations`, so no wording crosses over.

## Ground rules

1. **One file per step** (CLAUDE.md rule 1). Each step ends green on
   `pytest tests -q` and the `/health` artefact self-check, then gets its own commit and
   blueprint log entry.
2. **Disclosures never animate in.** Anything carrying a `copy.disclosure(...)` line, the
   footer disclaimer or the rehearsal banner renders at full opacity on first paint. Reveal
   classes go on decorative or explanatory blocks only.
3. **Progressive enhancement.** Hidden-until-revealed state is gated on a `js` class set on
   `<html>` by an inline script. No JS, no IntersectionObserver, or a failed script means
   everything simply shows.
4. **Reduced motion wins.** The existing global `prefers-reduced-motion: reduce` kill switch
   in `base.html` stays, and reveals resolve to visible under it.
5. **Keep the brand.** Warm paper/ember/moss stays. We borrow Simple's *structure* (big rounded
   panels, layered shadows, gradient accent word, staggered fades, a lifting CTA), re-tinted.
6. **No new third-party scripts.** At most one Google Fonts stylesheet. Simple loads ~40
   trackers; the funnel loads none, and that stays true.

## What we borrow, and the tweak we make

| simple.life pattern | Measured there | Routine Lab version |
|---|---|---|
| Section panels | 80px radius desktop, 24px mobile, inset from page edge | 40px desktop / 24px mobile (our column is `max-w-copy`, 80px would look bulbous) |
| Display type | Inter 72/80, weight 500, `-4px` tracking; 28px / `-2px` mobile | Inter, hero 56/60 weight 600 `-2.5px`; mobile 34/38 `-1.2px`. `text-wrap: balance` kept |
| Gradient accent word | blue → violet → teal, `background-clip: text` | ember → amber → rose on one hero phrase ("the week you actually have") |
| Layered float shadow | 5 stacked low-alpha shadows | `--shadow-float` token, used on day cards and panels instead of `border-line` |
| CTA | layered gradient, 3px bottom lip, hover lifts 2px + glow, press flattens | `.btn-primary`: ember gradient + lip, hover `translateY(-2px)`, press `scale(0.96)`; transition lists exact properties, never `all` |
| Scroll reveal | opacity 0→1, 0.5s, staggered 0.2s steps, fade only | opacity + 8px rise, 0.45s `cubic-bezier(0.2,0,0,1)`, 80ms stagger |
| Frosted header | white/70 + `blur(12.5px)` | already close (`bg-paper/90 backdrop-blur`); lower to /75, add hairline shadow instead of border |
| Pill toggle | hard `display:none` cut between tabs | cross-fade version (opacity + 4px), fixing what Simple gets wrong |
| FAQ / accordion | `max-height` 0.3s, plus-to-minus icon via `scaleY(0)` | apply the icon morph to our `<details>` summaries; keep the existing 140ms reveal |
| Testimonial cross-fade | stacked slides, 0.5s opacity | **not borrowed**: we have no testimonials and will not invent any |
| Big stat cards (21M / 18M / 4.7) | 3D numerals, gradient tiles | truthful version only: "12 questions", "about 4 minutes", "week 1 free". No invented counts |

## Steps

### 1. `static/app.css`: motion and surface foundation
- Tokens: `--shadow-float`, `--shadow-cta`, `--ease-out-soft: cubic-bezier(0.2,0,0,1)`,
  `--grad-accent`.
- Utilities: `.text-gradient`, `.panel` (radius 24px → 40px at `sm`), `.btn-primary` with
  hover/active/focus-visible states, `.reveal` / `.reveal.is-visible` gated on `html.js`,
  `.stagger > *` delays via `--i`.
- Reduced-motion block makes `.reveal` visible with no transition.
- *Verify:* tests, `/health`, visual check that nothing changed yet (no template uses them).

### 2. `templates/base.html`: fonts, header, reveal script
- Inter from Google Fonts (400/500/600/700), Tailwind `fontFamily.sans` switched to it.
- Inline 15-line script: add `js` to `<html>`, one `IntersectionObserver`
  (`threshold: 0.15`, `rootMargin: 0px 0px -10% 0px`), unobserve after first reveal.
- Header: translucency and hairline shadow; nothing sticky changes height.
- Fix found during study: `_progress.html` sets `aria-valuemax="{{ total }}"` while
  `aria-valuenow` is a percentage. Done in step 4 alongside the quiz, not here.
- *Verify:* page renders identically with JS disabled; rehearsal banner and footer
  disclaimer visible on first paint.

### 3. `templates/landing.html`: the page that borrows the most
- Hero inside a `.panel` with two blurred ember/rose glow blobs (CSS only, `aria-hidden`).
- Gradient accent on one phrase of `copy.hero_headline`. The headline string stays in
  `funnel_copy.py`; the split is done with a Jinja `replace` on a fixed phrase so the copy scan
  still sees the whole sentence.
- Primary CTA becomes `.btn-primary`. Fine print and the three disclosure lines stay
  directly under it, **not** revealed.
- Truthful stat row (3 tiles) replacing the pill badge.
- "How it works", "What you get" and trust cards get `.reveal` with stagger, and
  shadows instead of borders.
- *Verify:* copy scan, disclosure coverage, screenshots at 1440 and 390 compared against the
  simple.life references for rhythm (not likeness).

### 4. `templates/quiz_step.html` (+ `_progress.html`)
- Option cards: checked state animates border/background over 150ms; press `scale(0.98)`
  (cards are large, so 0.96 reads as a jump).
- Question enter: title → help → options staggered 60ms, opacity + 4px only. Each step is a
  full page load (POST-redirect-GET), so keep it under 300ms total or it starts to feel slow
  across twelve screens.
- Continue button → `.btn-primary`.
- Progress bar: fix `aria-valuemax` to 100; keep the existing width transition.
- *Verify:* `tests/test_funnel_flow.py` (both form encodings), keyboard-only run through all 12.

### 5. `templates/preview.html`: the artefact
- Day cards: `--shadow-float`, staggered reveal down the week.
- Optional pill toggle "Week 1 / Weeks 2–4" with a cross-fade, replacing the stacked locked
  section. Only if the locked content stays fully readable in the toggled state.
- Paid-unlock CTA → `.btn-primary`. Trial and rebill amounts render unrevealed.
- *Verify:* `/health` artefact self-check (`Mon`, `Sun`, `Meal structure`, `Tofu scramble`,
  prices) still passes, since it renders this template offline.

### 6. `templates/summary.html` and `templates/checkout.html`
- Summary: panel and button treatment only.
- Checkout: button treatment only. The price block layout is a compliance decision
  (disclosure adjacent to the card field), so it is not restyled beyond shadows and radius.
- *Verify:* disclosure coverage, checkout POST still reaches `rehearsal_done.html`.

### 7. Final pass
- Run the `make-interfaces-feel-better` checklist over the diff: no `transition: all`,
  concentric radii, 40px hit areas, `tabular-nums` on the progress percentage.
- Screenshots at 1440 / 768 / 390, reduced-motion on and off.
- Blueprint log entry summarising the set.

## Out of scope
Copy changes, price changes, new pages, videos/mascots, and anything touching
`claims.py`, `config.py` or the storage layer.
