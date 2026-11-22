# DESIGN.md — Floorwise (kegel plans for men and women)

Reference build. The *pattern* comes from simple.life (measurements in
`../funnel_app/capture/plan.md`); the brand, palette, imagery and every sentence are Floorwise's
own. Content context (question topics, flow order) was studied from quiz.kegel-plan.com; none of
its copy, statistics or reviews are reused.

## Visual Theme & Atmosphere
Soft, airy, clinical-but-friendly. Giant rounded tinted bands floating on white, big tight-tracked
headlines, one gradient phrase per headline, pill labels and pill buttons. Calm motion: opacity
fades only, no bounce.

## Color Palette & Roles
| Name | Hex | Role |
|---|---|---|
| Ink | `#1d2b36` | Headlines, body, dark pill CTA |
| Ink-60 | `#56626b` | Secondary text (6.1:1 on white) |
| Paper | `#ffffff` | Page background |
| Band | `#f2f8f7` | Tinted section panels |
| Quiz canvas | `#f4f6f6` | Flat full-viewport quiz background |
| Teal | `#0b6f78` | Primary accent, gradient start |
| Green | `#157a5a` | Gradient end |
| Coral | `#e8735a` | Glow and decorative only (never text) |
| Option hover | `#e7f3f1` | Quiz row hover/selected tint |
| Option border | `#0b6f78` | Quiz row selected border |

Gradient accent: `linear-gradient(90deg, #0b6f78, #157a5a)`; white text on either stop ≥ 5:1.

## Typography Rules
Inter if installed, else the system UI stack (no web-font request, so no third-party IP leak).
| Role | Size / line / weight / tracking |
|---|---|
| Display (hero) | 72/80/600/−3px desktop, 40/44/600/−1.4px mobile |
| Section heading | 56/60/600/−2px desktop, 32/36/600/−1px mobile |
| Card heading | 32/36/600/−1px desktop, 24/28 mobile |
| Lead | 22/32/400 desktop, 18/28 mobile, Ink-60 |
| Body | 16/24/400 |
| Quiz question | 28/34/700/−0.6px |
| Option label | 16/20/600 |

## Component Stylings
- **Band**: background Band, radius 80px desktop / 32px mobile, padding 152px / 64px vertical,
  inset 0 from viewport on desktop container of 1392px max.
- **Eyebrow pill**: 14px/600, teal text on `rgba(11,111,120,.1)`, radius 999px, padding 8px 16px.
- **Primary CTA**: gradient fill, white 18px/600, radius 99px, padding 18px 40px (64px tall),
  `border-bottom: 3px solid rgba(0,0,0,.18)` lip, shadow `0 10px 24px -8px rgba(11,111,120,.55)`;
  hover lifts 2px, active scales .97. Transition `transform, box-shadow` 200ms.
- **Header CTA**: Ink fill, white 15px/600, radius 99px, padding 12px 28px.
- **Feature card**: tall portrait, gradient teal→green, radius 48px, white 32px heading on top,
  a CSS phone mockup rising from the bottom edge.
- **Stat card**: dark teal-ink radial gradient, radius 48px, label top-left, huge gradient numeral.
- **FAQ item**: white, radius 24px, padding 28px 36px; 40px round grey plus button that rotates 45°
  when open; answer height animates.
- **Quiz option row**: white, min-height 72px (96px with an icon), radius 16px, gap 8px, 2px
  transparent border. Hover → Option hover fill. Selected → hover fill + teal border.
  `transition: background-color .15s ease, border-color .15s ease`, no transform.

## Layout Principles
8px base; scale 8, 12, 16, 20, 24, 32, 40, 48, 80, 152. Copy measure 600px centred. Header fixed,
72px, `rgba(255,255,255,.7)` + `backdrop-filter: blur(16px)`, never shrinks. Quiz column 350px on
phones, 480px max.

## Depth & Elevation
Mostly flat. Elevation only on: CTA (coloured shadow), phone mockups
(`0 30px 60px -20px rgba(29,43,54,.35)`), white FAQ rows on tinted bands (no shadow, contrast only).

## Do's and Don'ts
- Do keep one gradient phrase per headline.
- Do fade sections in (`opacity .5s`) when they enter the viewport, gated on `html.js`.
- Don't animate disclosure or price text; it is visible on first paint.
- Don't use stock photos, invented percentages or reviews. Illustrations are CSS/SVG.
- Don't make medical claims; every result screen carries the "not medical advice" line.

## Responsive Behavior
Breakpoints 640 / 1024. Bands lose the side inset under 640 (8px gutter). Feature cards stack.
Touch targets ≥ 48px. Reduced motion: reveals render visible, no transitions.
