# Lane C, Phase 0, Steps 1-2: Niche Shortlist, Offer Spec, Day-1 Tracker

Executes `VALIDATION_AND_AGENT_ASSIST_PLAN.md` §1.C Steps 1-2 and §2.4 for **Lane C (Free-Trial / Negative-Option)**.
Phase 0 means: **no live spend, `SUPERVISOR_ENABLED` off, no automated recurring charges/refunds/ad spend, dry-run
stays on.** Nothing in this document requires a code change to the engine.

Guardrail state assumed and verified in source for this pass:

| Guardrail | Where truth lives | Lane C phase-0 setting |
|---|---|---|
| Dry-run | `engine.settings` key `engine.dry_run`, read by `is_dry_run()` (`finance_agent.py:352-367`); fails closed to `true` on missing row, exception, or unparseable JSON | **Leave absent or `true`.** `ENGINE_DRY_RUN` is logging-only (`:1060`) |
| Supervisor | `SUPERVISOR_ENABLED`, default OFF; two auto-deciding loops double-decide and nothing in code enforces single-writer | **Off.** Standalone `supervisor.py` only *expires*; `supervisor_tick()` (`:1263`) is the one that micro-approves |
| Card envelope | `CARD_DAILY_CAP_MINOR=5000` ($50), `CARD_MONTHLY_CAP_MINOR=50000` ($500), bucketed by `agent_handle` | Ad-spend intents stay **≤ $50/day**, posted to the gate as rehearsal only |
| Processor isolation | `STRIPE_READ_KEY_FUNNEL` vs `STRIPE_READ_KEY_B2B` (`.env.example:118-119`), `STRIPE_WEBHOOK_SECRET_FUNNEL` (`:104`) | Lane C rides **FUNNEL keys only**. `hustle='funnel'` is its enum identity (`schema.sql:129`) |

The corpus's own kill statement drives every decision below: consumers increasingly skip disputing and instead
**cancel the card and report fraud**, and fraud-not-chargeback ratios close merchant accounts faster than any
regulator (`grey_hat_side_hustles.md:85`). So the phase-0 objective is not "is this funnel profitable", it is
"**can we collect recurring revenue from strangers without the bank noticing us**".

---

## 1. Niche & sunk-effort artefact shortlist (`reasoning` tier output)

Selection criteria, in elimination order, from `VALIDATION_AND_AGENT_ASSIST_PLAN.md` §1.C Step 1:

1. The user produces a **real artefact** before the paywall. Effort must be genuine, not a fake progress bar;
   effort is what makes results worth paying for *and* what survives a deception review.
2. **Recurring value afterwards**, so inertia attaches to something real.
3. **Multi-step cancellation is the norm in the niche**, so one-click cancel is a genuine differentiation rather
   than a cost we absorb alone.
4. **COGS small enough that a 20% refund rate still leaves positive contribution.** In this lane refunds are a
   tool (`:94`: honour refunds when a customer produces invoices, cheap versus a dispute).
5. **Virtual-card resistance**: recurring, identity- or history-bound products make one-off card numbers useless,
   because the artefact and continuity are the value, not the discount. This is the counter to `:73`, where a
   one-off CC number makes friction-based retention costless.

Ranking weights the recurring-value test (hard to replicate after cancelling) above raw search volume.

| Rank | Niche | Artefact (pre-paywall, real effort) | Recurring hook | Trial → rebill | Virtual-card risk | Refund/COGS room | Verdict |
|---|---|---|---|---|---|---|---|
| **1** | **AI SEO/AEO content auditor for SMB sites & local businesses** | 60-90 item crawl + scored audit report: technical debt, schema gaps, AI-answer/citation visibility, competitor gap. 15-25 min real setup (site + 5-10 competitors + services) | Monthly re-score + change alerts + fix-list regeneration. Continuity *is* the product | $1 trial (or $14.99 7-day) → **$29-49/mo** | **Low.** Value is the site's tracked history. A cancelled card loses the trend line, so a throwaway card buys nothing | Excellent. Crawl cost is pennies; ~20-25% refund absorbs comfortably | **Strongest for a solo operator in phase 0** |
| **2** | Compliance / accessibility readiness audit (ADA Title III, WCAG 2.2, EU accessibility) for a specific vertical's sites | Automated + manual-checked scan producing a risk report with cited failures and remediation priorities | Continuous monitoring + attestation/proof-of-effort cadence. Verticals fear demand letters | $1-14 → **$39-79/mo** | **Low.** Report provenance and history are the asset | Good; the manual QA pass is the only real labour | Strong, but **claims discipline required** (see trap) |
| **3** | Competitor review / reputation intelligence for local businesses & franchises | Brand-voice + review-health diagnostic: sentiment map, gap analysis, response-coverage score | Alerts on new reviews/ratings shifts + reply drafting. Recurring by nature | $1-14 → **$39-99/mo** | **Low-medium.** History-bound, but a user may churn once the novelty of the dashboard passes | Very good; API + LLM cost per account is small | Solid. Watch platform-API ToS (lane B's hazard leaks in here) |
| **4** | Personalized nutrition / training plan generator | Long questionnaire (40-70 items) + body data → generated 4-week plan with export. Peak sunk-effort in the set | Plan progression, adaptation, new cycles | $2.99 trial → **$19.99-29.99/mo** | **Low.** Identity- and progress-bound | Excellent; digital delivery | Highest conversion, **highest deception/regulatory surface**: body-related outcomes and testimonials drift into medical-efficacy territory, which the corpus explicitly excludes as *illegal*, not grey (`:19`). Needs careful copy |
| **5** | Freelancer / solo consultant earnings-positioning report (rate + pipeline diagnosis) | Rate calculator, pipeline audit, positioning gap report from real inputs | Pipeline tracking, seasonal rate signals, quarterly re-benchmark | $1-14 → **$19-39/mo** | **Medium.** Prosumer discretionary churn is real; less lock-in than a tracked website | Excellent | Viable, but weakest retention hook of the five |

### 1.1 Why rank 1 is the recommended phase-0 lane

Not because it earns most, but because it maximizes **information per dollar of processor risk**:

- B2B-ish buyer with a business card and a bookkeeper. Fewer fraud flags than consumer health niches, and the
  statement descriptor reads as a business expense rather than a surprise.
- Recurring artefact means month-2 retention is *measurable on a real axis* (did the report regenerate, did they
  look) rather than inferred. Month-2 retention ≥ 60% is the gate signal, so the metric must be meaningful.
- A real, inspectable deliverable makes the deception review trivial to pass and chargeback representment writable.
- Cancellation asymmetry is available: most auditors bill annually or make downgrade awkward, so one-click cancel
  is a marketing weapon rather than a pure concession.

**Positioning line, and the honest version of it.** Price is disclosed on the card screen; cancel is one click,
anytime; and the reason to stay is that the tracked history is worth more than the monthly fee. This is the
durability test the corpus converges on (`:306`): the model keeps working *after* the buyer understands exactly
what they are paying for.

### 1.2 Processor-health framing for each flagged risk

- **Virtual-card/prepaid BIN.** Detect at auth, not at failure. Send one pre-billing notice at day −2. On decline,
  accept it **silently**: no four-way retry. Each retry is a fraud complaint in waiting. Track prepaid-BIN share
  as a leading indicator; if it climbs, the cohort is not retainable and spend should come down before the ratio does.
- **Refund room.** Set the refund target before launch: **healthy refund rate 5-12%, hard ceiling 20%**, with the
  standing rule that a refund is cheaper than a dispute and is granted on request when a customer produces
  invoices. Budget COGS at ≤ 10% of revenue so refunds cannot flip contribution negative.
- **Fraud vs dispute discipline.** The two must be counted separately from day 1, since **fraud-not-chargeback is
  the kill metric** (`:85`). See §3.3; today `hustle_metrics` cannot hold it.
- **Save-offers as a processor tool, not just a revenue tool.** A pause or 50%-for-2-months offer converts a
  would-be dispute into a conversation. Anything that keeps a card on file and un-flagged is worth more than the
  discount given away.
- **Descriptor and monitored inbox.** Statement descriptor must match the funnel brand exactly, and the receipt
  must name a monitored address. Most "I never signed up" flags are a person who cannot recognize a charge.

**Two verification items before committing to a niche (do these by hand, ~half a day):**

1. Confirm no *named* platform-policy or regulatory boundary is assumed. The corpus states plainly that it could
   not corroborate some platform-policy bounds from source and reasoned structurally instead (`grey_hat_side_hustles.md:11-19`).
   Anything the reasoning tier asserts about a rule is a **hypothesis**, so for ranks 2 and 4 the specific
   regulatory claim must be read at the primary source before we build.
2. Reproduce two competitors' cancel paths ourselves, with screenshots. Do not accept a traced description.

---

## 2. Day-1 offer, disclosure & anti-Roach-Motel spec

One offer, one funnel, one price point, one variant live at a time (§3).

### 2.1 Trial-to-recurring terms

| Field | Phase-0 value | Reason |
|---|---|---|
| Entry | **$1 for 7 days** (alt: $14.99 7-day) | Paid entry filters freebie-seekers and forces a real card, so the fraud/cohort signal is trustworthy. A free trial measures intent; a paid trial measures willingness |
| Trial length | 7 days | Enough to consume the artefact, short enough that renewal is not a surprise |
| Rebill | **$39/mo**, same card | One price point only. Adding a second arm at validation volume halves every confidence interval |
| Billing anchor | Anniversary of first successful charge, UTC-stored, day-level display | Avoids month-boundary ambiguity in renewal-date copy |
| Trial → paid target | **≥ 3-5%** | Below this, no automation fixes it |
| Grace on decline | One card-updater retry at day **+3**, then stop | Aggressive dunning is ratio poison |
| Pre-billing notice | Day **−2**, email | The single cheapest fraud-flag reducer |
| Renewal reminders | Day −2 only in phase 0 | Two reminders is a lane-B nicety; one is the compliance floor |
| Money-back window on the trial charge | 14 days, no-questions, on request | Converts a would-be dispute into a refund |

### 2.2 Disclosure checklist (screen-level, must all be true before first traffic)

Every item is either present or the funnel does not take money. Copy is drafted by the `fast` tier; each line is
signed off by a human against this list.

| # | Where | Must contain | Failure mode if missing |
|---|---|---|---|
| 1 | Checkout, adjacent to the card field, no modal, no tooltip | "**$1 today for 7 days, then $39/month until you cancel**" | The dossier's core complaint: price framed as a small trial payment, then recurring at $20-40 undisclosed (`:44`) |
| 2 | Checkout | Card-field label states the recurring amount, not just the trial amount | Silent enrolment inside checkout, the Fleava pattern (`:48`) |
| 3 | Checkout | Auto-renewal **on by default is acceptable, but labelled**. No pre-ticked box hidden below the fold | ICPEN's most common dark pattern was making auto-renewal impossible to turn off *during* the purchase flow (`:77`) |
| 4 | Checkout | Link to full terms, and the renewal date | Renewal-date surprise is the top refund reason we want to prevent rather than arbitrate |
| 5 | Receipt / confirmation email | Amount charged, renewal amount, **renewal date**, one-click cancel link, monitored reply address | Receipt is our disclosure record; absence converts our good UX into unauditable UX |
| 6 | Account page | Next bill date and amount, always visible without navigation | Hiding the next date is a soft Roach Motel |
| 7 | Cancel page | Post-login, **one click, zero navigation, zero retention gate before the action** | Roach Motel (`:69`): phone call / mailed letter / "offline only" are outright banned |
| 8 | Any guarantee badge | Badge text must equal the linked policy. If policy says 14 days, badge says 14 days | Badge contradicting a buried no-refunds page is nearer a deceptive-representation claim than a dark pattern (`:89`) |
| 9 | Statement descriptor | Matches funnel brand | Bookkeeper cannot recognize the charge, and small-B disputes are mostly this |
| 10 | Refund policy page | Actual policy, stated once, no weasel clauses | A refund badge that lies is worse than no badge |

**Explicitly prohibited in phase 0:** cancellation fee; cancellation via email-only; "chat to an agent to cancel";
"call us"; any step that occurs *before* the cancel action completes. A post-cancellation 2-question survey is
fine, because the account is already cancelled when the questions appear.

### 2.3 Post-login one-click cancel + save-offer sequence

Order matters. The cancellation must be **executed first**, then the offer is presented, then the survey. Any
sequence where the offer intercepts the click is obstruction, and obstruction is what produces fraud flags rather
than mere churn.

```text
[Account] → "Cancel subscription" (one click)
   ↓ CANCEL IS NOW DONE. State = cancelled_at_end_of_period, shown on screen.
[Save-offer screen]  ← skippable, single "No thanks, I'm cancelled" already true
   A. Pause 30 days          (recommended default; cheapest retention, zero discount)
   B. Downgrade to $19/mo    (reduced scope, honestly described)
   C. 50% off for 2 months    (strongest, most expensive; use only after A/B fail)
   D. No thanks, keep me cancelled
   ↓
[2-question survey] ← optional, never blocking
[Confirmation email] ← cancellation date, final-charge date, reactivation link
```

Hard rules for this sequence, all enforceable as a rule (which is why they survive into automation):

1. **Action precedes persuasion.** The click sets state. Every offer is an *opt-back-in*, not an opt-out.
2. **No dark patterns in the offer screen**: no double negatives, no "Keep saving me money" as the affirmative
   button for staying, no pre-selected C, no hidden D.
3. **One offer per cancel event.** Stacking A then B then C is a maze, and mazes are what the corpus calls Roach Motel.
4. **Downgrade must be a real, reachable state.** If B offers a tier we would not sell cold, it is a lie with
   extra steps.
5. **Refund-on-request remains available after cancellation**, and honouring it when a customer produces invoices
   is the cheaper branch (`:94`).
6. **Instrumentation**: every offer outcome is logged (`pause_selected`, `downgrade_selected`,
   `discount_selected`, `cancel_confirmed`, `survey_reason`) into `agent_logs` as a stable dotted event name, so
   save-offer economics become computable rather than remembered.

Retention is worth this care: the corpus's own operator arithmetic puts **$14.3K/mo of cancellations improved at
~$142K annualized protected from friction alone** (`:59`), with no new customer acquired.

### 2.4 Isolation and rehearsal notes for this spec

- **Processor path.** Lane C uses the FUNNEL merchant keys exclusively; B2B billing never shares this account, so
  a lane-C ratio problem cannot take lane B's MRR with it. Set `STRIPE_WEBHOOK_SECRET_FUNNEL` before relying on
  webhooks: with it unset the handler accepts **unsigned** webhooks and logs a warning (`finance_agent.py:1139-1144`).
  Unsigned inbound is tolerable in phase 0 only because no automated action is wired to it yet.
- **Rehearse the gate, do not trust it.** Post each planned ad-spend intent to `/spend/gate` with a real
  `rationale`, `expected_roi_note`, `source_url`, `hustle='funnel'`, and a stable `idempotency_key`. With
  `engine.dry_run` true, `tier_action()` (`:283-293`) returns for tier 1 `auto_approved` /
  `initiated` / `execute=false`, and `notify_telegram_pending()` (`:867`) still fires the Approve/Deny card on
  **any pending tier-2**, with a ⚠️ marker above the $50 floor. Intent is recorded and visible; nothing executes.
  `dry_run` forcing a non-executing but *correctly-statused* intent is exactly what makes rehearsal meaningful.
- **Still owed before any live card:** `FINANCE_AGENT_URL` / `FINANCE_AGENT_TOKEN` are absent from `.env`, and an
  ungated worker **raises rather than spending**. Correct failure direction; keep it.

---

## 3. Sequential single-variable test plan + Day-1 metrics tracker

### 3.1 Why one variable at a time, stated as arithmetic

The graduation gate needs ≥ 200 paid conversions over 60 days. A trial → paid conversion of 4% means roughly
**5,000 trials** for those 200 conversions. Split 400 trials across 4 arms and each arm has ~30 events with a
95% interval of about ±7 points, while the effect we are hunting is 3-5 points. Multi-arm testing at validation
volume produces a **story**, not a result.

Rules, in priority order:

1. **One changed variable live at a time.** Sequential, not parallel.
2. **Fixed sample per test**, declared before it starts: minimum **n = 250 trials per arm**, or a fixed date
   window, whichever ends *last*. No peeking-and-stopping, which is where "significant" goes to die.
3. **Pre-register the metric that decides it** before traffic arrives. One primary, everything else diagnostic.
4. **Kill on safety before optimizing on revenue.** If dispute + chargeback > 1% or any fraud flag cluster
   appears, the test stops regardless of how good conversion looks.
5. **One test per variable.** Headline change and price change are different tests, run in different windows.
6. **Log a null.** A test that showed nothing is a result and must be written down, or it gets re-run by muscle
   memory in month 3.
7. No agent picks the winner. Agents draft the next hypothesis; a human selects the change.

### 3.2 Test queue, ordered by expected effect per unit of validation time

Variables that move fraud/dispute ratios outrank variables that move conversion, because ratios gate whether we
have a merchant account at all.

| # | Variable | Levels | Primary metric | Diagnostic | n / window |
|---|---|---|---|---|---|
| 0 | **Baseline** (no change) | one offer, one price, one headline | trial→paid, month-2 retention, refund %, dispute+CB %, fraud-flag count | CPC, LP→trial | n ≥ 250 trials | 
| 1 | **Pre-billing notice on/off** (day −2) | off → on | fraud-flag count per 1,000 charges | day-8 refund rate | full baseline cohort vs next | 
| 2 | **Disclosure placement** | below-fold checkbox → adjacent to card field | refund % citing "didn't know" | trial→paid | n ≥ 250/arm | 
| 3 | **Entry price** | $1 → $14.99 | net contribution per trial (not raw conversion) | cohort quality, refund % | n ≥ 250/arm | 
| 4 | **Trial length** | 7 → 14 days | month-2 retention | trial→paid, cash cycle | n ≥ 250/arm | 
| 5 | **Artefact depth** | short report → full report | month-2 retention | time-to-value, trial→paid | n ≥ 250/arm | 
| 6 | **Save-offer set** | A+B+C → A only | saves taken vs confirmed cancels | 60-day revenue retained | n ≥ 200 cancels | 
| 7 | **Headline/angle** (3 variants, run one at a time) | v1 → v2 → v3 | LP → trial-start rate | trial→paid (should be unchanged) | n ≥ 250/arm | 
| 8 | **Descriptor wording** | brand-short → brand+descriptor | fraud-flag count | refund % | 30 days | 

Note the deliberate ordering of tests 1-2 above price and copy: they target the **kill metric**. Tests 3-5 target
retention economics. Only test 7 is the intuitive first move, and it is seventh because it optimizes the top of a
funnel whose bottom we have not yet proven safe.

### 3.3 Tracker: fields, and the honest gaps

**Two storage layers, both required.** `hustle_metrics` is a daily rollup keyed `(day, hustle)` and has **no
per-visitor columns**, so it cannot answer cohort questions; the funnel worker's own docstring says as much. The
per-visitor fact table is `engine.trial_events`, owned by `funnel_worker.py` via inline DDL.

`hustle_metrics` (`schema.sql:448`, PK `(day, hustle)`) — Lane C's row is `hustle='funnel'`:

| Column | Type | Lane C phase-0 use | Writer |
|---|---|---|---|
| `revenue_minor` | bigint | Charges captured that day, cents | `add_metrics` (flow) |
| `cost_minor` | bigint | Ad spend + tooling + infra | `add_metrics` |
| `net_profit_minor` | **generated** | Do **not** write; naming it in an insert raises | DB |
| `gross_volume_minor` | bigint | Total authorized volume = **the ratio denominator** | `set_gauges` |
| `mrr_minor` | bigint | **Stock.** Gauges only | `set_gauges` |
| `active_subscriptions` | bigint/int | **Stock.** | `set_gauges` |
| `new_subscriptions` | int | Flow | `add_metrics` |
| `cancellations` | int | Flow | `add_metrics` |
| `dispute_count` / `chargeback_count` | int | Flow, the 1% ceiling | `add_metrics` |
| `dispute_ratio_pct` | numeric(7,**4**) | Four decimals on purpose: the warning zone sits near 1.0% and rounding to two hides the approach | `set_gauges` |
| `roi_pct` | numeric, nullable | Leave **NULL** on a zero-activity day; 0 would read as "returns nothing" rather than "nothing happened" | `set_gauges` |
| `llm_cost_usd` | numeric(12,4) | Our own agent-assist cost, in USD decimal | `set_gauges` |
| `targets_*`, `clients_onboarded`, `support_tickets`, `*_margin_pct` | | Unused by lane C; `support_tickets` is the exception, worth using for refund/claim volume |

**Flow vs stock, the trap that will bite first.** `add_metrics` (`common.py:382`) accepts only summable columns and
raises `ValueError: not summable, use set_gauges or an explicit ratio write` on anything else. `mrr_minor` and
`active_subscriptions` are **stocks**: adding a stock on every tick yields 24x the MRR by midnight. Ratios cannot
be summed either; store the counts and compute the ratio for the day. Both writers upsert `on conflict (day,
hustle)`, so a manual Day-1 row is safe and a later worker tick merges rather than collides.

**`trial_events` (`funnel_worker.py:343`) — the cohort layer.** Columns: `visitor_key`, `funnel_type`,
`offer_id`, `status ∈ (started, paid, cancelled)`, `occurred_at`, `amount_minor`, `currency`, `sequence_id`,
`idempotency_key` (partial unique index → our idempotency net). Derived from it: trial→paid, cancel-in-trial,
refunds (via `amount_minor` deltas on refund events), **month-2 retention** (paid cohort at day 0 still active at
day 60 — not derivable from any rollup). `offer_id` carries the test-variable label.

Note: `funnel_sequences` has **no** `angle` / `headline` / `ad_spend_minor` columns (verified: its columns are
`visitor_key, funnel_type, offer_id, status, scheduled_at, opt_in_at, trial_days, trial_amount_minor,
rebill_amount_minor, currency, payload, last_error` plus timestamps). Variant identity goes in `payload::jsonb`,
which is free-form — or a one-column migration if we prefer a real column. Test spend goes to `transactions` as
gate-posted intents; daily ad spend rolls into `cost_minor`.

**Three metrics the gate demands that have no home today** (this is the real finding, and Phase 0 is exactly when
to discover it):

| Needed by the gate | Current state | Phase-0 workaround | Automation prerequisite |
|---|---|---|---|
| **Fraud-not-chargeback count** (ceiling ≤ 0.5%) | No column. `risk_flag` enum has `fraud_flag` on `transactions` (`schema.sql:200`), but Stripe exposes `warning_reason=fraudulent` / Radar warnings, which arrive as **warnings, not disputes**, so they land in no table | **Manual daily tally**, CSV, alongside the metrics row | Stripe `radar.early_warning.created` + `issue.created` webhook handling, then a column |
| **Refund count** (the "healthy 5-12%" signal) | `ledger_entry_type` has `refund` and `risk_flag` has `refund_requested`, but `hustle_metrics` has **no refund column** | Count from Stripe / ledger in the CSV | A `refund_count` column, or derive into the rollup |
| **Virtual/prepaid BIN share** (leading indicator per `:73`) | Nowhere | Capture BIN type at auth in the CSV | Persist card-funding type at charge creation |

**ADJUSTMENT to §2.4 of `VALIDATION_AND_AGENT_ASSIST_PLAN.md`, marked because it changes what "populate the
metrics" means:** the doc says agents may *populate* ratios into `hustle_metrics`. True for dispute/chargeback
counts and `dispute_ratio_pct`, **not sufficient for lane C's actual kill metric.** Fraud-not-chargeback is
neither a dispute nor a chargeback, and no table holds it. Until it is captured, the ≤ 0.5% gate is unmeasurable,
which means lane C can pass every visible metric and still be disqualified on the one that closes accounts. Hence
the manual CSV below, starting day 1, and the webhook gap logged as a phase-3 prerequisite.

### 3.4 Day-1 tracking sheet (fills the gaps; 60 columns is too many, so this is the short list)

Start today, even at zero. `day` is UTC. If a field is unknown, write `0` for counts and leave ratios blank;
never invent a rate from a zero denominator.

```
day,date_utc,offer_id,test_variable,live_level,
sessions,lp_views,opt_ins,trial_starts,trials_started,
trial_paid,paid_conversions,cancels_in_trial,rebill_success,rebill_declines_silent,
active_subs,mrr_minor,rev_new,rev_rebill,rev_total_minor,
ad_spend_minor,tool_cost_minor,llm_cost_usd_minor,cost_total_minor,
refunds_count,refund_value_minor,disputes_count,chargebacks_count,fraud_flags_count,
early_warnings_count,radar_reviews_count,customer_service_emails,support_touches,
gross_auth_volume_minor,bin_prepaid_charges,churned_month2_cohort,cohorts_paid_day0_active_day60,
notes
```

Fields that must **never** be eyeballed, only computed:

| Metric | Formula | Threshold / gate |
|---|---|---|
| Trial → paid | `trial_paid / trials_started` | **≥ 3-5%**; below this no automation fixes it |
| Month-2 retention | `cohorts_paid_day0_active_day60 / cohorts paid 60 days ago` (from `trial_events`) | **≥ 60%** — the lane's real PMF signal |
| Dispute+CB ratio | `(disputes_count + chargebacks_count) * 100 / gross_auth_volume_minor` | **≤ 1%**, checked weekly, not monthly |
| Fraud ratio | `fraud_flags_count / all charges` | **≤ 0.5%** — the account-closing one |
| Refund rate | `refunds_count / paid_conversions` | Healthy 5-12%. **Rising refunds with falling disputes is a win**: we are resolving at the cheap stage |
| Net contribution | `(rev_total_minor − cost_total_minor) / trial_paid` | Positive before any spend increase |
| LTV:CAC | `3 × ARPU × month-2 retention proxy / CAC` | **≥ 2.5** on 3-month horizon |
| Prepaid BIN share | `bin_prepaid_charges / trial_paid` | Rising ⇒ cohort unretainable, cut spend early |
| Rebill silent-decline rate | `rebill_declines_silent / rebill_attempts` | Monitor; dunning aggressiveness |

Daily routine (10 minutes, human, no automation): pull Stripe funnel-account numbers → fill one CSV row → run the
three ratio formulas → if any tripwire moved, stop paid traffic **first** and diagnose second.

One caution inherited from the two-stack mess: report from **one** rollup per lane. `engine.hustle_metrics`
(Stack A, written by `add_metrics`/`set_gauges`) and `engine.daily_metrics` (Stack B, written by
`rollup_daily_metrics()`) overlap in columns and nothing reconciles them. Lane C reports from
**`hustle_metrics`**, always.

### 3.5 Bounded agent tasks for Steps 1-2, with tier and sign-off

| Task | Tier | Output | Human sign-off before |
|---|---|---|---|
| Deepen the chosen niche: 5 competitor teardowns incl. cancel path | `reasoning` | Per-competitor table + cancel trace | We re-run each cancel path ourselves with screenshots |
| Draft disclosure lines for checklist items 1-10 | `fast` | Copy per slot | Diff against the checklist; any line vaguer than the spec is rejected |
| Draft save-offer screen A/B/C/D copy, plus post-cancel survey Q1-Q2 | `fast` | Copy set | Confirm the copy never precedes the cancel action |
| Draft pre-billing notice and receipt email (must name renewal amount **and date**) | `reasoning` | Two emails | Verify dates/amounts render from live offer data, not hardcoded |
| Draft trap classification vocabulary for support/refund messages | `fast` | Closed tag set | Tags are frozen before traffic, or classification becomes unfalsifiable |
| Structure the CSV + the three formulas | `fast` | Sheet + formulas | Verify arithmetic by hand once, then trust the sheet |
| Draft the test-queue hypotheses in pre-registration form | `reasoning` | Per-test: change, primary metric, n, stop rule | Human picks which test runs next |
| Regulatory claim check for ranks 2 and 4 | `reasoning` | Structured argument, flagged as hypothesis | **Primary-source read by a human.** No citation, no launch |

Nothing above spends money, sends live outbound, touches billing state, flips `engine.dry_run`, or enables
`SUPERVISOR_ENABLED`. Per the never-delegated table, **refund policy, retry policy and save-offer policy changes
remain human edits**, and the chat agent keeps no writer tools: its writers are exactly `approve_transaction` /
`reject_transaction` (`finance_agent.py:2133-2135`), gated by `can_write` (`:2760`), and we keep them off.

---

## 4. Decisions needed from you before Step 3

1. **Niche.** Recommended rank 1 (AI SEO/AEO auditor): lowest fraud exposure, measurable month-2 retention,
   real inspectable deliverable. Rank 4 converts best but drags the heaviest copy-discipline burden.
2. **Entry price baseline.** $1 vs $14.99 sets the cohort-quality baseline and is test 3, so it must be fixed
   *before* baseline data starts, not changed during it.
3. **Rebill price.** One number. Recommended $39/mo for an audit/monitoring product.
4. **Rebill day.** Day 8 from trial start.
5. **Daily ad budget.** A concrete number ≤ **$50/day**, inside the existing `CARD_DAILY_CAP_MINOR` envelope,
   spent by hand.

Once those are fixed, Step 3 is mechanical: open the funnel merchant account, stand up one funnel, start the CSV,
and begin posting ad-spend intents to the gate as dry-run rehearsal.
