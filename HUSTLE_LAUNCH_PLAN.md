# Three-Hustle Launch Plan: Manual Loophole Play, Then Wire the Agents

Companion to `EXECUTION_PLAN.md` (engine ground truth) and `grey_hat_side_hustles.md` (the dossiers).
Venture-neutral: names and niches get chosen at Week 0, not before. Every number below is a threshold
to enforce, not a prediction.

Source note: the brief named `grey_hat_side_hustles_2.md`. That file does not exist in the repo; the only
such document is `grey_hat_side_hustles.md`, which is what this plan cites, and it contains all three lanes
(dossiers 1, 2 and 4). Code claims below were verified against the shipped source, and §5.0 lists where
`EXECUTION_PLAN.md` overstates what is actually wired.

Notation: `A` = Lead-Gen Middlemanning, `B` = White-Label Markup, `C` = Free-Trial / Negative-Option.

## 0. Kill mechanisms, stated as the variables they are

The corpus's own conclusion: every model is legal; what kills operators is **profit that depends on
the counterparty not noticing**. Each hustle therefore gets an offensive lever and a tripwire, and the
tripwire is measured continuously, not audited after the fact.

| Lane | Offensive lever (from the dossiers) | What actually kills you | Tripwire (hard stop) |
|---|---|---|---|
| A | Public-record/high-intent signal + exclusive retail at $60-199/lead | "Junk lead" chargebacks (intangible, no return shipping), TCPA on third-party outreach | Dispute rate > 1% of leads billed; any single TCPA demand letter; buyer > 50% of revenue |
| B | Provenance asymmetry + 40-50% markup floor + bundle positioning | Upstream ToS sweep (SalesRobot: $10K MRR → 0 in one sweep), channel conflict, licensor termination | Upstream tactic we resell is ToS-explicit; white-label > 50% of revenue; any MAP breach |
| C | Sunk-effort funnel + status-quo inertia + one-click cancel priced as conversion | **Fraud-not-chargeback** ratio, virtual-card drop-off, policy reversal | Fraud ratio > 0.5%, any dispute ratio > 1%, processor warning received |

One rule governs all three: the gatekeeper's whole design assumes a human sees the reasoning before
money moves (`transactions.rationale`, `expected_roi_note`, `gate_context` snapshot in
supabase/schema.sql:352). No automation may post revenue it did not observe and cannot reproduce.

## 1. Lane A: Lead-Gen Middlemanning

### 1.1 The margin decision is the whole business

Same operator, one paragraph, 3% versus "one of the highest margins in the industry": owning the demand
relationship vs being one hop in a network (`grey_hat_side_hustles.md:199`). Therefore:

- **Sell exclusive, at retail, to 2-5 buyers you have spoken to.** No network hop, no shared leads.
- **Signal priority**, cheapest-to-assemble first: (1) permit/public-record filings with no contractor
  attached (dossier: $199/lead, near-zero COGS, `grey_hat_side_hustles.md:187`); (2) proximity/window
  signals (permits, new-business registrations, listing changes); (3) owned-form inbound where we pay for
  the traffic. Skip aggregated "intent" databases in week 1: unverifiable consent is exactly the liability.
- **Deliverable is a proof pack, not a CSV**: source URL + filing date + property/decision-maker + the
  verbatim timestamp. That pack is simultaneously the product, the chargeback evidence, and the
  replacement-policy arbitration record.

### 1.2 Anti-chargeback payment structure (do this before the first sale)

The chargeback story is permanent and unfalsifiable ("the number was junk",
`grey_hat_side_hustles.md:208`). Outrank it structurally rather than arguing it:

1. **Prepaid lead credit**, billed before delivery. A refund of unused credit is not a dispute, and
   card networks count *disputes*, not refunds. This single change moves most of the lane's risk to zero.
2. **Written 48-72h replacement window, 2 strikes per lead then it stands.** Bad leads get swapped, not
   refunded. Publish it in the invoice body so it is in the cardholder's statement narrative.
3. **Statement descriptor + invoice contact that a buyer recognizes.** Most small-B disputes are the
   bookkeeper not recognizing the charge.
4. **Zero-lead trial:** first 1-2 leads free, on invoice, no card. Disputes require a charge; a free lead
   cannot be disputed, and it converts the buyer faster than a pitch.
5. Card-on-file for the recurring credit top-ups only, after the first paid pack.

### 1.3 Consent and TCPA containment

Selling a contact obtained under "get a quote" to unknown third parties is where grey becomes actionable
(`grey_hat_side_hustles.md:206`). Containment, in order of safety:

- **Tier 1 (default):** sell *public-record facts about properties/businesses*, not consumer contacts, and
  the buyer initiates. No consumer phone consent question exists. This is why permit data is lane A's
  starting play.
- **Tier 2:** consumer inbound from our own form, with the named-third-party disclosure on the form
  itself, timestamp, IP, and the exact form HTML version archived per submission. We never dial; the
  buyer dials, and our disclosure is what they bought.
- **Never:** scraping a contact and having the buyer cold-call it with no disclosure anywhere.

Store the evidence at the moment of capture (hash of form + source URL + UTC timestamp). Consent evidence
compiled after a demand letter arrives is worth almost nothing.

### 1.4 Manual validation protocol

- Day 1-2: pick one metro + one trade with a public permit feed. Pull filings for the last 30 days,
  filter "filed, no GC attached" manually. Target: 50 qualified records.
- Day 3: call 10 contractors personally. Ask two questions only: what do you pay per exclusive lead, and
  what makes you refuse to pay for one. That second answer is the spec for the QA gate.
- Day 4-7: sell 3 leads at $99-199, on invoice, prepaid. Call the buyer at day 7 and day 21: did the lead
  convert, would you buy again, at what price.
- No ad spend in lane A until a lead type has sold 5 times.

### 1.5 Metrics, PnL, and graduation gate

Unit economics to hold: acquisition float ≤ $30/lead (`grey_hat_side_hustles.md:32`), sell ≥ 3x float,
net ≥ $60/lead after refund leakage. Track weekly: leads delivered, replacement rate, dispute count,
days-to-payment, revenue concentration.

**Graduate to automation when all of:** ≥ 15 leads sold, ≥ 3 distinct paying buyers, no buyer > 50% of
lane revenue, replacement rate < 15%, **dispute rate 0 on prepaid credit**, and the QA accept/reject
decision expressible as a rule with ≥ 90% agreement between two independent passes on the same records.

## 2. Lane B: White-Label Markup and Agency Arbitrage

### 2.1 Where the spread is, and what not to sign

Normative floor: resellers will not touch under 40-50% markup (`grey_hat_side_hustles.md:111`); observed
wholesale/retail pairs run $20 → $49. Target **≥ 2.5x on wholesale seat cost** at a price the market
already pays. Sell into a market the licensor lacks (geography, language, vertical, channel), not into
their own turf; that is what suppresses channel conflict instead of arguing it away.

Clause checklist, before any money moves:

| Clause | Requirement | Why (corpus) |
|---|---|---|
| Resale rights | Written, explicit, transferable to end clients | "legal risk is essentially zero *if the licence permits it*" |
| Price floor / MAP | We hold a floor; never discount below licensor direct | $12/seat reseller undercut $29 direct → cannibalised (`:125`) |
| Exclusivity | **Refuse territory exclusivity** | UK exclusivity demanded, agency folded a year later (`:118`) |
| Upstream tactic disclosure | Ask in writing whether our tactic is permitted | $10K MRR → zero from one LinkedIn sweep (`:132`) |
| SLA + data egress | Export of our client data on demand, in usable form | Terminal dependency: licensor change kills MRR overnight |
| Notice period | ≥ 60 days termination | Enough runway to migrate or refund clients |

### 2.2 The single most important structural hedge

Two independent one-hop dependencies can wipe MRR: the *licensor* and the *platform the licensor
automates*. Our arbitrage exists precisely where the upstream tactic is grey on a third platform
(LinkedIn/Meta/Google/GHL ecosystems). So:

- **Lane B must reach ≥ 2 licensor product lines before it exceeds 30% of total revenue.** Same product
  family, different vendors, is acceptable. Different *platform dependencies* is not.
- Whitelabel stays **< 50% of consolidated revenue** regardless of how well it performs.
- Keep the support boundary visible: we are the vendor of record, we absorb the complaint, and we never
  tell the client "the underlying platform has issues" (`:122`). That lie is a one-time saving that
  converts a licence renewal into a provenance-discovery crisis.

### 2.3 Pricing and licensing structure

- Flat monthly per client (not per seat) where the licensor charges us per seat. Seat-count opacity is the
  margin. Predictability is the client's reason to stay.
- Setup fee ≥ 1 month of MRR: filters tire-kickers and covers onboarding labour.
- Month-to-month with 30-day notice. Do not build a lock-in fight we lose on the licensor's roadmap.
- Disclose that support and billing come from us. Provenance (which vendor built it) may stay unstated;
  billing identity may not be false. That line is what keeps this lane "commercial risk" rather than
  "deception risk".

### 2.4 Manual validation protocol

- Week 1: shortlist 5 candidate licensors by the clause checklist, without touching a client.
- Interview 5 prospective buyers (agencies/MSPs/practices in the target vertical). Question: what would
  make you resell this instead of building or buying direct.
- Sell **2 pilot clients by hand at full retail, one month paid up front**, provisioned manually. No
  storefront, no site polish, no domain-branded email drip.
- At month 2: measure manual hours/client and whether they renewed unprompted. Renewal + low support
  load is the only proof this is a business and not a job.

### 2.5 Metrics, PnL, and graduation gate

Track: gross margin %, MRR, churn, licenses-on-cost vs licenses-revenue (the actual spread), support
tickets/client/hr, concentration (largest licensor share, largest client share), and a written count of
hours per client per month.

**Graduate when:** ≥ 4 paying clients, ≥ 2 months of renewals, gross margin ≥ 55% after licensor cost and
tooling, support < 2 hrs/client/month, no client or licensor > 50% of lane revenue, and provisioning
executable from a written runbook by someone other than the founder.

## 3. Lane C: Free-Trial / Negative-Option

Highest margin (80-90% gross on retained subs), weakest current rules (the FTC click-to-cancel rule was
vacated, `grey_hat_side_hustles.md:69`), worst tail risk. The corpus's own survival prescription: run it
with one-click cancellation and disciplined refunds (`:291`). That is not ethics branding; it is the
mechanism that keeps the merchant account alive.

### 3.1 The funnel, built to harvest inertia honestly

- **Paid trial or low-friction entry, disclosed price on the same screen as the card field**, with the
  renewal amount and date in the confirmation email (receipt doubles as the disclosure record).
- **Sunk-effort before the paywall is real:** the user must produce something (a scored result, an
  export, a generated artefact). Friction must be genuine effort, not a fake progress bar; effort is
  what makes the results worth paying for, and that is also what survives a deception review.
- **The retention prize is measurable:** $14.3K/mo of cancellations improved → $142K annualized protected
  from friction alone (`grey_hat_side_hustles.md:59`). Budget retention work accordingly.

### 3.2 Anti-cancellation, the defensible version

Friction must be **friction**, not obstruction, because obstruction is what produces fraud flags:

| Move | Defensible | Reason |
|---|---|---|
| Save-offer at cancel (pause, downgrade, 50% for 2 months) | Yes | Real economics; retention, not denial |
| One-click cancel, then a 2-question survey | Yes | Cancels the moment they click; survey after the click |
| Cancel via email with 3-day SLA | Yes | Not faster than a button, so it must not be the only path |
| Require a phone call, mailed letter, or "offline only" | **No** | Textbook Roach Motel; converts churn to fraud-flag |
| Charge a cancellation fee | **No** | Dispute magnet, and it reads as deception |

### 3.3 Fraud-flag defence (the actual kill mechanism)

Consumers increasingly stop disputing and instead cancel the card and report fraud (`:85`), and
fraud-not-chargeback ratios close merchant accounts faster than any regulator. Plus the slow extinction
event: one-off virtual card numbers make friction-based retention costless (`:73`).

Mitigations, in the order that matters:
1. **Never let fraud complaints outpace chargebacks.** Route "cancel my subscription" mail to instant
   cancellation with zero negotiation. Refund-on-request when a customer produces invoices (`:94`).
2. **Statement descriptor that matches the funnel brand**, and an email from a monitored address named in
   the receipt.
3. **Virtual-card triage:** detect prepaid/virtual BINs, send one pre-billing notice at day -2, and accept
   the decline silently rather than retrying 4 times (each retry is a fraud complaint in waiting).
4. **Card-updater on declines, one retry at day +3, then stop.** Aggressive dunning is ratio poison.
5. **Never** a refund-badge that contradicts the policy (`:89`); state the actual policy on the badge.
6. Split merchants by lane from day 1 (the `.env` already separates `STRIPE_READ_KEY_FUNNEL` /
   `STRIPE_READ_KEY_B2B`): lane C burning a high-risk funnel account must not take lane B's B2B MRR with it.

### 3.4 Manual validation protocol

- Build one offer, one funnel, one price point by hand. Copy by hand, reviewed by hand.
- Traffic: small paid budget, capped hard (see §6), spent *manually*, daily, no rules engine.
- Read **every** support message and **every** refund request personally for the whole test. This is the
  entire point of the manual phase: the refund reason vocabulary is the input to later automation.
- Test the cancel flow on a real card, as a customer, twice. If cancelling takes more than one click
  after login, that is a bug, not a feature.

### 3.5 Metrics, PnL, and graduation gate

Track per day, per funnel (columns already exist in `engine.hustle_metrics`: `mrr_minor`,
`active_subscriptions`, `new_subscriptions`, `cancellations`, `dispute_count`, `chargeback_count`,
`dispute_ratio_pct`, `revenue_minor`, `cost_minor`, schema.sql:448):

- Trial → paid conversion ≥ 3-5% (below this, no automation fixes it).
- **Month-2 retention ≥ 60%** (this is the lane's product-market signal).
- Chargeback + dispute ≤ 1%; **fraud-flag count ≤ 0.5% of charges**; monitor both weekly, not monthly.
- Refund rate treated as healthy signal: a rising refund rate with a falling dispute rate means we are
  winning at the cheap stage.
- LTV:CAC ≥ 2.5 on 3-month horizon; net contribution positive before scaling spend.

**Graduate when:** 60 consecutive days of data, ≥ 200 paid conversions, retention above threshold, both
ratio floors clean with zero processor warnings, refunds handled inside a written SLA, and a cancel-flow
that demonstrably completes in one click.

## 4. Stage 1 graduation gates, stated as gates

No agent is assigned to a lane until it passes its own gate. Two reasons, both technical rather than
philosophical:

1. **The gate must be expressible as a rule.** Workers `claim()`, dedupe, and lease (`for update skip
   locked`). An agent amplifies a decision procedure; it does not substitute for one. "It looks like a
   good lead" is not automatable; "permit filed ≤ 30 days ago, no GC on record, valuation band X" is.
2. **Failure cost jumps by orders of magnitude** once a loop can act. In lane C one bad retry policy is
   a merchant account; in lane A one bad scrape is a TCPA class posture; in lane B one upstream policy
   change is an MRR cliff.

| Gate | Lane A | Lane B | Lane C |
|---|---|---|---|
| Cash | ≥ 15 leads sold, 0 disputes on prepaid | ≥ 4 clients, ≥ 2 renewals | ≥ 200 paid conversions, 60 days |
| Margin | net ≥ $60/lead | gross ≥ 55% | LTV:CAC ≥ 2.5 |
| Safety | 0 consent complaints | 0 MAP breaches, 2 licensors | fraud ≤ 0.5%, disputes ≤ 1% |
| Concentration | no buyer > 50% | no licensor/client > 50% | no single processor warning |
| Automatable | QA rule ≥ 90% agreement | runbook executable by non-founder | lifecycle rules written down |

Partial graduation is allowed: graduate *discovery* first (read-only, no card, no spend) even before cash
gate passes, since discovery costs nothing and produces the evidence store that later needs to exist.
Never graduate *checkout* before the cash gate.

## 5. Stage 2: plugging in the existing Sirius / Ghost X2 layer

### 5.0 Four corrections to `EXECUTION_PLAN.md`, verified in source first

Everything in §5 relies on these, so they are stated before the mapping. All four re-verified in this
pass, not taken on faith.

1. **The lease layer is unwired.** `WorkerStore.claim()` (workers/common.py:496) exists and is correct,
   but `grep -rn "\.claim(" backend` outside tests returns **zero callers**. All three live workers use
   only `cursor_get`/`cursor_put`. Horizontal scaling is therefore *not* currently protected: a second
   replica takes real work, it does not idle on a contended lease. Call `claim()` in the new workers, or
   run exactly one replica per lane.
2. **Handle names.** The live discovery worker's `HANDLE` is `worker:digital_arbitrage`
   (digital_arbitrage_worker.py:376), not `worker:arbitrage` (that is the Stack B handle,
   engine/workers/arbitrage_worker.py:36). Since `check_card_limit()` buckets by `agent_handle`, using the
   wrong name silently splits one budget into two phantom envelopes.
3. **Dry-run truth is in the database, not the env var.** `route_spend` calls `store.is_dry_run()`
   (finance_agent.py:808), which reads `engine.settings` key `engine.dry_run` and fails closed to `true`.
   `ENGINE_DRY_RUN` feeds `dry_run_default`, which is read for logging only (finance_agent.py:1060) and
   never consulted on the spend path. **Flipping `ENGINE_DRY_RUN=false` in `.env` does not enable live
   spend.** Without a DB row, the gate stays in dry-run.
4. **Two execution paths in the engine tree are dead-on-arrival.** `engine/workers/arbitrage_worker.py:222`
   and `engine/workers/whitelabel_worker.py:90` pass `provider=` to `record_result()`, whose parameter is
   `external_provider` (engine/finance_gatekeeper.py:214) → `TypeError` on every execution. The funnel
   worker uses the correct name.

Ground truth carried forward, unchanged: `is_dry_run()` fails closed to `true` on any error
(finance_agent.py:352); `SUPERVISOR_ENABLED` defaults **OFF** and nothing in code or SQL enforces the
single-writer rule (it is operational convention only); `FINANCE_AGENT_URL` / `FINANCE_AGENT_TOKEN` are
read at workers/common.py:964 and are **absent from `.env`**, so ungated workers raise rather than spend.

Two stacks, one `engine` schema, and they do not talk: Stack A (live) is `supabase/schema.sql` +
`backend/finance_agent.py` + `backend/workers/*` → `transactions`, `financial_ledger`, `hustle_metrics`;
Stack B (Module 1) is `supabase/migrations/0001-0003` + `backend/engine/*` → `approval_requests`,
`ledger_entries`, `agents`, `arbitrage_targets`, `daily_metrics`. Tiers differ: 30/250 on Stack A
(`FINANCE_TIER1_MAX_MINOR=3000`), 50/250 on Stack B (`APPROVAL_TIER1_MAX_MINOR=5000`). The deployed
arbitrage worker creates `engine.arbitrage_targets` from its own inline DDL because Stack A's schema never
installed one, while Stack B's buyer logic reads the `migrations/0001` version of the same-named table.
**Same table name, two owners, no shared definition.** Every lane below states which stack it lives on,
and phase 1 stays on Stack A only.

### 5.1 Which gate the lanes run on

All phase-1 lanes run on **Stack A** (live deploy): `POST /spend/gate` → `route_spend()`
(finance_agent.py:779), which always inserts a `transactions` row plus an `agent_logs` audit row, and
answers one of: `auto_approved/authorized/execute=true`, `auto_approved/initiated/execute=false` (dry
run), `pending` (tier2), `blocked` (tier3 or envelope refused), `already_decided` + `duplicate=true`
(replay). Inputs the gate actually accepts: `agent_handle`, `title`, `amount_minor`, `currency`,
`idempotency_key`, `external_id`/`external_provider`, `hustle`, `vendor`, `rationale`,
`expected_roi_note`, `source_url`, `metadata`. **There is no per-request venture identity**, which is why
§5.3 exists.

### 5.2 Actor mapping per lane

| Lane | Handle | Stack | Owns | Forbidden | New build required |
|---|---|---|---|---|---|
| A | `worker:leadsource` (new) | A | Permit/record scraping, dedupe, evidence pack, `arbitrage_targets` rows | Consumer dialling, buyer billing | `lead_evidence` + `lead_disputes` (§5.4) |
| A | `worker:leadqa` (new) | A | Accept/reject by rule, replacement-window clock, buyer credit ledger | Charging a card | Billing stays manual/invoice in phase 1 |
| B | `worker:whitelabel` (exists: agency_worker.py:193) | A | Prospect intake, provisioning, renewal ticks, support routing | Price below the MAP floor | MAP floor as a `settings` key |
| B | `worker:licensor_watch` (new, read-only) | A | Upstream ToS/pricing page-hash capture | Any mutation | None; rows into `agent_logs` |
| C | `worker:funnel` (exists: funnel_worker.py:162) | A | Trial lifecycle, save-offer sequencing, pre-billing notices, dispute refunds | Card issuance, uncapped ad spend | Refund/retry policy config |
| C | `worker:storekit` (later) | A | Apple subscription mirror | Stripe billing | New `hustle` or a venture tag |
| All | `finance-agent` | A | Tier classification, envelope, ledger posts, Telegram cards | Scraping, copy, provisioning | nothing |
| All | `supervisor` | A | Alert cards, expiry sweep, heartbeat | Auto-approving (it only expires) | nothing |
| All | `telegram` | A | Read/report, owner-only decision relay | Initiating spend | nothing |

Why lane A gets a **new** handle rather than riding `worker:digital_arbitrage`: that worker is provably
discovery-only (zero references to `request_spend`/`SpendTicket`/`SpendGateway` in 1221 lines, and it runs
happily with `FINANCE_AGENT_URL` unset), which is exactly the safety property a scraping loop wants. Do not
dilute it by bolting billing onto the same handle, because the card envelope is keyed on
`agent_handle`: giving buyer-side spend its own handle keeps a discovery runaway from eating the billing
budget, and vice versa. Register the new handles in `engine.agents` **before** deploying them, on both
stacks: `request_approval` raises `unknown agent` on an unregistered handle, so an unregistered agent
cannot spend at all, which is the good failure mode.

**Do not create two workers with the same handle on both stacks.** `worker:funnel` and
`worker:whitelabel` already exist in `backend/workers/` *and* `backend/engine/workers/`, and both trees
write to the one shared `engine` schema.

### 5.3 Guardrails, and the specific way each one breaks

1. **Unique handles.** `agents.handle UNIQUE` (0001_engine_core.sql:111), and `request_approval` raises
   `unknown agent` (0001:522). The bad failure mode is reusing one handle for two things:
   `check_card_limit()` sums `engine.financial_ledger` filtered on `agent_handle` + `currency` over
   calendar day and month (finance_agent.py:561-574, cap entry types `charge, fee, expense, dispute,
   chargeback, transfer_out`), so two lanes sharing a handle share one envelope and lane C's traffic
   starves lane B's tooling spend. Default caps are tight on purpose: `CARD_DAILY_CAP_MINOR=5000` ($50)
   and `CARD_MONTHLY_CAP_MINOR=50000` ($500). Note the **issuer fails open** (`CardIssuerClient.check_limit`
   :644): a dead issuer API means only the local budget protects us, so the local cap is the real cap.
2. **Leases per (handle, job).** `WorkerStore.claim()` with `for update skip locked` and a
   `worker.lease.contended` audit row exists, and fails closed (returns False on any error) — but see §5.0
   #1: **no production worker calls it today.** Either wire it into the new workers or run one replica per
   lane; do not assume `EXECUTION_PLAN.md` §1.2's horizontal-scaling guarantee is live. Job names are
   free-form with no enforced convention; use `leadsource:permits:scan`, `whitelabel:renewals`,
   `funnel:lifecycle`, one per lane-venture pair.
3. **Two dedupe nets.** `seen()`/`mark_seen()` write `agent_logs` rows under `event='worker.item.seen'`
   (common.py:608/629) and `seen()` fails closed by returning True on error, so a DB blip skips an item
   rather than duplicating it. Plus `transactions_idempotency_uq` (schema.sql:426),
   `transactions_external_uq` (:422), `financial_ledger_external_uq` (:317). Lane A identity: (jurisdiction,
   filing id, parcel). Lane C: the Stripe event id. Caveat for lane A: `seen()` state lives in a **log
   table with no TTL defined here**; on a pruned `agent_logs` the dedupe forgets. Treat
   `arbitrage_targets (hustle, source, external_ref)` UNIQUE (0001:285) as the durable net, not `seen()`.
4. **Per-agent envelopes.** Local budget evaluated first, so a runaway loop stops without touching the
   network; the tighter of local and issuer wins.
5. **Idempotency and replay.** `route_spend` checks `find_existing(external_id, external_provider,
   idempotency_key)` *before* classifying (:800) and re-checks after insert (:849) for the concurrent case,
   returning `already_decided` with `duplicate=true`. Existing funnel workers already use stable keys
   (`funnel:step:{id}`, `funnel:{identity_hash}`). Every new intent needs one, or a crash between approval
   and execution becomes a second charge.
6. **Tier thresholds.** Two gates coexist, 30/250 (Stack A) and 50/250 (Stack B), both DB-overridable via
   `engine.settings` (`finance.tier*_max_minor` vs `approval.tier*_max_minor`). Pick Stack A for all three
   lanes in phase 1 and route nothing through the Stack B gatekeeper, or the same $40 purchase is
   auto-approved on one path and queued on the other.
7. **Dry-run flip order.** To go live on a lane: set `engine.settings.engine.dry_run = false` (the gate
   reads the DB), not `ENGINE_DRY_RUN=false` (it does not). Per-action dry-run probing already exists in
   funnel_worker.py:784, so a new worker should probe then execute the same way.
8. **Human-in-the-loop for the irreversible things**: card issuance, ad spend above tier 1, refunds above
   the micro cap, any lane-C policy change, any lane-B client migration. `notify_telegram_pending()` fires
   on *every* pending tier-2 automatically from `route_spend` (:867), with a ⚠️ marker above the
   $50 floor. Cards carry `rationale`, `expected_roi_note`, and an immutable `gate_context` snapshot of the
   envelope at decision time; keep putting the real reasoning there.
9. **Approval expiry is live and not dry-run-aware.** `TxStore.expire_due()` mutates
   `approval_status → 'expired'` regardless of `ENGINE_DRY_RUN` (supervisor.py:477). Pending test intents
   older than the expiry window disappear, which is correct behaviour to know about before judging a
   "lost" approval.

### 5.4 The gap that must be filled before lane A can be automated

Neither stack models **evidence or disputes**. `risk_flag` on `transactions` (`disputed`, `chargeback`,
`fraud_flag`, `refund_requested`) plus `entry_type='dispute'` ledger rows means the system can *count* the
kill mechanism but cannot hold the *proof that prevents it*. There is no consent record, no dispute dossier,
and `arbitrage_targets.evidence_count` is a count with no artifacts behind it. The only opt-in field in the
repo is `funnel_sequences.opt_in_at` (funnel_worker.py:277), which is a timing offset for trigger
sequencing, not captured consent. Minimum viable addition, one migration on Stack A:

```sql
create table engine.lead_evidence (
  id            uuid primary key default gen_random_uuid(),
  target_id     uuid not null references engine.arbitrage_targets(id) on delete cascade,
  source_url    text not null,
  captured_at   timestamptz not null default now(),   -- UTC, never local
  source_hash   text not null,                        -- sha256 of the fetched payload
  payload       jsonb not null,
  consent_tier  text not null check (consent_tier in ('public_record','own_form_disclosed','purchased'))
);

create table engine.lead_disputes (
  id            uuid primary key default gen_random_uuid(),
  target_id     uuid not null references engine.arbitrage_targets(id) on delete cascade,
  buyer_id      uuid not null,
  state         text not null check (state in ('open','replaced','credited','upheld','expired'))
                default 'open',
  reason        text not null,
  opened_at     timestamptz not null default now(),
  window_ends_at timestamptz not null,
  resolved_at   timestamptz,
  resolution    text
);
```

Hash the payload at fetch time. Retroactive evidence ("here's the page as of today") is worthless in both
a chargeback representment and a consent dispute, and a scraped page can quietly change.

**The two rollup tables are not the same table.** `engine.hustle_metrics` (Stack A, PK `(day, hustle)`,
written by `WorkerStore.add_metrics` for flows and `set_gauges` for stocks, with `net_profit_minor`
generated) and `engine.daily_metrics` (Stack B, written by the `rollup_daily_metrics()` RPC) overlap in
columns and nothing reconciles them. Report from one, per lane, and say which; two dashboards quoting two
numbers is how a lane looks profitable while bleeding.

**Venture identity is still absent.** No `engine.ventures` table, no `venture_id` column anywhere in `.py`
or `.sql`. Today the only separation dimensions are `hustle_kind` (three values), `agent_handle`, and the
`account text default 'operating'` string. `EXECUTION_PLAN.md` §1.3 is right that this is the missing piece,
with one correction: `ledger_entries` and `hustle_metrics` live in **different schemas**, so a single ALTER
list touches two unrelated stacks. Scope the migration to Stack A only:

```sql
create table engine.ventures (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique, display_name text not null,
  stripe_account text, card_profile text,
  llm_budget_usd_day numeric(10,2) not null default 0,
  status text not null default 'active' check (status in ('active','paused','retired')),
  created_at timestamptz not null default now()
);
alter table engine.transactions   add column venture_id uuid references engine.ventures(id);
alter table engine.hustle_metrics add column venture_id uuid references engine.ventures(id);
create index if not exists txn_venture_idx   on engine.transactions (venture_id, created_at desc);
create index if not exists metrics_venture_idx on engine.hustle_metrics (venture_id, day desc);
```

Note that adding `venture_id` to `hustle_metrics` also means dropping/replacing its `PK (day, hustle)` with
`(day, hustle, venture_id)`, and `add_metrics()`'s upsert `ON CONFLICT` target must change in the same
commit. Sequencing matters here, so this migration lands **before** three lanes have history, not after.

### 5.5 Telegram surface per lane

Existing surface: `/status`, `/tick`, `/pending`, `/approve|/reject <uuid>`, inline `approve_`/`deny_`
cards with race-safe still-pending semantics and `editMessageText` final state, plus a plain-English agent
(read-mostly tools; writers opt-in). Additions, in this order:

1. **Per-lane reporting** — `/lane a|b|c` rendering the lane's `hustle_metrics` row plus its envelope, so
   a lane's ratio creep is visible without cross-lane noise. Read-only, so it inherits no risk.
2. **Risk cards, not just spend cards** — an anomaly card when dispute/fraud ratio crosses threshold, or
   when a licensor's ToS page hash changes. These are *events without a spend*, which today the gate has
   no reason to raise; emit them as supervisor digest entries rather than approving anything.
3. **Explicitly do not give the chat agent writers** for: refunds, card issuance, cancellations. Those
   stay button-press approvals. A model that confidently cancels a subscription is a refund we cannot
   claw back, and the write path already refuses on invented or ambiguous ids — keep that behaviour and
   keep the write surface tiny.
4. Approvals from Telegram must stay owner-chat-gated (`TELEGRAM_APPROVAL_CHAT_ID`), with
   `TELEGRAM_WEBHOOK_SECRET` set. It ships empty in `.env.example:94` and was unset on the VPS as of the
   last deployment, so unsigned mode is live: acceptable while the surface is read-only, unacceptable once
   lanes are pushing money through button taps. Fix before phase 2. (`WEBHOOK_SIGNING_SECRET`,
   `.env.example:102`, is a separate knob for the Stripe-side webhooks; both need real values.)

## 6. Phase 2: rollout order across the three lanes

Order is chosen by **blast radius of the first automated mistake**, not by expected revenue.

| Phase | Lane | What gets automated | Ceiling | Gate to advance |
|---|---|---|---|---|
| 0 | all manual | Nothing. Evidence capture + metrics populated by hand. Prereqs: fix §5.0 items 1-4, register handles, set `TELEGRAM_WEBHOOK_SECRET` | $0 automated | §4 gates |
| 1 | **B** whitelabel | Renewal reminders, provisioning steps, prospect intake, ToS page-hash watcher | $0 card; tier-1 only tooling spend ($30 auto on Stack A) | 30 days, zero double-provision, no MAP breach |
| 2 | **A** lead discovery | Scraping, dedupe, evidence hash, QA rule, pack assembly | discovery is free; billing stays manual/invoice | ≥ 90% QA-rule agreement, zero junk leads delivered |
| 3 | **C** lifecycle read-only | Trial-lifecycle state, pre-billing notices, save-offer sequencing | $0 charges issued by the agent | 30 days, ratios flat, no false "cancelled" |
| 4 | **C** billing actions | Auto-refund on request, dispute refunds, dunning-stop rules | micro cap ($5) only; every tier-2 stays Telegram-approved | 60 days, fraud ratio ≤ 0.5% sustained |
| 5 | scale-out | Replicate handles/jobs per venture, add `worker:buyer` for real checkout | per-venture envelopes | nothing colliding, **leases actually contending** |

Why B first: it moves no card money (our own Stripe, disclosed price, real product), it produces the
steady MRR that funds the other two, and its failure mode (angry licensor) is recoverable by contract
rather than fatal. Why C last: automated *actions* in lane C touch the one thing that cannot be undone —
charging and un-charging consumers on a processor relationship with a ratio ceiling. Lane A sits between:
its automation is mostly read-heavy, but it touches consumer data, so evidence capture must be live
before volume.

Anti-collision rules enforced during the rollout:
- Distinct `agent_handle` **and** distinct `job` name per lane-venture pair. No exceptions.
- Per-lane card caps in `settings`, low for lane C, and lane B's tooling spend under a separate handle so
  lane C's dunning can never exhaust the shared envelope.
- Separate Stripe keys per lane (`STRIPE_READ_KEY_FUNNEL` vs `STRIPE_READ_KEY_B2B`); lane C is the only
  high-risk merchant.
- One auto-deciding loop at a time (`SUPERVISOR_ENABLED`, default OFF). Two loops double-decide, and
  nothing in code or SQL enforces the exclusion; it is convention, so write the choice down. Note the two
  loops are not symmetric: standalone `supervisor.py` only *expires* pending rows, while
  `supervisor_tick()` in finance_agent.py:1263 is the one that micro-approves (≤ $5) and escalates. Its
  sibling also polls while the webhook is registered, producing the known 409 Conflict traceback. Resolve
  before phase 1, not during.
- Keep `engine.dry_run = true` (the DB key) for every new worker until its first manually-approved live
  action. It fails closed on error, missing row, and unparseable JSON: a DB blip means no spend, which is
  the intended direction.

## 7. Immediate actions (this week, no agents)

Business:
1. Create the two Stripe accounts (B2B + high-risk funnel). Lane C cannot start without it.
2. Pick lane A's jurisdiction + trade, pull 50 permit records by hand, sell 3 leads on invoice.
3. Shortlist 5 lane B licensors against the §2.1 checklist; kill any licensor whose written answer on our
   tactic is "probably fine".
4. Start `hustle_metrics` rows by hand, daily, for all three lanes, even the zero-revenue ones. A metric
   nobody collected during the manual phase cannot be defended during the automated one.

Engine, in dependency order:
5. **Fix `record_result(provider=...)` in the two Stack B workers** (arbitrage_worker.py:222,
   whitelabel_worker.py:90 → `external_provider`). One-word fixes; those execution paths currently
   `TypeError` on every call, so anything downstream that appears "not yet built" is actually broken.
6. **Register `TELEGRAM_WEBHOOK_SECRET`** (ships empty at `.env.example:94`), settle
   `SUPERVISOR_ENABLED` single-writer status, and decide + document whether `supervisor.py`'s polling stays
   on while the webhook is registered. Also give `FINANCE_AGENT_URL` / `FINANCE_AGENT_TOKEN` real values in
   `.env`, since every gated worker currently raises without them.
7. **Write the evidence/dispute tables plus `engine.ventures` + `venture_id`** (§5.4, Stack A only), before
   any scraping at volume, and change `add_metrics()`'s conflict target in the same commit.
8. **Wire `claim()` into the new lane workers, or declare single-replica** (§5.0 #1), and give each new
   worker its own `handle` registered in `engine.agents`.
9. **Document the dry-run truth** in one place: live spend requires `engine.settings.engine.dry_run =
   false`; `ENGINE_DRY_RUN=false` alone does nothing on the spend path. Worth a line in `.env.example`,
   because that file is what people read.
