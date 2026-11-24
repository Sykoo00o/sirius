# Reusable Funnel Business Blueprint

The quiz → personalised artefact → disclosed trial → subscription model behind Routine Lab
(`funnel_app/`), written as a vertical-agnostic architecture you can clone into finance,
productivity, B2B ops, wellness or career niches.

**Ground rules for this document**

* Every mechanism points at the file that implements it. Function names are stable; line
  numbers are not quoted because they drift.
* The fork that already exists, `kegeldoc_app/`, is used as measured evidence for what changes
  per vertical (§5.1).
* Where the brief or an older doc describes something the code does not do, a **"Code reality"**
  note says what the code actually does. Plan around the code, not the note.
* No secret values appear here. Companion document for hosting, systemd, tunnels and the
  engine's kill switches: [`docs/AGENTIC_SETUP_BLUEPRINT.md`](AGENTIC_SETUP_BLUEPRINT.md).

### The loop at a glance

```text
 Ad / link ──► GET /               lp_view          (visitor × UTC day)
                 │  signed cookie minted, no signup
                 ▼
            GET/POST /onboarding/{n}   quiz_started  (visitor × UTC day)
                 │  12 typed questions, resume position, answers in SQLite
                 ▼
            GET /summary            quiz_completed   (visitor × UTC day)
                 ▼
            GET /preview            preview_shown    deterministic Week 1 + optional LLM note
                 ▼
            GET /checkout           checkout_viewed  price, renewal, refund, cancel lines on screen
                 │ POST /checkout/start ──► processor Checkout (trial_end set by us, dates owned by them)
                 ▼
            webhooks ──► trial_paid, rebill_*, refund_issued, dispute_created, radar_early_warning
                 ▼
            /dashboard ──► POST /account/cancel (done first) ──► one save-offer menu ──► optional survey
                 ▼
            09:05 UTC export ──► CSV row + ceilings (exit 2 = stop paid traffic, decided by a human)
```

---

## 1. Core Value-Delivery & Conversion Loop (Pre-Paywall Architecture)

### 1.1 Acquisition & anonymous session tracking

**Identity without a signup wall** — `funnel_app/session.py` `SessionCodec`:

* The cookie holds `<visitor_key>.<mac>`: `visitor_key = secrets.token_urlsafe(12)`, `mac` =
  first 32 hex chars of HMAC-SHA256 under `FUNNEL_SESSION_SECRET`. No server-side session table.
* `verify()` uses `hmac.compare_digest`, rejects keys longer than 64 chars, and a bad signature
  simply yields a fresh visitor (never a partial trust).
* Minted in `visitor_middleware` (`main.py`) on first sight, so every route — including
  redirects and error pages — gets the same cookie. Attributes: `max_age` 30 days, `httponly`,
  `samesite=lax`, `path=/`. Cookie name is per app (`rl_vid`, kegeldoc `kd_vid`).
* An empty `FUNNEL_SESSION_SECRET` is tolerated: a random per-process secret is generated,
  logged as a warning, and `/health` shows `secret_ephemeral` — every restart then resets
  in-progress quizzes.
* The cookie is a **convenience key, never a money key**: billing identity is the processor's
  event / subscription id (`session.py` docstring).
* **Code reality:** `cookie_kwargs()` sets no `secure` flag ("Phase 1 runs on http://localhost").
  Behind a TLS tunnel you should add `secure=True` in the clone.

**Conversion denominators** — `main.py` `PAGE_EVENTS`, `count_page_view()`, `track_page_view()`,
`telemetry_middleware`:

| Path (prefix match) | Event | Deduped visitor × UTC day? |
|---|---|---|
| `/` (exact) | `lp_view` | yes |
| `/onboarding/…` | `quiz_started` | yes |
| `/summary` | `quiz_completed` | yes |
| `/preview` | `preview_shown` | no (diagnostic) |
| `/checkout` | `checkout_viewed` | no |

Rules that keep the denominators honest:

1. Counted in middleware **after** the response, only for GETs and only when status < 400; any
   exception is swallowed with a warning (instrumentation never breaks a page).
2. Dedupe key `"{event}:{visitor_key}:{YYYY-MM-DD}"` enforced by the partial unique index
   `trial_event_dedupe_uq` on `idempotency_key` (`telemetry.py`), not by an app-level check.
3. **Redirects count at exactly one step:** `REDIRECT_COUNTS_AT = {"lp_view"}`. A 1xx/3xx response
   on any other step records nothing, so a bounce is not a view, but a deep-link arrival that
   lands on `/` through a redirect is not lost from the top of every ratio.
4. `NON_PAGE_PATHS = {"/checkout/complete", "/checkout/cancelled", "/checkout/start", "/onboarding"}`
   are matched **exactly**: `/onboarding` (the 302 entry hop) is excluded while `/onboarding/3`
   still matches by prefix; the post-card pages are excluded so they cannot inflate the
   checkout denominator.
5. `trial_started` is written at the checkout POST (`main.py`, `billing.py`), keyed
   `trial_started:{visitor}:{day}`; money events are keyed on processor ids (§4.1).

**Code reality:** `funnel_app/PAGE_TRAFFIC.md` predates the current table: it maps
`/artefact…` → `quiz_completed` and `/checkout/preview…` → `trial_started`, and describes the
index as `(event, visitor_key, idempotency_key)`. The code above is authoritative.

### 1.2 Diagnostic quiz & typed profile parsing

**Questions as data** — `funnel_app/quiz.py`:

* `Question(key, section, title, help, kind="single"|"multi", options, allow_note=True, required=True)`;
  `SECTIONS` groups them (Routine Lab: 4 sections, 12 questions; kegeldoc: a branching flow of
  35–39 screens built on the same primitives). Every question must change the output: "A fake
  progress bar is the thing we are not allowed to build."
* `parse_answer(question, posted_single, posted_fields, raw_note)` → `(values, note, error)`:
  single-choice values must be in `option_values`; multi-choice reads the field named after the
  key (plus a fallback `answers` field), **drops unknown options**, de-duplicates in order, and
  `"none"` clears the others. Notes are trimmed to `MAX_NOTE_LENGTH = 280` (key suffix `_note`).
* **Resume position:** `next_unanswered(answers)` returns the first unanswered index; `/onboarding`
  302s there, so a returning visitor resumes instead of restarting. Each step posts to its own
  route (`POST /onboarding/{index}`), and `progress_pct()` is a real ratio of answered questions.
* Answers persist in `responses.sqlite3` (`funnel_app/storage.py`, table `funnel_responses`) keyed
  by `visitor_key`, with `offer_id` (`OFFER_ID`) and status vocabulary `started/paid/cancelled`
  aligned to `engine.trial_events`.

**The only untrusted → typed boundary** — `funnel_app/profile.py`:

* `profile_from_answers(answers)` → `plan_builder.Profile` (frozen dataclass).
* `first()` re-validates each stored value against the question's options and falls back to
  `DEFAULTS` — a partially answered quiz still yields a real plan, because `/preview` is "the worst
  possible place to be strict".
* `INT_FIELDS` cast to integers; `SET_FIELDS` become `frozenset`s.
* `is_complete()`, `missing_questions()` gate `/summary` → `/preview`.

### 1.3 Deterministic pre-paywall artefact engine

`funnel_app/plan_builder.py` — `build_plan(profile) -> Plan` is a **pure function** ("the same
`Profile` always produces the same `Plan`"). Its docstring gives the three reasons it is not an
LLM call: (1) **product truth** — the artefact must be reproducible to survive a deception review;
(2) **copy discipline** — every string is human-written and scanned; (3) **cost and latency** —
no inference cost, no timeout on the screen after five minutes of user effort.

Contract worth copying:

* Vocabulary tables (`SESSION_TYPES`, meal templates, swap rules) + pure helpers (`_schedule`,
  `_scale`, `_meals`, `_metric_rows`).
* `Profile.signature()` = first 8 hex of SHA-256 over the sorted profile JSON, rendered in the plan
  footer and stored on the subscription (`plan_signature`): the visible receipt that the same
  answers give the same plan.
* `Plan` carries `source` (`"builder"` | `"builder+llm"`) and serialises via `to_dict()`.
* **Week 1 renders free**; weeks 2–4 exist in the object and render locked behind the trial
  (`ARTIFACT_FREE` / `ARTIFACT_PAID` in `funnel_copy.py` are the product-truth lists).
* Self-audits on `/health`: `_audit_meal_tables()` / `plan_audit()` and `selfcheck_render()` (an
  offline render of the artefact that must contain its required content and price lines).

**Optional voice layer** — `funnel_app/llm_flavour.py` `coach_note()`:

* Loads `backend/llm_router.py` **by file path** (`spec_from_file_location`), so the funnel runs with
  the engine stopped; `FUNNEL_PREVIEW_MODE=local` disables it entirely.
* `fast` tier, `max_tokens=220`, timeout `FUNNEL_LLM_TIMEOUT_SECONDS` (25 s), output capped at
  `MAX_COACH_CHARS = 420`, niche-specific `SYSTEM_RULES`.
* The output goes through `claims.violations()`. **Any hit, error, timeout or empty reply returns
  `None`** and the page renders the human-written `builder_coach_note()`. The model is never
  edited into compliance; it is rejected.

---

## 2. Compliance, Disclosures & Dynamic Copy-as-Code

### 2.1 Forbidden claims, enforced by tests and at runtime

`funnel_app/claims.py`:

* `FORBIDDEN_TERMS` — words that assert an outcome the product cannot promise. Matched
  case-insensitively on word-initial prefixes (so "guaranteed" is caught under "guarantee" without
  matching inside other words), per line, after stripping HTML.
* `_NEGATED_TERMS` — "not / no / never / without … <term>" (with at most a few filler words) is
  not a claim.
* `DISCLAIMER_EXEMPTIONS` — complete disclaimer sentences exempted by exact equality, so a claim
  cannot be smuggled inside a disclaimer.
* `APPROVED_FRAMINGS` — the product-truth vocabulary the funnel may promise.

Where it runs:

| Check | File |
|---|---|
| Every template's source, module copy, artefact lists, plan-builder strings, disclaimer negations, the four disclosures, configured amounts, "a price change moves every slot at once", refund terms vs spec, trial disclosed before payment everywhere | `funnel_app/tests/test_copy_guardrails.py` (e.g. `test_template_source_is_claim_free`, `test_price_change_moves_every_slot_at_once`, `test_trial_is_disclosed_before_payment_everywhere_it_is_offered`) |
| Live scan of every string `Copy` can emit plus policy pages | `funnel_copy.guardrail_scan()` → `/health` |
| LLM output | `llm_flavour.coach_note()` (reject → fallback) |

### 2.2 One price source, every surface

`funnel_app/funnel_copy.py` + `FunnelSettings` (`funnel_app/config.py`):

* Amounts exist once, in env-backed settings: `FUNNEL_TRIAL_AMOUNT_MINOR`, `FUNNEL_TRIAL_DAYS`,
  `FUNNEL_REBILL_AMOUNT_MINOR`, `FUNNEL_CURRENCY`, `FUNNEL_REFUND_WINDOW_DAYS`,
  `FUNNEL_LITE_AMOUNT_MINOR`, `FUNNEL_PAUSE_DAYS`, `FUNNEL_PREBILLING_NOTICE_DAYS`,
  `FUNNEL_DECLINE_RETRY_DAYS` (plus `FUNNEL_BRAND_NAME`, `FUNNEL_PUBLIC_URL`, `FUNNEL_SUPPORT_EMAIL`).
* `build_copy(settings)` produces an immutable `Copy`; `Copy.slots()` → `claims.Slots`;
  `claims.rendered_disclosures(slots)` assembles the four mandatory sentences: `price_line`
  ("X today for N days, then Y until you cancel"), `renewal_line`, `refund_line`, `cancel_line`,
  each with the screen location it must appear on.
* The **same** `claims.price_line()` is used by checkout and by the receipt email (`mailer.py`),
  so the email cannot word the offer differently from the page the customer agreed to.
* `/policy/{slug}` pages come from `Copy.policy_pages()`, so terms and refund pages carry the
  configured numbers too. The processor's Checkout line items and invoice custom field are built
  from the same amounts (`stripe_billing.create_checkout_session`).
* `disclosure_coverage(copy)` is shown on `/health`; `Copy.for_renewal_date(date, is_real=…)` derives
  a per-subscription variant without mutating shared state.
* Renewal dates carry their provenance: `next_bill_date()` returns `(date, is_real)`; templates
  print "Next billing date" only when it came from the processor and "Estimated next bill"
  otherwise (`templates/dashboard.html`).

---

## 3. Isolated Billing, Retention & Anti-"Roach Motel" Exit Architecture

### 3.1 Isolated payment client

`funnel_app/stripe_billing.py`:

* **Own keys, own account:** `STRIPE_SECRET_KEY_FUNNEL`, `STRIPE_PUBLISHABLE_KEY_FUNNEL`,
  `STRIPE_WEBHOOK_SECRET_FUNNEL`, `FUNNEL_STRIPE_MODE`; `FunnelKeys.load()` validates mode and key
  pairing (details in the companion doc). `client_for(settings)` returns `None` unless a secret
  key is present **and** `FUNNEL_CHARGES_ENABLED` is true — `None` means checkout records intent and
  moves no money.
* **Direct form-encoded calls:** `httpx` + `encode_form()` (Stripe's nested bracket encoding), no
  SDK. Header `Stripe-Version` from `STRIPE_API_VERSION`. GETs may retry; writes are never retried
  by the client ("the idempotency key is the retry mechanism for writes").
* **`funnel-` namespaced idempotency keys on subscription mutations:**
  `funnel-cancel-{sub}`, `funnel-resume-{sub}`, `funnel-pause-{sub}-{resumes_at}`,
  `funnel-resume-collection-{sub}`, `funnel-coupon-{sub}-{coupon}`,
  `funnel-downgrade-{item}-{amount}`. **Code reality:** `create_checkout_session()` posts **without**
  an idempotency key; double-submits are absorbed by Checkout sessions expiring
  (`checkout.session.expired` is tracked) rather than by a key.
* **Processor-authoritative dates:** we set `trial_end` once when creating the session (refusing a
  `trial_end` in the past); after that the renewal date is read from the subscription via
  `period_end_epoch()` (uses `trial_end` while `trialing`, else `current_period_end` /
  `billing_cycle_anchor`, else the item's period end). `main.SUBSCRIPTION_READ` lets
  `/health` warn that dates are still estimates until one subscription has been read this process.
* **Card classification:** `card_of_object()` extracts `funding` (`credit`/`debit`/`prepaid`), country,
  brand and last4 from charges, payment methods, Checkout sessions and Radar's flat
  `network.card_funding`. Unobserved funding is stored as `"unknown"`/`""`, never guessed as consumer,
  because understating the prepaid share hides risk (`telemetry.CARD_FUNDING_TYPES`).
* **Webhook state machine:** `BillingService.handle_event()` (`billing.py`) — risk signals first,
  then expiry, then the subscription lifecycle (`checkout.session.completed/expired`,
  `invoice.paid`, `invoice.payment_failed`, `customer.subscription.*`); unknown types return 200
  `ignored:<type>`.

### 3.2 Lifecycle communication & decline ladder

`funnel_app/mailer.py`, `billing.py`, `billing_store.py`:

* **Transport:** `FUNNEL_SMTP_URL` set → SMTP (`smtp://` STARTTLS / `smtps://` TLS, stdlib); unset →
  an RFC-822 `.eml` in `FUNNEL_EMAIL_OUTBOX`. Plain-text only. `send_all()` reports per-message
  failure and never raises into the customer action.
* **Emails:** `trial_receipt_email` (amount, renewal amount, renewal date, cancel link, reply address),
  `cancellation_email`, `renewal_notice_email`, `decline_email`.
* **Day −2 renewal notice:** `send_due_notices()` finds subscriptions whose period ends within
  `FUNNEL_PREBILLING_NOTICE_DAYS` (default 2), **records the send first** via
  `mark_notification_sent()` (`unique (visitor_key, kind)`), then mails. Triggered by
  `POST /ops/send-notices` from the 09:15 UTC timer.
* **Decline ladder:** on `invoice.payment_failed`, `record_decline()` sends one decline email
  naming the retry date (`FUNNEL_DECLINE_RETRY_DAYS`, default 3), deduped per period
  (`decline:{period_end}` marker). `_track_rebill_decline()` records `rebill_declined` keyed on the
  event id, and `rebill_decline_terminal` once per subscription period when the period end is
  ≥ 3 days old, `attempt_count >= 2`, and the charge has not since settled.
* **Code reality:**
  * The retry itself is performed by the processor's own retry settings. No code configures
    Stripe's retry schedule; "one retry at +3, then stop" is a policy the code **measures and
    tells the customer about**, and you must set the matching retry rule in the processor dashboard.
  * "Silent" prepaid declines are a spec rule (`LANE_C_PHASE0_STEP1-2.md` §1.2: "On decline, accept
    it silently: no four-way retry"). The code does **not** branch on prepaid funding at decline
    time: every decline with an email on file gets the one decline email, and the prepaid share is
    handled as a metric (§4.2), not as a different dunning path.

### 3.3 Action-first cancellation & save-offer engine

`funnel_app/save_offer.py`, `billing.cancel_now()`, `main.py` `/account/*`:

1. `POST /account/cancel` → `billing.cancel_now()` calls `cancel_at_period_end` **first**, then writes
   local state (`cancelled_at_period_end`), then renders `cancel_done.html` with the confirmation.
   If the processor refuses, the row is left alone and the screen says the cancel did not happen
   ("a screen reading Cancelled over a subscription that will still bill is the Roach Motel").
   Repeat clicks return "Already cancelled." and no new offer.
2. Only that confirmation page shows **one** menu (`offer_menu()`), in spec order:
   A `pause` (`FUNNEL_PAUSE_DAYS`, via `pause_collection`), B `downgrade` (the Lite tier,
   `FUNNEL_LITE_AMOUNT_MINOR`, a tier "we would sell cold"), C `discount` (half price for two months,
   **hidden unless `FUNNEL_DISCOUNT_COUPON_ID` is set**), D `keep_cancelled`. Every option is an
   opt-back-in against an already-cancelled baseline; there is no "next offer" concept.
3. `POST /account/cancel/offer` → `accept_offer()`; an unknown or unconfigured value falls through to
   "stay cancelled"; a processor rejection renders "Your cancellation still stands".
4. `GET/POST /account/survey` — optional two-question exit survey; an empty submit is complete.
5. Outcomes use the closed vocabulary `OUTCOME_EVENTS` (`pause_selected`, `downgrade_selected`,
   `discount_selected`, `cancel_confirmed`), stored in `funnel_save_offer_events`; telemetry
   records `cancel_clicked`, `save_offer_selected`, `survey_submitted`.

---

## 4. Unit Economics, Risk Ceilings & Kill-Switch Telemetry

### 4.1 Cohort event ledger (`trial_events`)

Table, indexes and idempotency semantics are in the companion doc §3; `funnel_app/telemetry.py`
`EVENTS` is the closed taxonomy (an unknown name raises):

| Stage | Events |
|---|---|
| Acquisition / activation | `lp_view`, `quiz_started`, `quiz_completed`, `preview_shown`, `checkout_viewed` |
| Conversion | `trial_started`, `trial_paid`, `checkout_expired` |
| Retention / exit | `cancel_clicked`, `save_offer_selected`, `survey_submitted` |
| Rebill | `rebill_succeeded`, `rebill_declined`, `rebill_decline_terminal` |
| Risk | `refund_issued`, `dispute_created`, `radar_early_warning` |

Each row carries `visitor_key`, `offer_id`, `test_variable`, `amount_minor` (captured),
`gross_volume_minor` (authorised, the dispute denominator), `refunded_minor`, `card_funding`,
`card_country`, `reference_id`. Risk events are keyed on the processor event id and mirrored into
`agent_logs`; a dispute's `.updated`/`.closed` is not recounted; Radar warnings count only when
`is_fraud_warning()` says the signal names fraud.

**Flow vs stock** (`scripts/export_lane_c_metrics.py`):

* **Flows** (events in a UTC day) are counted from `trial_events`.
* **Stocks** — `active_subs`, `mrr_minor` — are **gauged** from `funnel_subscriptions` at export time,
  never summed from events ("a stock added on every tick yields 24x the MRR by midnight"). A day
  exported after the fact gets a note that the stock is as of the run, not the day.

### 4.2 Daily CSV & kill-metric engine

`scripts/export_lane_c_metrics.py` (stdlib only, read-only SQLite, one CSV):

* **Rows:** one per `(day, offer_id, test_variable)`; reruns replace that row only. Header = the spec's
  `SPEC_COLUMNS` (from `LANE_C_PHASE0_STEP1-2.md` §3.4) + extras, then `notes`, then the computed
  ratios `METRIC_COLUMNS`.
* **Three column kinds:** `COMPUTED_COLUMNS` (from rows), `TYPED_COLUMNS` (ad spend, tool cost, LLM
  cost, sessions, chargebacks, support…; preserved verbatim, blank rather than 0 when unknown), and
  ratios. Zero denominators are blank, never 0 or 100.
* **Operator notes preserved:** `NOTE_SEPARATOR = " | "`. `build_note()` keeps the operator's text
  first and replaces only the script's warnings after the separator on each rerun.
* **Automatic warnings in `notes`:** mixed variants, prepaid share > 20% ("cut spend early"), unknown
  BIN types, dispute count% ≠ amount%, `LOW VOLUME` when charges < `LOW_VOLUME_BELOW = 250`, undefined
  month-2 retention, stock gauged late, non-numeric cost cells.
* **Ratios:** `trial_paid_pct`, `refund_pct` (refunds / paid conversions), `dispute_cb_pct` and
  `dispute_amt_pct`, `fraud_pct` (fraud-named Radar warnings / charges), `bin_prepaid_share_pct`,
  `month2_retention_pct`, `rebill_decline_rate_pct`, `net_contribution_minor`.
* **Hard ceilings (`CEILINGS`):** `dispute_cb_pct ≤ 1.0`, `fraud_pct ≤ 0.5`. With `--fail-on-ceiling` a
  breach exits **2**; the systemd metrics unit then shows as failed. Exit 0 = exported, 1 = nothing to
  export.
* Spec targets that are **not** automated ceilings (`LANE_C_PHASE0_STEP1-2.md` §1.2, §3.4): trial→paid
  ≥ 3–5%, month-2 retention ≥ 60%, refund rate healthy 5–12% with a 20% hard ceiling, positive net
  contribution, LTV:CAC ≥ 2.5. Read them from the sheet.

**Code reality — who halts the vertical:** nothing in the funnel pauses ads or disables checkout
automatically. The script's job is to make a breach loud (failed unit, stderr, notes); the spec's
daily routine is human: "if any tripwire moved, stop paid traffic **first** and diagnose second".
If you want an automatic halt in a clone, wire the exit code to flipping `FUNNEL_CHARGES_ENABLED`
off or pausing the ad account, and treat that as a new, tested feature.

---

## 5. Vertical-Swapping Playbook (Niche #2 in under a day)

### 5.1 What actually changed in the kegeldoc fork (measured)

`diff funnel_app/X kegeldoc_app/X`, changed lines per file:

| File | Changed lines | Why |
|---|---|---|
| `telemetry.py`, `mailer.py`, `save_offer.py`, `billing_store.py`, `__init__.py` | **0** | Untouched core |
| `session.py`, `storage.py`, `stripe_billing.py` | **2 each** | One constant each: cookie name, `OFFER_ID`, default statement product name |
| `config.py` | 28 | Brand/port defaults, plus kegeldoc's multi-plan and intro-discount settings |
| `billing.py` | 45 | Multi-plan support only (`plans.py`), not niche content |
| `llm_flavour.py` | 53 | `SYSTEM_RULES` and prompt digest for the new niche |
| `funnel_copy.py` | 108 | Niche wording, artefact lists, trust points |
| `claims.py` | 132 | New forbidden vocabulary and exemptions |
| `profile.py` | 144 | New profile fields and defaults |
| `main.py` | 495 | Extra screens (welcome, compatibility, analysing, ready, email) and plan picker |
| `quiz.py` | 689 | New branching question flow |
| `plan_builder.py` | 1264 (1073 → 363 lines) | New deterministic builder |
| `plans.py` (new), templates (+7 new) | — | Optional 1/4/12-week plans with a server-side intro timer; niche screens |

So a straight clone touches **five niche files** (`quiz.py`, `profile.py`, `plan_builder.py`,
`claims.py`, `funnel_copy.py`) plus prompt rules in `llm_flavour.py`, defaults in `config.py`, three
one-line constants, and the templates. `main.py` changes only if you add or rename screens.
`billing.py` changes only if you add a feature (kegeldoc's multi-plan paywall).

### 5.2 File-by-file customisation map

**Change per vertical**

| File | What you rewrite | Keep the contract |
|---|---|---|
| `quiz.py` | `SECTIONS`, `QUESTIONS` (keys, options, help) | `Question` shape, `parse_answer`, `next_unanswered`, `progress_pct`; every question must change the output |
| `profile.py` | `DEFAULTS`, `INT_FIELDS`, `SET_FIELDS`, `profile_from_answers` | Never raises; fallbacks for every field |
| `plan_builder.py` | `Profile` fields, vocabulary tables, `build_plan` | Pure function; `Profile.signature()`; `Plan.to_dict()`; free first unit + locked remainder; `source` field |
| `claims.py` | `FORBIDDEN_TERMS`, `DISCLAIMER_EXEMPTIONS`, `APPROVED_FRAMINGS` | `violations()`, `Slots`, `price_line`, `rendered_disclosures` |
| `funnel_copy.py` | headlines, `ARTIFACT_FREE/PAID`, `TRUST_POINTS`, policy text | Every amount from `FunnelSettings`; `guardrail_scan`, `disclosure_coverage` |
| `llm_flavour.py` | `SYSTEM_RULES`, `_profile_digest` | Reject-to-fallback; `builder_coach_note` |
| `config.py` | `brand_name`, `public_url`, `port`, price defaults | Env-only, "missing means do less", `FUNNEL_CHARGES_ENABLED` default false |
| `session.py` / `storage.py` / `stripe_billing.py` | `COOKIE_NAME` / `OFFER_ID` / statement product name | Nothing else |
| `templates/` | landing, quiz step, preview, policy, dashboard | Disclosure blocks fed from `Copy` |
| `tests/` | plan-builder and copy tests for the new vocabulary | Keep the guardrail, billing, telemetry and posture tests |

**Do not touch** (identical across both apps): `telemetry.py`, `billing_store.py`, `mailer.py`,
`save_offer.py`, `scripts/export_lane_c_metrics.py` (point it at the app with `FUNNEL_APP_DIR`), and,
unless you add a billing feature, `billing.py` and the rest of `stripe_billing.py`.

### 5.3 Launch-day checklist

1. `cp -r funnel_app <new>_app`; change `COOKIE_NAME`, `OFFER_ID`, the statement product name, and
   the `config.py` brand/port defaults (pick a free loopback port).
2. Write the quiz and profile; walk it end to end at 375 px.
3. Write the deterministic builder; add `test_plan_builder` cases proving the same answers give the same
   plan and every option changes something.
4. Write the niche's forbidden-claims list and copy; run `tests/test_copy_guardrails.py` until clean.
   Treat legal assertions from any model as hypotheses (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §2.5).
5. Set prices, trial length, refund window, pause days and Lite price in `.env`; leave the discount
   coupon unset until the processor coupon exists.
6. Copy `deploy/funnel_app/` to `deploy/<new>_app/`, rename units, state dir, hostname and port; for
   metrics add `Environment=FUNNEL_APP_DIR=<repo>/<new>_app`.
7. Run the full suite; open `/health` and require: copy scan clean, all disclosures present, artefact
   self-check passing, no readiness findings except the expected rehearsal ones.
8. Deploy in rehearsal (`FUNNEL_CHARGES_ENABLED=false`), then do the two-card manual test in
   `funnel_app/PHASE3_MANUAL_TEST.md` before any paid traffic.
9. Before arming charges on a processor account shared with another funnel, add per-app tagging of
   checkouts and ignore the other app's events; today both apps would count each other's disputes
   (Execution Log, kegeldoc apply entry).
10. Log the launch in `SIRIUS_MASTER_BLUEPRINT.md` (CLAUDE.md rule 4).

### 5.4 Known gaps to fix once, in the core, before scaling to many verticals

* No auth on `/ops/*`, `/dev/*` and `/health` (companion doc §4.2).
* Cookie lacks `secure` (§1.1).
* No idempotency key on Checkout session creation (§3.1).
* No prepaid-specific decline path; the processor's retry schedule is configured outside the code (§3.2).
* No account/app tagging for shared processor accounts (§5.3 step 9).
* Ceiling breach signals but does not halt spend (§4.2).
* `funnel_app/PAGE_TRAFFIC.md` describes an older page-event table (§1.1).
* Niche-neutral modules are copied, not shared: a fix to `billing.py` must be applied to every fork.
  Once a third vertical exists, consider moving the zero-diff modules into a shared package.
