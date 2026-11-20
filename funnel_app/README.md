# funnel_app: Lane C, Rank 4 (personalised routine generator)

A standalone pre-paywall funnel, kept separate from the B2B apps on its own port (8090), its
own SQLite file and its own deploy. It shares code with nothing that bills.

Phase 1 built the front of it: a landing page, a twelve-question quiz, a genuine week-one
artefact shown *before* the card details. Phase 2 built the money and the exit: the disclosure
screen and Stripe Checkout, a member dashboard with the four-week plan and a stated renewal
date, a one-click cancel that executes before it persuades, the save-offer menu behind it, an
optional exit survey, and the lifecycle emails.

Charges still default off. `FUNNEL_CHARGES_ENABLED=false` means the checkout button records
intent and touches no card, and every date on the account page is labelled an estimate rather
than presented as a renewal date. `SUPERVISOR_ENABLED` is untouched and no ad spend is
connected.

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

## Three metrics the funnel still cannot produce

Named here so nobody reads the funnel as complete. The webhook unblocked what needed one, and
`funnel_save_offer_events` now computes save-offer economics from rows rather than from vibes;
these three still need data the funnel does not hold:

* **Fraud-not-chargeback rate.** Needs the processor's risk verdict on the trial charge, which
  the checkout session does not return.
* **Refund count.** The webhook stores refund events when they arrive, and nothing issues one
  programmatically, so this stays a number looked up in the dashboard until refunds are wired.
* **Prepaid/virtual-card BIN share.** The single most important number for this niche, since a
  virtual-card survivability below roughly 50 percent kills the offer regardless of how the
  funnel performs. The card brand and last four are stored; BIN-level classification needs the
  metadata a payment token carries, and the product decision about blocking prepaid cards needs
  Phase 0's sign-off.
