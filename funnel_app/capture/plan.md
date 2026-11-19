# Capture and build plan: simple.life as a reference for the Routine Lab funnel

Lane: **reference build**. simple.life is somebody else's brand (Simple/mindbody, a weight-loss
coaching app). What is reproduced is their *pattern*: section order, grid, spacing rhythm,
type scale, radii and interaction behaviour. Name, logo, palette, typeface role, imagery and
copy stay Routine Lab's own. **No pixel diff is run against simple.life**, because the
acceptance test for a reference build is a side-by-side look confirming that brand, palette,
copy and imagery all differ, plus the funnel's own test suite.

Scope per the operator: the whole funnel. Landing `/`, quiz `/onboarding/*` and `/summary`,
`/preview`, `/checkout`, `/policy/*` and `/health` all move onto one design system.

Captured with Playwright (chromium, `deviceScaleFactor: 1`, `reducedMotion: 'reduce'`) at
390 / 768 / 1440. Artifacts: `survey.home.json`, `survey.survey.json`, `outline.home.json`,
`quiz.states.json`, `sections.source.json`, `source/*.png`.

## Rights note

Reference lane, so nothing is lifted: no simple.life image, font file, logo, illustration or
sentence enters the funnel. Their pages are WordPress + a React quiz; `survey.home.json` shows
a Tailwind v4 token set and Inter, and their quiz option icons are 96px images. Our type stays
the Inter-family stack we already load, our palette stays the warm one in `base.html`, and our
copy stays generated in `funnel_copy.py` (the copy guardrail test fails if a borrowed sentence
carries a banned claim word).

## What the capture says

### Landing (homepage, 12 blocks in page order, `outline.home.json`)

Hero → "Trusted by millions" stat strip → features (3 cards + video) → testimonial → FAQ
(5 items) → coach block → blog strip → community → contacts → footer. Two of those are
marketing-only for us (blog, contacts) and have no equivalent; the order we can actually honour
is **hero → proof strip → what-you-get (cards) → how it works → FAQ → closing CTA → footer**.

Geometry that is worth taking, measured not guessed:

| Thing | Measured | Note |
|---|---|---|
| Section background | `rgba(246,212,255,.2)`, radius **80px** | Every band is one giant tinted panel, inset from the viewport edge |
| Section padding | **152px** vertical desktop, ~64px mobile | Very generous; the rhythm is the look |
| Container | 1392px at 1440 viewport (≈96px gutters) | Wide on desktop, single narrow column on mobile |
| Hero H2 | **72px / 80px / 500**, letter-spacing **−4px**, centred, 600px measure | Tight negative tracking at a big size is the signature |
| Hero sub | 24px / 32px / 400, colour `rgba(50,54,83,.6)` | Body copy is a 60% tint of the ink, not a grey |
| Section heading | 36px / 40px / 500, ls −1.2px, centred inside a 419px card, radius 48px | Feature cards are tall portrait panels |
| Eyebrow | 20px pill, `rgba(137,82,255,.1)` fill, radius 999px, pad 12px 16px | A *pill* label, not a small caps kicker |
| CTA | radius **99px**, pad 12px 32px, 16px/600 white on ink | Hero variant: 64px tall with a coloured drop shadow |
| FAQ | white `.faq-item`, radius 24px (`rounded-2xl`), pad 8px 36px, 5 items | Question row hover shifts text colour; open/close is height only |
| Header | `position: fixed`, 72px, 70% white, no shadow, does **not** shrink on scroll | Height identical at 390/768/1440 in `boxes-*.json` |
| Spacing scale | 8, 12, 16, 20, 24, 32, 40, 48, 80, 152 | 4px-multiple; 8 is the base unit |
| Motion | `transition: opacity 0.5s` on reveal, option states 0.15s ease | No bounce anywhere |

### Quiz (`/survey/main/ob/age`, `quiz.states.json`)

This is the part worth the most to us, because our quiz is the product.

- Whole-viewport **flat panel**, bg `rgb(245,246,248)`; content column **350px** in a 390
  viewport (20px gutters). No card around the question, no page chrome, no footer.
- Option row: **350×96, radius 16px, gap 8px**, white resting fill.
- States, all on `background, border-color` / **0.15s ease** / no transform:
  - resting: `#ffffff`, border transparent
  - hover: `rgb(237,239,252)` (tint), border still transparent
  - selected: same tint **plus** border `rgb(125,139,247)`
  - label: 16px / 20px / **600**
  Tap advances the page itself; there is no visible Continue on single-choice steps.
- An **analysing interstitial** with a ticking percentage ("Analyzing your profile", 89%→97%)
  before the first question, and a persistent results disclaimer: *"Results may vary depending
  on your starting point, goals, and effort. Simple does not provide medical advice."*

Two of their choices we should **not** copy, and the reason:

1. **No visible progress bar during questions.** They show a percentage only on the loading
   screen. For a sunk-effort funnel the bar is the retention mechanic, so ours keeps it.
2. **A mandatory account wall before the artefact.** That is precisely what our funnel exists
   to test the alternative to, so nothing goes behind a sign-up.

Everything else in the quiz pattern is adopted: flat viewport panel, one question, 96px rows at
16px radius with an 8px gap, the hover-tint-then-border-selected progression at 0.15s, and
auto-advance on single-choice tap.

## Design decision: keep the warm palette, take the structure

Our existing tokens (`paper #fdfbf7`, `cream #f6f1e7`, `ink #2b2622`, `ember #e8590c`) stay,
because the brand is ours and the copy guardrail tests assert our disclosure copy in our own
wording. What moves to the reference pattern:

- section bands become big tinted panels with a large radius (24 mobile / 40 desktop, not their
  80px, which reads as a pill behind a 42rem copy column) and 64→96px vertical padding;
- headline scale steps up with tight negative tracking (currently 34px mobile / 52px desktop at
  −2.4px, going to 40px/72px at −1.2/−3px, weight 600 not 500, since Inter at 72px/500 goes
  thin at small sizes);
- eyebrows become pills with a tinted fill;
- quiz options become borderless tinted-fill cards that gain the accent border only when
  selected, at 0.15s, and taller tap targets;
- CTA stays a pill (already 99px-equivalent via `rounded-2xl` → moving to `rounded-full`) with
  the coloured shadow, which the existing `--shadow-cta` already does.

## Pass mark

No pixel target. Acceptance, per width 390/768/1440:

1. `pytest tests -q` still green (159) plus new tests for the pattern: option-card markup,
   progress bar present and accurate, no banned claim introduced by new copy.
2. `capture/shots.js` against the running app produces section shots at all three widths, and
   `boxes-*.json` shows no section collapsing to 0 height (the silent-blank-render class of bug
   this funnel already got bitten by once).
3. Contrast checked against WCAG 2.2 AA for every new foreground/background pair. We do not
   inherit the reference's faults: their `rgba(50,54,83,.6)` body text measures 4.5:1 on white,
   borderline for body size, so our equivalent keeps `muted` (#6f665c on paper, 5.2:1).
4. Keyboard walk of the quiz with visible focus, and the multi-select steps still answerable
   (auto-advance must stay disabled for them).
5. Side-by-side look: our name, palette, typeface, copy and imagery all visibly differ from
   simple.life.

Left out on purpose: their blog strip, contacts block, app-store badges, chat widget, Hotjar /
GA / GTM / kit-ads tracking, cookie banner (we set one functional cookie and no advertising
cookies, so no banner is owed), and every image asset.
