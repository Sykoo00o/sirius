# Handover: what the rebuild took from simple.life, and what it did not

The reference build is finished. This is the stage-5 handover, adapted to the lane the operator
picked: **there are no pixel percentages in this file, and that is not a gap.** A pixel diff
answers "is this the same page?", which is the wrong question when the page belongs to somebody
else. The right question is "did we take the pattern and keep the brand", and it is answered
below by the pattern table, the departure list, and a side-by-side look.

`capture/plan.md` is the brief the build worked from. This file is what came back.

## Was the rebuild done?

Yes, across the whole funnel: `/`, `/onboarding/0..11`, `/summary`, `/preview`, `/checkout`,
`/policy/{refund,terms,privacy}`, `/health`. One design system, in `static/app.css`, drawn on by
every template. `pytest tests -q` was green at 190 at handover and `node capture/audit.js` was
clean at 390, 768 and 1440. Phase 2 added the money and the exit to that funnel and is recorded
in its own section at the bottom, because the counts and the walk below are the rebuild's, not the
funnel's current state.

## Which screen carries which band (added by Phase 2)

`data-section` is what `capture/walk_visual.js` and `page_sections.js` measure, so a new
template that skips it joins the audit as an invisible page: measured, boxed as zero-height, and
reported as nothing. Phase 2 added six templates and a `/health` band, all marked, so the
member-side screens get the same 0-height and gutter detection as the quiz. The inventory is
here because grepping nine templates for it is not documentation.

| Template | Bands | Why that split |
|---|---|---|
| `checkout.html`, `checkout_live.html` | `price-block` | One name for both, because it is the disclosure band that is audited, not the rehearsal/live fork. The two templates must measure the same way or a live page can be unmeasured while looking identical. |
| `checkout_done.html` | `trial-confirmed` | The post-payment screen where the renewal date and amount are restated: the screen a dispute starts from. |
| `dashboard.html` | `billing-summary` | Amount and next-bill line, held as one band so a comparison of the two cannot be missed by an audit that walks bands separately. Measured 493px tall at 390px wide. |
| `cancel_done.html` | `cancel-confirmed`, `save-offer` | Two, deliberately, because their order on the page is the compliance property. Action precedes persuasion means the confirmation band precedes the offer band, and the box positions are the cheap way to see that violated. Measured: confirmation at y=88 for 480px, menu starting at y=653, so the menu begins 85px below where the confirmation ends. `member_walk.js` fails the run if that ever inverts. |
| `health.html` | `billing-checks` | Operator-facing, but audited anyway: an unreadable readiness page is how a missing key stays missing. |
| `survey.html` | none | Two questions and a skip link, no band structure. Walked anyway, and clean at 390 and 768. |
| `offer_done.html`, `checkout_error.html` | none | Single-message outcome screens. If Phase 3 grows either past one message, give it bands and it enters the section walk. |

Measured by `capture/member_walk.js`, which is new with Phase 2: a paid row is seeded by
`capture/member_seed.py`, the signed `rl_vid` cookie is rebuilt in Node from the same HMAC the
app uses (truncated to 32 hex characters, which `session.py` does and a full-length MAC gets you
silently logged out), and the screens are then reached by **clicking the funnel's own buttons**
rather than by injecting forms. Clicking matters: the cancel screen's ordering only exists on the
visit that performed the cancellation, and the offer screen only renders for a member who was
just offered something. `capture/audit.js` walks Phase 1's routes, which are all GET-reachable
without a subscription, so it cannot reach any of the above.

Two things the click-through found that a static read of the templates could not: the dashboard's
cancel button leads to a page that is *already* cancelled, so any walk that navigates to
`/account/cancel` by URL measures the "Already cancelled" branch rather than the fresh one, and
`/account/cancel/offer` after a pause renders no bands at all (header and footer only, 672px page),
which is correct for that template but would look like a broken page to a walk that assumed
otherwise.

## Pattern followed

Each row names the thing the capture measured, the number it measured at, and what `app.css`
does now. The reference's figures come from `capture/plan.md`; ours come from the same probe
scripts pointed at localhost.

| Pattern element | Measured on simple.life | Routine Lab now | Where |
|---|---|---|---|
| Page as bands | one giant tinted panel per argument, radius 80px, inset from the viewport edge | 24px radius on phones / 40px at `sm`, inset `px-3`, tinted | `.band`, `.band-tint`, `.band-deep`, `.band-panel` |
| Band air | 152px vertical desktop, ~64px mobile | 96px desktop / 56px mobile, scaled for a 42rem column | `--band-pad-*` |
| Display headline | 72/80 at −4px, weight 500, centred, 600px measure | 38/42→56/60→68/72, −1.4/−2.6/−3.4px, weight 600 | `.display` |
| Band heading | 36/40/−1.2px | same at `sm`, 28/32/−0.9 on phones | `.band-heading` |
| Eyebrow | 999px pill, 10% accent fill, 12/16px padding | 999px pill, 8/14px, tinted fill, moss and neutral variants | `.eyebrow`, `.eyebrow-moss`, `.eyebrow-neutral` |
| CTA | radius 99px, coloured drop shadow | `border-radius: 999px` on `.btn-primary`, `--shadow-cta` | `.btn-primary` |
| Quiz canvas | flat viewport-height panel, 350px column, no card, no footer | flat `#f7f2e9` shell, 30rem column, no card behind the question | `.quiz-shell`, `.quiz-column` |
| Option row | 350×96, radius 16px, 8px gap, 16/20/600 label | `min-height: 64px` + 16px padding = 96px, radius 16px, `space-y-2`, `.option-label` at 16/22/600 | `.option` |
| Option states | rest white → hover tint → selected tint + accent border, all on `background, border-color`, 0.15s ease, no transform | same progression at 150ms; border drawn 2px at rest and recolored, so selection shifts nothing | `.option`, `.option:hover`, `.option:has(input:checked)` |
| Auto-advance | single-choice tap advances, no visible Continue | kept for single-choice only, on a 220ms beat; multi-select still needs Continue | `quiz_step.html` |
| Motion | `opacity 0.5s` reveals, no bounce anywhere | 450ms reveals, 200ms quick variant on the quiz, `--ease-out-soft` | `.reveal`, `.stagger` |
| Header | fixed, 72px, no shadow, identical height at all three widths | sticky 56px, no shadow, same at all three widths | `base.html` |

## Deliberate departures

Five places where the rebuild says no to the reference. Each is a judgement, so each carries its
reason, in the order of how much it cost to hold the line.

1. **The progress bar stays.** simple.life shows a percentage only on its loading screen; during
   the questions there is nothing. For a sunk-effort funnel the bar *is* the retention mechanic,
   so copying that absence would be copying a weakness. It now renders on all twelve steps and
   on `/summary`, with `role="progressbar"` and a truthful `aria-valuenow`, and
   `test_progress_bar_tracks_answers_and_reaches_the_end` asserts it climbs monotonically to 100.
   A progress bar that lies is the specific dishonesty this funnel is not allowed to be.

2. **Nothing goes behind an account wall.** Their funnel ends at a mandatory sign-up before you
   see the artefact. Ours exists to test the opposite shape: the week renders on screen from the
   visitor's own answers, unblurred, before any card details. That is the hypothesis, so it is
   not negotiable as a design reference.

3. **The CTA fill is flat, not a gradient.** This one was forced by arithmetic. Their CTA is a
   solid ink pill; ours was a two-stop ember-to-amber sweep inherited from the old design, and a
   white label on it measured **3.7:1** at the amber end, where the labels are 16px semibold and
   the bar is 4.5:1. Darkening the amber end to keep the sweep leaves the button the colour of a
   warning sign, so `--grad-cta` is now a flat `#cf4a07`: white at 4.53:1 at every pixel, hover
   included. The gradient survives where it is affordable, which is one 38px+ headline phrase.

4. **The headline weight is 600, not 500, and the radius is 40px, not 80px.** At 72px/500 Inter
   goes thin on subpixel-rendered screens, and the headline is the sentence that carries the
   value proposition. An 80px band radius behind a 42rem copy column reads as a pill rather than
   a page. Both are the reference tuned to our measure and our font weight, not a disagreement
   with them.

5. **Warm palette, their structure.** Their `rgba(246,212,255,.2)` lilac bands and `rgba(50,54,83,.6)`
   body text stay theirs. That body-text treatment is worth naming because it is the one thing in
   the capture that reads beautifully and measures badly: a 60% tint of ink on white is 4.5:1,
   exactly the floor, which is borderline for body copy and a fail on any band that is not white.
   Our equivalent is `#6f665c` on paper at 5.2:1. We took the rhythm and left the fault behind.

Everything the reference has that we have nothing to put there stayed out: blog strip, contacts
block, app-store badges, chat widget, Hotjar/GA/GTM/kit-ads, cookie banner (one functional
cookie, no advertising cookies, so no banner is owed).

## States replayed

The capture recorded the option row in three states, and all three were driven on the rebuild in
`capture/quiz_walk.js`, with the transition properties read back off the live element rather than
assumed from the stylesheet:

| State | Reference | Rebuild, measured |
|---|---|---|
| resting | `#ffffff`, border transparent | `#fffdfa`, border 2px transparent |
| hover | `rgb(237,239,252)`, border transparent | `#fdf1e7`, radius 16px, `@media (hover: hover)` gated so a touch device does not stick |
| selected | same tint + `rgb(125,139,247)` border | same tint + `#cf4a07` border |
| timing | 0.15s ease on `background, border-color`, no transform | 150ms ease on `background-color, border-color, box-shadow`, no transform |

Two things the replay looked like defects and were not, recorded here because they cost time
twice:

- The selected state appeared not to fire. It does: `:has(input:checked)` resolves correctly, and
  what looked like a missing selection is auto-advance navigating away in the same tick.
  Forcing `checked` and re-measuring settled it.
- Row height read 62px against the reference's 96px. That one was real, and `min-height: 64px`
  is the fix: 64 content box plus 32px of padding plus the 2px borders.

And one that was a real defect found by accident: my own probe ran with `reducedMotion: 'reduce'`
and the template carried a blanket `* { transition: none !important }`, which cancels the
one-shot entrance reveals, and a cancelled `.reveal` sits at `opacity: 0` forever. Half a landing
page invisible, and only on a machine with reduce on, which is nobody's laptop and everybody's
grandmother. The reduced-motion block is now a policy with two halves, and
`test_reduced_motion_policy_keeps_content_visible` pins it.

## Verification, and the constraint that shaped it

**This model cannot see images.** Three screenshot reads returned "this model cannot see images",
so the skill's usual final step, a side-by-side look, was not available to me and every check
below substitutes measurement for eyes. That is a real limitation of this handover and the reader
should do the side-by-side look themselves; what follows is what measurement could reach.

1. `cd funnel_app && ../backend/.venv/bin/python -m pytest tests -q` → **190 passed** (159 before
   this pass; `tests/test_design_pattern.py` is new and contributes 31).
2. `node capture/audit.js` → **clean** at 390, 768 and 1440: no horizontal document scroll, no
   invisible text, no contrast failures, no dead `label[for]`, no empty headings, no console
   errors, on all nineteen routes. Keyboard focus confirmed: a 3px solid ring after one Tab.
3. Contrast computed for every pair the palette produces, in `CONTRACT_PAIRS`. Four pairs failed
   and were fixed by picking the value off the ratio rather than off the eye: white on the CTA
   fill (3.7:1), `ember-dark` small text on a tint (4.31:1), `moss` on paper (4.02:1), and the
   middle stop of the headline gradient (2.26:1, under even the 3:1 large-text floor).

The two colour layers disagree in both directions, which is the most useful finding here. The
token test waved through a CTA that painted `#cf4a07` to `#f0700a`, because the token it asserted
was not the value the button was filled with. The naive browser pass flagged a compliant headline
at 1.22:1, because `-webkit-text-fill-color: transparent` genuinely makes the computed `color`
meaningless and the honest question for clipped text is its worst gradient stop. So both exist,
and each documents in its own header what it cannot see. Anyone changing a colour wants to run
both.

## Run it

```bash
cd funnel_app
../backend/.venv/bin/python -m uvicorn main:app --port 8091   # any free port; audit.js assumes 8091
../backend/.venv/bin/python -m pytest tests -q                # 190 at handover, 265 now
node capture/audit.js                                         # 390/768/1440 structural + contrast pass
node capture/walk_visual.js capture/rebuild 390               # section boxes, 0-height detector
node capture/shots.js http://127.0.0.1:8091/ capture/rebuild capture/sections.rebuild.json 390 768 1440
```

Against the reference itself, for the side-by-side look this handover could not do:

```bash
node capture/shots.js https://simple.life/ capture/source capture/sections.source.json 390 768 1440
node capture/quiz_walk.js        # their option-row states, live
```

Screenshots are gitignored (`funnel_app/capture/**/*.png`): 22MB of regenerable bitmaps, with no
lane in which anything diffs them. The measurements they came from are tracked: the `*.json`
probes and `capture/plan.md`.

## Open, for whoever looks with eyes

- `.text-gradient` deliberately keeps a 13.62:1 ink fallback for CSS-off, image-off and print. It
  reads as over-engineered until you remember a headline is content and content should survive a
  stylesheet failing to load. Documented rather than optimised away.
- The `-20px` literals in `.quiz-progress` and the `.quiz-shell` gutter are two spellings of one
  number. A `tailwind.config` scale value would be the better form; it is not worth one token for
  a gutter used twice.
- Auto-advance's 220ms beat is a judgement call, not a measurement. If the quiz starts feeling
  laggy on real devices, that number is the first thing to cut.

## Phase 2, added after this handover

The rebuild covered the funnel up to the paywall. Phase 2 is what happens after the card:
`/checkout` in live mode, `/dashboard`, `/account/{plan.txt,plan.pdf,cancel,survey}`,
`/webhooks/stripe` and `/ops/send-notices`, in `stripe_billing.py`, `billing.py`,
`billing_store.py`, `save_offer.py` and `mailer.py`.

Nothing about the design system changed and nothing needed to: the member screens are built from
the same bands, eyebrows, pills and `.btn-primary` as the quiz, so the pattern table above is
still an accurate description of every page in the funnel. The additions are the `data-section`
inventory above, and `capture/member_walk.js`, which is the section walk for routes that need a
paid cookie.

The two properties Phase 2 had to earn are not visual, so they are not in the pattern table, and
they are the two most likely to be undone by a well-meaning later edit:

* **Action precedes persuasion**, held twice: `test_cancel_is_executed_before_any_offer_appears`
  on the HTML, and `member_walk.js` on the measured band positions.
* **A disclosed date is the processor's date.** The dashboard says *Estimated next bill* until a
  real `current_period_end` has been read, and that distinction is copy, a template branch, a
  `/health` finding and a test, because a renewal date is the sentence a chargeback is argued
  over.
