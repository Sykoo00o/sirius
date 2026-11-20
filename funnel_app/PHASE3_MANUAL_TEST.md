# Phase 3: safety posture and the two-card manual test

Phase 3 ships instrumentation. Nothing in it is allowed to spend money, and that is a claim that
needs checking rather than asserting, so both halves are below: what the code cannot do, and the
hour of clicking that proves the real thing works once the keys arrive.

## Part 1: nothing is armed

Phase 3 was required to leave `engine.dry_run` and automated spend alone, and to leave
`SUPERVISOR_ENABLED` off. Each of those is a fact about a file, so each is asserted against the
file, in `tests/test_telemetry.py`, rather than remembered:

| Check | Where the truth lives | Result |
| --- | --- | --- |
| No card can move by default | `config.py:190` `_flag("FUNNEL_CHARGES_ENABLED")`, default `false`; `BillingService.client is None` | asserted |
| The reason no money moves is stated, and names a variable an operator can act on | `billing.py` `rehearsal_reason` | asserted |
| No funnel setting defaults a spend switch on | `config.FunnelSettings.from_env()`, every bool whose name hints at spend/supervisor/agent/auto | asserted |
| `SUPERVISOR_ENABLED` still reads the environment with **no default**, so absent means off | `backend/finance_agent.py:256` | asserted against source text |
| `engine.dry_run` is still a settings-table key defaulting to dry-run, and still wins over `ENGINE_DRY_RUN` | `backend/engine/settings.py:74`, `backend/engine/db.py:194` | asserted against source text |

The last row deserves its sentence again, because the two spellings look like a contradiction.
`ENGINE_DRY_RUN` is an environment *default*; `engine.settings['engine.dry_run']` is the live
switch and it wins, which `engine/config.py`'s own startup warning says out loud. §2.4's point is
that the row an agent can write is the switch that matters, so what a test must protect is that
both readers of that row still default to `True`. A test that grepped for a nonexistent
`backend/engine.py` and skipped was green while guarding nothing; it now reads where the key
actually is.

Three more things Phase 3 does not do, checked the same way:

* **No scheduler, no timer, no new process.** The funnel has no in-app cron and Phase 3 added
  none. The notice sweep is still a route a human or a cron line calls.
* **Nothing new leaves the host.** `telemetry.py` makes no network call of any kind: it writes two
  SQLite tables and returns. The only outbound request in the whole phase is the export script's
  best-effort Stripe cross-check, which is `GET`-only, is skipped by `--offline`, is skipped for
  every historical day, and sends the key as a base64 `Authorization: Basic` header rather than in
  a URL, so it cannot land in shell history or a proxy log. It has no write endpoint to reach.
* **Telemetry never decides anything.** `telemetry.py` has no reader that feeds a charge decision.
  The database can be emptied and the funnel behaves identically, which is the property that makes
  it safe to have touched `billing.py` at all.

Full suite at the point of writing: **339 passed, 0 failed, 0 skipped** (`tests/`, 11s), including
the 74 Phase 3 tests and the whole Phase 1/2 baseline.

```bash
cd funnel_app
uv run --with pytest --with fastapi --with httpx --with anyio --with jinja2 \
   --with python-multipart --no-project python -m pytest tests -q
```

## Part 2: the two-card manual test

Rehearsal cannot show you a date the processor owns, a BIN the card networks classify, or a webhook
Stripe actually signed. This is the walk that does, once you drop three lines into `funnel_app/.env`:

```bash
STRIPE_SECRET_KEY_FUNNEL=sk_live_...      # or sk_test_ with FUNNEL_STRIPE_MODE=test
STRIPE_PUBLISHABLE_KEY_FUNNEL=pk_live_...
FUNNEL_CHARGES_ENABLED=true
# and, in the same operation, the webhook signing secret:
# STRIPE_WEBHOOK_SECRET_FUNNEL=whsec_...
```

Restart, then `curl -s http://127.0.0.1:8090/health | grep -A20 billing` and confirm it reports a
`live` key, a configured webhook secret, and no missing variables. It names each one it still
wants, and a key whose prefix contradicts `FUNNEL_STRIPE_MODE` stops checkout entirely rather than
charging a different merchant account.

Point the funnel account's webhook at `https://<host>/webhooks/stripe` and subscribe to:
`checkout.session.completed`, `checkout.session.expired`, `invoice.paid`,
`invoice.payment_failed`, `invoice.upcoming`, `customer.subscription.updated`,
`customer.subscription.deleted`, `charge.refunded`, `charge.dispute.created`,
`charge.dispute.closed`, and **`radar.early_warning.created`** (Radar is a paid add-on; without it
the fraud column reads 0% and means *not measured*).

### Card A, a real credit card: the whole promise

1. Private window → `/`. Land, do not refresh. `lp_views` must gain exactly **1** (per visitor per
   UTC day, so the refresh later in this walkthrough should *not* add a second row).
2. Quiz, all twelve questions. Expect one `quiz_started` and one `quiz_completed`, and the artefact
   to render before any card field appears.
3. Checkout click. Expect one `trial_started`. On the Stripe-hosted page, the $1.00 trial, the
   $39.00 renewal and the renewal date must all be visible in the disclosure. Note the date.
4. Pay with card A. Back on `/checkout/complete`, then `/account`: `trial_paid` recorded, and
   `card_funding=credit` on that row (`sqlite3 data/billing.sqlite3 "select event,card_funding,
   card_brand from trial_events order by id desc limit 5"`).
5. **The date test.** `/account` must read **Next billing date**, not **Estimated next bill**
   (`templates/dashboard.html` picks between them on whether the processor supplied a period end),
   and the date must equal the Stripe dashboard's `current_period_end`. If it still says
   "estimated" with a live key and a paid subscription, the funnel is displaying a locally computed
   date and that is a disclosure bug, not a cosmetic one.
6. `/health`: the `trial_started` and `trial_paid` counters increment, and the rehearsal warning is
   gone.
7. `/account` → cancel. The confirmation must render **above** the save-offer menu, and the
   dashboard must immediately say the subscription ends on its period end. `cancel_clicked`
   recorded once.
8. Decline the offer. Then reload `/account`: still cancelled, `cancel_at_period_end=true` in the
   dashboard, and no second cancel event. A cancel is reversible at the processor and never
   deletes anything.
9. Repeat 7 but **accept** the lite offer ($19.00). The dashboard must re-read as active with the
   new price and a new stated period end, and the Stripe subscription must show the same.
   `save_offer_selected` recorded with the chosen option. That is the second half of the one-click
   promise: the row and the processor agree.
10. Exit survey: submit it, and confirm `survey_submitted` with the answer text.
11. `python3 ../scripts/export_lane_c_metrics.py --day "$(date -u +%F)"` from `funnel_app/`.
    Expect `lp_views 1, trials_started 1, trial_paid 1, bin_prepaid_share_pct 0.00`, a populated
    `mrr_minor`, *no* LOW VOLUME surprise being treated as a trend, and blank ratios where there is
    no denominator. Then run it again: still one row for today, and your typed notes unchanged.

### Card B, a prepaid or virtual card: the thing that kills the lane

Same walk, one substitution at step 4, and the point is the *leading indicator*, not the sale.

1. Get a genuine prepaid/virtual card (a Vanilla Visa gift card, a Dojo/Meta one-time card, a
   Revolut/Privacy.com virtual number). A debit card is not a substitute; `funding` will say
   `debit` and teach nothing.
2. Complete the trial. `trial_paid` must land with `card_funding=prepaid`, and the export's
   `bin_prepaid_share_pct` must move off zero. If it reports 0.00 while the dashboard shows a
   prepaid card, the funding is not being read off the payment intent and the single most important
   column for this niche is lying.
3. Cancel immediately (step 7) so the card is not renewed, and check `charges_unknown_bin` is 0 for
   the day. A non-zero count means the share is computed over a smaller set than the charges taken,
   which under-reports risk in the flattering direction; the export warns about it, and here is the
   case where the warning should have fired.
4. In the Stripe dashboard, open the trial payment and check the Radar risk score, then trigger an
   early warning if Radar is enabled and post a test `radar.early_warning.created` to the endpoint.
   The funnel must record `radar_early_warning`, answer 200, and survive the *same* delivery
   replayed twice with one row (`fraud_flags_count` stays 1).
5. **The foreign-event test, five minutes, worth the whole exercise.** In the dashboard, send a
   test event from a *different* account (or `stripe trigger --live-mode checkout.session.completed`
   against a live key while the app holds a test one). The funnel must answer 200, record the
   refusal in `agent_logs` with a `mode-mismatch:` or `ignored:test-mode-account-mismatch:` reason,
   and change **no** subscription and no metric. A live event from another merchant entering our
   ceilings is how a lane gets killed by somebody else's chargebacks.

### What to look at on the morning after

```bash
cd funnel_app
python3 ../scripts/export_lane_c_metrics.py --day "$(date -u -d yesterday +%F)" --fail-on-ceiling; echo "exit $?"
```

Exit `2` means a kill metric tripped and the summary names it. Exit `1` means nothing was exported
(the funnel has never run, or the database predates telemetry), which a cron wrapper has to tell
apart from a breach. Then read `notes` before any ratio: it says whether the day was low-volume,
whether MRR was gauged as of the run rather than as of the day, and whether the cumulative
processor check disagreed with the local rows, which is the shape of a webhook that has been dead
for weeks.
