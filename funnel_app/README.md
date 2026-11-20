# funnel_app: Lane C, Rank 4 (personalised routine generator)

A standalone pre-paywall funnel, kept separate from the B2B apps on its own port (8090), its
own SQLite file and its own deploy. It shares code with nothing that bills.

Phase 1 built the front of it: a landing page, a twelve-question quiz, a genuine week-one
artefact shown *before* the card details. Phase 2 built the money and the exit: the disclosure
screen and Stripe Checkout, a member dashboard with the four-week plan and a stated renewal
date, a one-click cancel that executes before it persuades, the save-offer menu behind it, an
optional exit survey, and the lifecycle emails.

Phase 3 built the measurement, and only the measurement: an event ledger with the card facts the
kill metrics need, the webhook handlers that fill it, and a script that turns a UTC day into the
row Phase 0's sheet expects. Nothing in it spends money.

Charges still default off. `FUNNEL_CHARGES_ENABLED=false` means the checkout button records
intent and touches no card, and every date on the account page is labelled an estimate rather
than presented as a renewal date. `SUPERVISOR_ENABLED` is untouched and no ad spend is
connected. `tests/test_telemetry.py` asserts that posture rather than trusting the diff, because
the risk of a measurement phase is a measurement phase that accidentally arms something.

## Run it

```bash
cd funnel_app
cp .env.example .env                     # then read it; one line decides whether money moves
../backend/.venv/bin/python -m uvicorn main:app --port 8090
```

Then open `http://127.0.0.1:8090`. Three pages are worth reading before anything else:

* `/health`, the pre-flight page: the copy scan, the four mandated disclosures, an offline
  render of the artefact, the meal-template audit, live event counts, and a billing-readiness
  section that states which of the funnel keys are set and what that means for a charge.
* `/dev/responses`, the captured rows. Linked from no funnel page on purpose.
* `/policy/refund`, which must match the Phase 0 spec wording, not improvise it.

With charges off, the whole journey is walkable end to end in rehearsal, including the cancel
and save-offer screens: land, quiz, artefact, checkout click, dashboard, cancel, offer, survey.
What rehearsal cannot do is show you a date the processor owns. Until a real subscription
exists, the dashboard says *Estimated next bill*; that wording is the difference between a
forecast and a disclosure, and no rehearsal flag changes it.

Two operations belong to an operator rather than to the funnel:

```bash
# Day-minus-two renewal notices. A route rather than an in-app timer, so that a cron entry,
# a curl and a human on a renewal morning all reach the same idempotent sweep, and so the
# screen that says whether it ran is not the app's own memory.
curl -X POST http://127.0.0.1:8090/ops/send-notices

# Yesterday's row, alerting on a kill metric. The script finds `funnel_app/.env` from its own
# location (`config.ENV_PATHS`), so `cd` is for tidiness; process env still wins over the file,
# which is how a systemd `EnvironmentFile=` or a compose block overrides it. A cron entry that
# supplies neither points at the default database and exports a cheerful empty day, so name the
# path in the unit if the app's database is not at the default.
cd /opt/ghost-x2/funnel_app && python3 ../scripts/export_lane_c_metrics.py \
    --day "$(date -u -d yesterday +%F)" --fail-on-ceiling
```

Mail goes to `data/outbox` as `.eml` files unless `FUNNEL_SMTP_URL` is set, which is what makes
the trial receipt and the cancellation confirmation reviewable by eye before anything is sent
to a customer.

## Tests

```bash
../backend/.venv/bin/python -m pytest tests -q
```

`tests/test_copy_guardrails.py` is the one that protects the licence: no claim may reach the
page, and every disclosure must. `tests/test_funnel_flow.py` walks the funnel twice, once with
each legal form encoding, and asserts on the rendered artefact rather than status codes alone.

`tests/test_billing.py` covers Phase 2 against a Stripe double that subclasses the real client
and replaces only the transport seam, so the request bodies under test (`trial_end`,
`cancel_at_period_end`, `pause_collection`, the price override, the coupon) are still built by
production code. A provider outage is one of the cases it pins, which is why the harness asserts
on every request that the app, `sys.modules`, and the classes the test raises are all *one* copy
of `stripe_billing`: import the same file twice and an exception raised from one copy is not an
instance of the other, so every `except` silently stops matching and a provider refusal turns
into a 500 that reads like an app bug. Read `conftest.py`'s
`assert_app_and_suite_agree_on_stripe_classes` before touching that file's fixtures.

`tests/test_telemetry.py` covers Phase 3: which page views are recorded and how often, the
idempotency of every event under a redelivered webhook, the funding and BIN facts a charge row
must carry, the mode-mismatch rule, the day-+-3 retry stopping condition, and the export script's
arithmetic against hand-built event days. Two harness facts save the next reader an hour:
`TestClient.get` follows redirects while `client.request` does not, so `conftest.attach` pins
`follow_redirects=False` for *every* verb (a per-verb default made a browser-less walk record
`quiz_started` twice, and the app was blamed for a harness asymmetry); and FastAPI resolves a
route's endpoint when it is decorated, so a test that patches `route.endpoint` gets a 200 and
proves nothing. Patch the module-level seam instead, which is why page tracking is
`count_page_view(path)` rather than only `track_page_view(request)`.

## The morning sheet: `scripts/export_lane_c_metrics.py`

One UTC day in, one row out, in Phase 0 §3.4's exact columns and order, so a note written against
the original sheet still lines up.

```bash
cd funnel_app
python3 ../scripts/export_lane_c_metrics.py --day 2026-11-20       # human summary, writes the sheet
python3 ../scripts/export_lane_c_metrics.py --stdout --offline      # CSV to a pipe
python3 ../scripts/export_lane_c_metrics.py --since 2026-10-01      # backfill, no network for history
python3 ../scripts/export_lane_c_metrics.py --fail-on-ceiling       # exit 2 on a kill metric
```

Sheet path: `--csv`, else `$FUNNEL_METRICS_CSV`, else `funnel_app/data/metrics/lane_c_daily.csv`.
The script reads the funnel's own SQLite files with the same `FUNNEL_BILLING_DB_PATH` /
`FUNNEL_DB_PATH` the app uses, opens them read-only so it can run while traffic is live, and needs
no dependencies beyond the standard library. Exit codes: 0 exported, 1 nothing exported (the
database has never run, or predates telemetry), 2 exported with a ceiling breached, which is the
only shape a cron line can alert on.

Three behaviours are worth knowing before trusting a number out of it:

**Flow versus stock.** `active_subs` and `mrr_minor` are gauged from the subscription table, never
accumulated from events, because §3.3's trap is a stock summed per tick yielding 24x MRR by
midnight. A day exported *after* the day itself gauges them as of the export run, and the row says
so in `notes`, because a table with no history cannot say what it held sixty days ago: the number
reads low by whoever had cancelled, in a direction knowable and an amount not.

**Blank, not zero.** A ratio with no denominator is blank. `0.00%` reads as a perfect refund rate
and is how a lane gets told it is healthy on a day with no traffic. Counts we can observe and
found none of are real zeros; derived rates are blank, and net contribution is blank on a day with
no typed costs because contribution against zero cost is the most flattering possible reading of a
loss.

**`notes` is shared.** Your own text comes first and survives a rerun; the script appends its
warnings after a ` | ` separator and regenerates only that tail. Everything after the first
` | ` is machine-written, which is what keeps a rerun from stacking warnings until an operator's
judgement is unreadable. Hand-typed columns (`sessions`, `chargebacks_count`, support volume, the
three cost lines) are preserved verbatim, and a cost cell that cannot be parsed is named and left
out of the total rather than guessed as zero.

Beyond §3.4's columns, the sheet appends the columns the spec's arithmetic needs but never lists
(`checkout_expired`, `charges_observed`, `charges_unknown_bin`, `rebill_declines_total`, and the
nine computed ratios after `notes`), because a figure that exists only in whatever process printed
it is a figure nobody looks at by day 34.

## Telemetry tables

`PAGE_TRAFFIC.md` states which page views are counted, which bounces are not, and why the tracker
is a `(visitor_key, path)` function rather than only a middleware. `PHASE3_MANUAL_TEST.md` is the
safety-posture audit and the two-card walk to run once real keys are in place.

`telemetry.py` writes two tables into the *billing* database, deliberately, so event counts and
subscription counts can never be read a beat apart.

`trial_events` is the cohort fact table: one row per funnel event, with `visitor_key`, `offer_id`,
`test_variable`, UTC `occurred_at`, the money columns, and the card facts (`card_funding`, card
brand, country) where the processor supplied them. Events: `lp_view`, `quiz_started`,
`quiz_completed`, `trial_started`, `trial_paid`, `trial_extend_offered`, `checkout_expired`,
`cancel_clicked`, `save_offer_selected`, `survey_submitted`, `refund_issued`, `dispute_created`,
`radar_early_warning`, `rebill_scheduled`, `rebill_succeeded`, `rebill_declined`,
`rebill_decline_terminal`.

`agent_logs` holds what no column fits: why an event was refused, which payload field a decision
read, the reason a webhook was ignored. It is the audit trail for "the sheet says zero, was that
zero traffic or zero listening".

Idempotency is enforced by partial unique indexes that mirror `funnel_worker.py`'s
`trial_event_dedupe_uq`, one per event that must mean one thing, and the rule it encodes is that
**a row count is not a charge count**: Stripe re-delivers, so a `checkout.session.completed`
delivered twice pays for a card once and must not buy the member a second subscription. Page views
dedupe per visitor per UTC day for the same reason, which is why a visitor refreshing the landing
page fourteen times is one `lp_view`.

Two refusals are recorded rather than silently dropped, and both are the mode rules: an event
whose `livemode` disagrees with the key this app holds belongs to *another merchant account* and
never enters our ceilings, and a live-mode event arriving at a rehearsal app is the same incident
seen from the other side. `livemode` is the vendor's spelling, on the event; a handler that parses
`live_mode` finds nothing, ever, and the guard is silently off. That is a real bug this suite found
and it is worth naming because the symptom was invisible: every cross-account protection looked
like it was working.

## Layout, and why it is split this way

| Module | Owns |
| --- | --- |
| `config.py` | Every `FUNNEL_*` variable. Defaults fail toward doing less. |
| `quiz.py` | The twelve questions, option labels, answer validation, resume position. |
| `profile.py` | The only place untrusted form data becomes typed data. |
| `plan_builder.py` | The deterministic artefact: schedule, meals, metrics, fallback rule. |
| `claims.py` | Forbidden vocabulary and the four mandated disclosure sentences. |
| `funnel_copy.py` | All wording, with amounts interpolated from settings. |
| `llm_flavour.py` | Optional coach-note voice, with the guardrail applied to the output. |
| `storage.py` | Append-only event rows, shaped to match `engine.trial_events`. |
| `session.py` | Signed visitor cookie. Progress identity, never billing identity. |
| `main.py` | Routes, the progress guard, the `/health` self-checks. |
| `stripe_billing.py` | The funnel's own Stripe client: hand-rolled form encoding over `httpx`, no SDK, and the key-isolation checks that refuse a B2B key in the funnel slot. |
| `billing.py` | Every money decision: checkout completion, cancel, offers, notices, webhook state. The only module that writes a subscription. |
| `billing_store.py` | SQLite for subscriptions, notice markers and save-offer outcomes, plus the date and plan-wording helpers they all read. |
| `telemetry.py` | `trial_events` and `agent_logs`, their dedupe indexes, and every `BillingService.track` call. Recording only: no module reads it to decide a charge. |
| `save_offer.py` | The save-offer menu: what may be offered to *this* subscription, and exactly which row columns each accepted option writes. |
| `mailer.py` | Trial receipt, cancellation confirmation, renewal notice, decline notice. SMTP or `.eml` outbox, chosen once from config. |

Two decisions worth knowing before editing:

**The artefact is deterministic.** `plan_builder.py` is not an LLM call, for three reasons: a
pre-paywall artefact must be reproducible to be defensible, every string in it must be
human-written to stay claim-free, and it must not return 503 on the screen where a visitor has
just spent five minutes. The LLM contributes one coach note, loaded from
`backend/llm_router.py` by file path so the funnel deploys without the engine, and that note is
discarded if it trips the copy scan. A sentence about fat loss with the words removed is still a
sentence about fat loss, so notes are rejected rather than scrubbed.

**Copy is code.** The four mandated disclosures are generated in `claims.py` from
`FunnelSettings`, and `disclosure_coverage()` asserts each one renders. Templates iterate that
dict and never write these sentences, so changing a price in `.env` moves every slot in one
operation instead of leaving one stale claim on one page.

**Action precedes persuasion.** `/account/cancel` executes the cancellation and shows the
confirmation *before* any offer renders. The menu below it is one screen, skippable, and offers
at most one choice per cancel event. This ordering is the compliance property the module exists
to hold: a cancel button that opens a negotiation is a Roach Motel, and a test asserts the
confirmation copy's position on the page comes first, not merely that both appear.

**The processor is the authority on money; the row is for display.** `current_period_end` and
`trial_end` are copied from the subscription the client read back, never computed locally,
because a disclosed renewal date has to be the real one. Where a read is impossible (a save
offer that succeeded at the processor and then failed to re-read), the funnel logs and keeps the
offer rather than unselling a change the customer already has.

## What is deliberately not here

No ad pixels, no scheduler, no account system. The quiz has no login because a mandatory account
before the artefact would defeat the thing being tested, which is also why membership is keyed by
the signed visitor cookie: the account page is reachable only from the browser that paid.
Cancellation never deletes a subscription, only `cancel_at_period_end`, so a mistaken click is
reversible and the refund window has an object to refund against. There is no in-process cron:
the notice sweep is a route, deliberately, so that nothing about whether customers were told
about a charge lives in one process's memory.

## Deploy notes

SQLite is right for this stage: one writer, no network hop, and the row shapes already match
`trial_events` so the move to Postgres is a migration rather than a redesign. Turn `secure`
cookies on with TLS once the funnel has a certificate (`session.py`, `cookie_kwargs`); they
default off so local HTTP works. Point the funnel's webhook endpoint at `/webhooks/stripe` and
set `STRIPE_WEBHOOK_SECRET_FUNNEL` in the same operation: with no secret, unsigned events are
still accepted (spec §2.4 allows it while nothing automated acts on them) and `/health` says so
in plain words rather than implying deliveries are verified.

Verifying live needs three things rehearsal cannot supply: `STRIPE_SECRET_KEY_FUNNEL`,
`STRIPE_PUBLISHABLE_KEY_FUNNEL` and `FUNNEL_CHARGES_ENABLED=true`. With those set, `curl
http://127.0.0.1:8090/health` names each variable it is still missing, and a key whose `sk_` or
`pk_` prefix contradicts `FUNNEL_STRIPE_MODE` stops the funnel from serving checkout at all
rather than charging a different merchant account.

## Metrics the funnel can produce now, and the one it still cannot

Phase 3 closed two of the three gaps this section used to name:

* **Fraud-not-chargeback rate.** `radar.early_warning.created` arrives on its own webhook, and the
  0.5% ceiling is computed against observed charges. An early warning *is* the verdict; there is no
  second source, so a Radar-free subscription reports 0% and means "not measured", which the export
  notes when charges exist and warnings never do.
* **Prepaid/virtual-card BIN share.** `payment_method_details.card.funding` is read off the
  checkout session, the invoice payment and the dispute, so the leading indicator is a column
  (`bin_prepaid_share_pct`) rather than a dashboard chore. `charges_unknown_bin` sits beside it on
  purpose: a share computed over only the charges whose BIN resolved understates the warning in
  exactly the direction that keeps traffic running.
* **Refund count.** Still partly manual. The webhook records `charge.refunded` the moment it
  arrives, so refunds issued at the processor's dashboard are counted; nothing in the funnel
  *issues* a refund programmatically, so the refund rate measures what happened, not what the
  product offers. That is the honest reading for a lane whose refund window is a human decision.

The ceiling that cannot be measured from the funnel at all is `n ≥ 250 per arm`: volume is an ad
spend decision, and the export labels a thin day `LOW VOLUME` rather than letting a five-charge
ratio look like a trend.
