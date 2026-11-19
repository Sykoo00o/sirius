# Validation & Selective Agent Assistance: Zero to First Revenue, Then Automate

Companion to `HUSTLE_LAUNCH_PLAN.md` (thresholds and rollout order) and `EXECUTION_PLAN.md` (engine ground truth).
This document answers a narrower question: **what is the smallest sequence of human actions that produces real
money or a real kill signal in each lane, and where may an agent legally help before that happens.**

Where this document and `HUSTLE_LAUNCH_PLAN.md` overlap on thresholds, that document is the authority and this
one cites it. Numbers below are restated, not redecided, except where flagged as **ADJUSTMENT**.

Notation: `A` = Lead-Gen Middlemanning, `B` = White-Label Markup & Agency Arbitrage,
`C` = Free-Trial / Negative-Option. Source dossiers: `grey_hat_side_hustles.md` dossier 4 (A), 2 (B), 1 (C).

Two naming traps worth settling in the first line, because both appear in the brief:

1. **The agent assistance described here does not mean `engine.agents`.** Lane A's "buyers" are contractors and
   lane B's are agencies (MSPs, practices). Separately, `engine.agents.handle` is a UNIQUE database identity that
   gates spending (`supabase/migrations/0001_engine_core.sql:111`, `request_approval` raises on an unknown
   handle). A "business agent" is not a row in that table, and nothing in validation should be.
2. **"Flip `dry_run` off" and "enable `SUPERVISOR_ENABLED`" are not validation prerequisites, and the first one
   is the wrong knob.** `route_spend` reads `engine.dry_run` from `engine.settings` (`finance_agent.py:808`);
   `ENGINE_DRY_RUN` feeds `dry_run_default`, which is read for logging only (`finance_agent.py:1060`). During
   validation the gate should stay dry-run, because we want it to record intent without acting. Validation uses
   the LLM router and manual books; the gate is rehearsed, never trusted.

Re-verified in this pass, against the deployed source rather than the other plans:

| Claim | Status |
|---|---|
| Router exposes exactly two tiers, `fast` / `reasoning` | Confirmed `llm_router.py:51-53`; `complete()` defaults to `fast` (`:360`) |
| Chat agent is read-only unless writers are passed in | Confirmed `finance_agent.py:2760-2762`, `can_write` derived from `TOOL_APPROVE`/`TOOL_REJECT` presence |
| Writer tools are exactly three | Confirmed: `get_pending_transactions`, `approve_transaction`, `reject_transaction` (`:2133-2135`) |
| `hustle_metrics` columns exist for lane C tracking | Confirmed `schema.sql:448` region, incl. `dispute_ratio_pct` |
| `FINANCE_AGENT_URL` / `_TOKEN` absent from `.env` | Confirmed; ungated workers raise rather than spend (`workers/common.py:964`) |
| Lease layer exists but has no production caller | Confirmed; `grep "\.claim("` outside tests returns zero callers |

The operative sentence for §2, from the corpus itself (`grey_hat_side_hustles.md:199`, an operator with millions
in revenue in the lane): *"the leads are the EASY part ... The trick to this whole thing is having the right
buyers."* Agents are excellent at the easy part. Every assist task below is therefore pointed at the hard part,
and no assist task is allowed to become the business.

---

## 1. Step-by-step validation per hustle

Shared shape, so the three lanes stay comparable and the metrics land in one place. Five steps per lane, then a
gate table (§1.4). Three rules bind all nine sequences:

- **No ad spend before the first organic or outbound sale.** Spend buys volume; it does not buy a diagnosis.
- **Start the metrics row on day 1, even at zero.** `HUSTLE_LAUNCH_PLAN.md` §7.4 says a metric nobody collected
  during the manual phase cannot be defended during the automated one. That is a data-quality fact, not a slogan:
  ratio floors need a denominator history.
- **The founder's phone is the primary instrument.** Every lane below has a step that is "call ten people and ask
  two questions". No agent can do those ten calls, and each one produces the acceptance rule that later gets
  automated. Skipping them is what makes a loop dangerous rather than merely premature.

### 1.A Lane A: Lead-Gen Middlemanning

Product is a **proof pack, not a CSV**: source URL + filing date + property/decision-maker + verbatim timestamp
(`HUSTLE_LAUNCH_PLAN.md` §1.1). That single artifact is the product, the chargeback representment, and the
consent record, which is why it exists before any automation does.

**Step 1, Market & niche selection (days 1-2).**
Criteria, in elimination order:

1. Jurisdiction publishes permits (or equivalent filings) as a **downloadable feed**, not a JS search widget.
   If it is a widget, the extraction cost eats the margin; if it is a feed, discovery is free forever.
2. Trade has a lead price already visible in the market: the dossier's own range is **$60 roofing, $90 solar,
   $199 remodel permit** (`grey_hat_side_hustles.md:187`, :190). If nobody publicly pays for the trade's leads,
   there is no resale market to enter.
3. Valuation band high enough that $99-199 is rounding error to the buyer: **$40-70K jobs** is the observed
   remodel band (`:187`).
4. The signal survives a phone call: someone answers and knows what the filing is.
5. Feed is **public record about a property or business**, which is consent Tier 1 (§1.3 of the launch plan) and
   removes the TCPA question entirely, because the *buyer* initiates contact.
6. Volume: ≥ 30-50 qualified filings per month, or the lane cannot support 15 sales (§1.4 gate).

Target output: **50 qualified records**, filtered "filed, no GC attached", last 30 days, one metro, one trade.
Skip aggregated "intent" databases in week 1: unverifiable consent is exactly the liability.

**Step 2, Offer, funnel & risk setup (days 2-3).** The payment structure is the product decision here, and it
must be finished before the first sale, not retrofitted after the first dispute.

- **Prepaid lead credit**, billed before delivery. A refund of unused credit is not a dispute, and card networks
  count disputes, not refunds. This one change moves most of the lane's risk to zero.
- **Written 48-72h replacement window, 2 strikes per lead then it stands.** Bad leads get swapped, not refunded.
  Put it in the invoice body so it lands in the statement narrative.
- **Recognizable statement descriptor + invoice contact.** Most small-B disputes are a bookkeeper not recognizing
  a charge.
- **Zero-lead trial: first 1-2 leads free, on invoice, no card.** Disputes require a charge; a free lead cannot
  be disputed, and it converts faster than a pitch.
- Card on file for top-ups only, and only after the first paid pack.
- Merchant account: B2B lane. Do **not** run lane A through the high-risk funnel account. The repo already
  separates `STRIPE_READ_KEY_FUNNEL` / `STRIPE_READ_KEY_B2B`; keep that split on day 1.
- No funnel build. A one-page PDF spec of the pack and an invoice are the whole funnel.

**Step 3, Manual demand & cash test (days 3-14).**

1. **Day 3: ten calls to contractors, two questions only.** (a) What do you pay per *exclusive* lead?
   (b) What makes you refuse to pay for one? Question (b)'s answers are the literal spec for the future QA gate;
   they are also the only buyer list that matters.
2. Deliver 1-2 free leads to the two most interested of the ten. Invoice, no card.
3. **Sell 3 leads at $99-199, prepaid.** One lead type, one trade, no substitution.
4. Call the buyer at **day 7 and day 21**: did the lead convert, would you buy again, at what price. Day 21 is
   when renewal intent is real and the "junk lead" story either appears or does not.
5. Repeat the pull weekly, by hand, and log the hours. Hours-per-50-records is the input to the labor-saving
   argument later; guessing it afterwards is how operators automate something that was never the bottleneck.
6. **No ad spend in lane A until a lead type has sold 5 times.**

**Step 4, Unit economics & risk check (weekly).** Acquisition float ≤ $30/lead (`:32`), sell ≥ 3x float,
net ≥ $60/lead after refund leakage. Track weekly: leads delivered, replacement rate, dispute count,
days-to-payment, revenue concentration. The three failure numbers to watch are not margin numbers: **replacement
rate** (product truth), **dispute rate on prepaid credit** (should be structurally 0, and anything else means the
structure is wrong), and **largest-buyer share of revenue** (a buyer at > 50% is a job, not a business).

**Step 5, Pass / pivot / graduate.** See §1.4. Lane-A-specific pivot readings:

- Leads sell but do not convert → wrong signal, not wrong market. Move down the signal ladder
  (permit → proximity/window → owned-form inbound), do not lower the price.
- Converts but nobody rebuys → the buyer's sales process is the bottleneck; ask day-21 "who called it, how fast"
  and consider fewer, better buyers.
- Replacement rate ≥ 15% → the QA rule is unexpressible, and an unexpressible rule cannot be automated. Fix the
  source filter first.

### 1.B Lane B: White-Label Markup & Agency Arbitrage

Cheapest to validate, lowest blast radius, and the only lane whose revenue is recurring on our own Stripe at a
disclosed price. It goes first in the rollout for exactly this reason (`HUSTLE_LAUNCH_PLAN.md` §6).

**Step 1, Vendor & niche selection (week 1).** Score 5 candidate licensors against the §2.1 clause checklist
before touching a client. Hard requirements:

| Clause | Requirement | Corpus basis |
|---|---|---|
| Resale rights | Written, explicit, transferable to end clients | "legal risk is essentially zero *if the licence permits it*" (`:128`) |
| Price floor / MAP | We hold a floor; never discount below licensor direct | $12/seat reseller undercut $29 direct → cannibalised (`:125`) |
| Exclusivity | **Refuse territory exclusivity** | UK exclusivity demanded, agency folded a year later (`:118`) |
| Upstream tactic disclosure | Ask in writing whether our tactic is permitted | $10K MRR → zero from one LinkedIn sweep (`:132`) |
| SLA + data egress | Export of our client data on demand, in usable form | Licensor change kills MRR overnight |
| Notice period | ≥ 60 days termination | Runway to migrate or refund clients |

Plus two arithmetic gates: **≥ 2.5x on wholesale seat cost** (normative floor is 40-50% markup, `:111`, observed
pairs run $20 → $49), and **sell into a market the licensor lacks** (geography, language, vertical, channel).
Selling into their own turf is what manufactures channel conflict.

Kill rule, stated bluntly in the launch plan and repeated here because it is the highest-value sentence in the
validation: **kill any licensor whose written answer about our tactic is "probably fine"**. Ambiguity from a
licensor is a future $10K-to-zero event.

**Step 2, Offer, pricing & risk setup.** Flat monthly per client where the licensor charges us per seat (seat
opacity is the margin; predictability is the client's reason to stay). Setup fee ≥ 1 month of MRR: filters
tire-kickers and covers onboarding labor. Month-to-month with 30-day notice; do not build a lock-in fight we lose
on the licensor's roadmap. Disclose that **support and billing come from us**: provenance may stay unstated,
billing identity may not be false. That line is what keeps this lane "commercial risk" rather than "deception
risk", and it is why we never tell a client "the underlying platform has issues" (`:122`).

Structural hedge before any client: lane B must reach **≥ 2 licensor product lines before exceeding 30% of total
revenue**, and stays **< 50% of consolidated revenue** no matter how well it performs. Same product family,
different vendors, is acceptable; different *platform dependencies* is not.

**Step 3, Manual demand & cash test (weeks 1-4).** Interview 5 prospective buyers (agencies / MSPs / practices in
one vertical). One question: what would make you resell this instead of building or buying direct. Then **sell 2
pilot clients by hand at full retail, one month paid up front, provisioned manually by hand**. No storefront, no
site polish, no domain-branded drip. At month 2, measure manual hours per client and whether they renewed
**unprompted**. Renewal plus low support load is the only proof this is a business rather than a job.

**Step 4, Unit economics & risk check.** Track: gross margin %, MRR, churn, licenses-on-cost vs licenses-revenue
(the actual spread), support tickets/client/hr, largest-licensor share, largest-client share, and a **written**
count of hours per client per month. Watch for the two silent killers rather than the margin: an upstream ToS or
pricing page that changed (capture a page hash weekly, from day 1, read-only), and MAP drift on our own side.

**Step 5, Pass / pivot / graduate.** See §1.4. Lane-B pivot readings:

- Buyers want it but only at a price that breaks the 2.5x → wrong licensor, not wrong market. Re-shortlist.
- Renews only after we chase → it is a churn clock, not MRR; recompute LTV with a 3-month life before scaling.
- Support > 2 hr/client/month → the licensor's product quality is our cost line; renegotiate SLA or exit.
- One licensor > 50% of lane revenue → concentration tripwire fired; add the second licensor before adding
  clients.

### 1.C Lane C: Free-Trial / Negative-Option

Highest margin (80-90% gross on retained subs), weakest current rules (the FTC click-to-cancel rule was vacated,
`:69`), worst tail risk. The corpus's own survival prescription is to run it with one-click cancellation and
disciplined refunds (`:291`), which is a mechanism, not branding: it is what keeps the merchant account alive.

**Step 1, Niche & offer selection.** Criteria:

1. A **genuine artefact** the user produces before the paywall: a scored result, an export, a generated report.
   Sunk effort must be real effort, not a fake progress bar; effort is what makes results worth paying for *and*
   what survives a deception review.
2. Recurring value or a consumable, so inertia has something to attach to.
3. Price disclosed **on the same screen as the card field**, with renewal amount and date in the confirmation
   email, so the receipt doubles as the disclosure record.
4. **Virtual-card survivability.** One-off card numbers make friction-based retention costless (`:73`) and are in
   structural growth. Prefer niches where a real card is normal, and instrument prepaid/virtual BIN detection from
   the first charge.
5. A COGS line small enough that a 20% refund rate still leaves positive contribution; in this lane refunds are a
   tool.
6. Nobody in the niche currently cancels in one click. That gap *is* the positioning.

**Step 2, Funnel, trial mechanics & processor setup.** One offer, one funnel, one price point. Copy by hand,
reviewed by hand. Enrolment disclosure technically true, badge never contradicting the policy page (`:89`: an
unqualified "30 day money back guarantee" linking to a no-refunds page is materially closer to a
deceptive-representation claim than a dark pattern). Statement descriptor matches the funnel brand; the receipt
names a monitored email address.

Defensible anti-cancellation set (obstruction, not friction, is what produces fraud flags):

| Move | Verdict | Why |
|---|---|---|
| Save-offer at cancel (pause, downgrade, 50% for 2 months) | Yes | Real economics; retention, not denial |
| One-click cancel, then a 2-question survey | Yes | Cancels on the click; survey after it |
| Cancel via email with 3-day SLA | Yes | Acceptable as an *extra* path, never the only one |
| Phone call / mailed letter / "offline only" | **No** | Textbook Roach Motel; converts churn to a fraud flag |
| Cancellation fee | **No** | Dispute magnet, reads as deception |

Merchant account: the high-risk funnel Stripe account, isolated from B2B from day 1, so lane C burning a funnel
account cannot take lane B's MRR with it. Retry policy decided before launch, not tuned under pressure: one
pre-billing notice at day -2, accept prepaid/virtual declines **silently** (no 4x retry, each retry is a fraud
complaint in waiting), card-updater on declines, one retry at day +3, then stop.

**Step 3, Manual demand & cash test.** Traffic is a small paid budget, capped hard (§2.4), spent *manually*,
daily, by hand, no rules engine. Then the step that makes the whole phase worth doing: **read every support
message and every refund request personally for the entire test**. The refund-reason vocabulary is the input to
the lifecycle rules that automation will later execute; nobody can write those rules from imagination. Finally,
**test the cancel flow on a real card, as a customer, twice**. If cancelling takes more than one click after
login, that is a bug, not a feature.

**Step 4, Unit economics & ratio check.** Daily, into `engine.hustle_metrics` (columns already exist: `mrr_minor`,
`active_subscriptions`, `new_subscriptions`, `cancellations`, `dispute_count`, `chargeback_count`,
`dispute_ratio_pct`, `revenue_minor`, `cost_minor`):

- Trial → paid conversion ≥ 3-5%. Below this, no automation fixes it.
- **Month-2 retention ≥ 60%**: this lane's actual product-market signal.
- Chargeback + dispute ≤ 1%; **fraud-flag count ≤ 0.5% of charges**. Weekly, never monthly; ratios move faster
  than a monthly review can catch them.
- **A rising refund rate with a falling dispute rate is a healthy signal**, because it means we are winning at the
  cheap stage. Read them as a pair or the pair will mislead.
- LTV:CAC ≥ 2.5 on a 3-month horizon; net contribution positive before scaling spend.

**Step 5, Pass / pivot / graduate.** See §1.4. Lane-C pivot readings:

- Conversion fine, month-2 retention < 60% → the artefact was not worth keeping. Fix the product, not the funnel.
  No paid-traffic increase fixes this.
- Fraud-not-chargeback outpacing chargebacks → immediate stop on paid traffic; that ratio closes accounts faster
  than any regulator (`:85`).
- Any processor warning → treat as the hard stop it is, and stop scaling before discussing whether it is fair.

### 1.4 Gate table (restated from §4 of the launch plan, unchanged)

No agent is assigned to a lane until it passes its own gate. The two reasons are technical: a worker amplifies a
**decision procedure**, and a decision must be expressible as a rule for a `claim()`-and-execute loop to use it;
and failure cost jumps by orders of magnitude the moment a loop can act.

| Gate | Lane A | Lane B | Lane C |
|---|---|---|---|
| Cash | ≥ 15 leads sold, 0 disputes on prepaid | ≥ 4 clients, ≥ 2 renewals | ≥ 200 paid conversions, 60 days |
| Margin | net ≥ $60/lead | gross ≥ 55% after licensor cost + tooling | LTV:CAC ≥ 2.5 |
| Safety | 0 consent complaints | 0 MAP breaches, ≥ 2 licensors | fraud ≤ 0.5%, disputes ≤ 1% |
| Concentration | no buyer > 50% | no licensor/client > 50% | no single processor warning |
| Automatable | QA rule ≥ 90% agreement, two independent passes | provisioning runbook executable by a non-founder | lifecycle rules written down |

**ADJUSTMENT (one addition, marked because it is new):** the "Automatable" column is the gate that actually
predicts a smooth graduation, so treat it as the leading indicator, not the last box. Record ≥ 90% agreement,
non-founder runbook success, and written lifecycle rules *during* validation, before the cash numbers could
possibly be met. A lane that hits its cash gate while its rule is still vibes has bought volume it cannot
operate.

**Partial graduation is allowed** (launch plan §4): graduate *discovery* early, since read-only scraping costs
nothing and produces the evidence store that must exist anyway. **Never graduate checkout before the cash gate.**

---

## 2. Agents during validation: bounded research, marketing and dry runs

The architecture is already the right shape for this and needs no new abstraction. `backend/llm_router.py`
exposes two tiers (`llm_router.py:51-53`): `fast` (llama-3.3-70b class, defaults to the NVIDIA NIM provider) and
`reasoning` (defaults to OpenRouter, which is the only configured provider selling Anthropic slugs). Provider
order prefers the free tier for high-volume turns: `LLM_FAST_PROVIDER` → nvidia, `LLM_REASONING_PROVIDER` →
openrouter (`:683-684`), verified live in the blueprint log entry of 2026-11-18 23:55.

**Routing rule: rout by whether the output needs judgement, not by how long the prompt is.**

| | `fast` tier | `reasoning` tier |
|---|---|---|
| Jobs | Transcribe call notes into rows, reformat, summarize a page we already chose, draft bulk copy variants, classify a refund reason into an existing vocabulary | Pick a niche, red-team an offer, compare licensors on ToS/MAP/channel conflict, draft an acceptance rule, interpret ratio creep, draft next-iteration hypotheses |
| Cost posture | High volume, near-zero cost (NIM free tier absorbs it) | Sparse, paid, and that is the point |
| Output | Enters a spreadsheet only after a human reads it | Becomes a proposal a human signs or discards |

One caveat on cost: `LLM_ROUTER_MAX_COST_MINOR` is an **unused constant**, not enforced (`llm_router.py:876`).
There is no LLM spend guard in the router today (no `llm_budget_usd_day` read in any `.py`), so "bounded" for LLM
calls is a habit, not a guardrail. Lane C's ratio discipline has no equivalent protection on the inference side.

### 2.1 Research workflows

Universal contract, applies to every row below: **agents draft, humans sign.** No agent output enters a buyer
list, a vendor shortlist, or an evidence pack until it passes a stated human acceptance test. Each row states the
tier, the input, the deliverable, and the human check that gates it.

**Lane A research**

| Task | Tier | Deliverable | Human check before use |
|---|---|---|---|
| Enumerate jurisdictions with downloadable permit feeds for one trade | `reasoning` | Table: jurisdiction, feed URL, format, cadence, volume est., access terms | Fetch each URL by hand; if it needs JS, delete the row |
| Enumerate candidate signals (permits, new-business registrations, listing changes, valuation-window) | `reasoning` | Ranked signal list with assembly cost and consent tier | Only Tier 1 (public record) advances in week 1 |
| Draft the QA accept/reject rule from the ten contractor calls | `reasoning` | Rule in `field, operator, threshold` form | Two independent passes over 20 records, ≥ 90% agreement |
| Assemble proof-pack text and normalize decision-maker fields | `fast` | Filled pack per record | Spot-check 5 of 50 against the source page |
| Shortlist retail buyers (contractors in the metro, contact routes) | `fast` then human | Prospect sheet | **Every row individually verified before any dial.** This is the TCPA boundary |
| Draft outreach copy for businesses | `fast` | Email/SMS/postcard copy variants | Read aloud; anything presuming prior consent is deleted |
| Hash-and-store source payloads | `fast` | sha256 + fetched payload per record | Verify hashing happens at fetch time (see trap) |

The evidence requirement is the one hard engineering constraint in lane A. Neither stack models evidence or
disputes today, which is why the launch plan's §5.4 migration (`engine.lead_evidence`, `engine.lead_disputes`) is
prerequisite to scraping at volume. The `source_hash` column exists precisely because a scraped page can quietly
change, and retroactive evidence is worthless in both a representment and a consent dispute. So an agent-assisted
scrape must write `{source_url, captured_at (UTC), source_hash, payload, consent_tier}` **at capture time**,
never later.

**Lane B research**

| Task | Tier | Deliverable | Human check before use |
|---|---|---|---|
| Scout SaaS with reseller/white-label programs in a vertical | `reasoning` | Candidates + wholesale/retail pair + implied markup | We compute markup ourselves; never accept the agent's arithmetic |
| Extract ToS/redistribution/MAP/SLA/egress/termination clauses | `reasoning` | Quote + section citation per clause, per candidate | **Open the ToS and check the quote exists verbatim** (see trap) |
| Flag MAP and channel-conflict risk | `reasoning` | Risk note per candidate | Founder decides whether the market is one the licensor lacks |
| Draft the tactic-permitted question to the licensor | `fast` | Email, ready to send | Founder sends it. "Probably fine" from the licensor kills the candidate |
| Map agency / MSP / practice segments per vertical | `reasoning` | Segment table: size, buying trigger, budget band | Crossed out by hand against reality |
| Page-hash watch on each licensor's ToS + pricing page | `fast`, scheduled | Change alert into `agent_logs` | Alert is informational only; a human re-reads the page |
| Normalize the 2-5 pilot client records for provisioning | `fast` | Clean intake sheet | Provisioning itself stays manual in phase 1 |

**Lane C research**

| Task | Tier | Deliverable | Human check before use |
|---|---|---|---|
| Find niches with recurring value, real pre-paywall effort, and multi-step cancellation | `reasoning` | Ranked niche shortlist | Artefact authenticity verified by using the product |
| Competitor teardown: entry price, renewal price, cancel path | `reasoning` | Per-competitor cancel-path trace | **We reproduce the cancel path ourselves.** Screenshots, not prose |
| Fraud / chargeback / compliance trap scan (descriptor, BIN, dunning, disclosure) | `reasoning` | Checklist, mapped to our funnel | Anything the agent asserts about a *rule* is treated as a hypothesis, not a fact |
| Draft offer angles, headline variants, risk-reversal copy | `fast`, batch | Variants tagged A/B/C | We run **one** variant at a time (see experiment discipline) |
| Classify incoming support and refund messages | `fast`, batch | Tagged vocabulary + quotes | Founder reads **every** message anyway in the manual phase. Classification assists review, never replaces it |
| Draft next-week hypothesis from last week's funnel data | `reasoning` | Hypothesis + suggested metric movement | Human picks the one thing to change |

### 2.2 Marketing and GTM workflows

Where the router plugs into what is already deployed, today, with no new build:

```bash
# Quick drafting, high volume, NIM free tier:
curl -s localhost:8000/llm/complete -d '{"prompt":"...","tier":"fast"}'
# Judgement work, OpenRouter reasoning tier:
curl -s localhost:8000/llm/chat    -d '{"prompt":"...","tier":"reasoning"}'
```

Both routes exist, are optional, and degrade with 503 rather than crashing when the router is dark
(`finance_agent.py:1696,1741`). `/llm/routing` reports the live tier/provider table, so "which model answered
this" is always checkable rather than assumed.

**Marketing backlog, one row per lane:**

| Lane | Task | Tier | Cadence | Gate |
|---|---|---|---|---|
| A | Outreach scripts for a business audience (postcard/email, QR to a landing page) | `fast` | Once per lead type | Sent only to businesses whose public-filing basis we can cite |
| A | Landing page promising a specific public record's existence | `fast` draft, `reasoning` review | Once | Copy must match what the pack actually contains |
| A | Weekly funnel retrospective + hypothesis | `reasoning` | Weekly | Human picks one change |
| B | Positioning one-liner per segment; onboarding email set | `fast` | Per segment | Claims checked against the licensor's actual capability |
| B | Pricing-objection handling, renewal-save copy | `fast` | As needed | **May not invent discounts.** MAP floor is a fact, not a suggestion |
| B | Monthly competitor pricing sweep into a comparison table | `reasoning` | Monthly | Prices confirmed on the vendor's own page |
| C | Funnel variants, A/B/C headlines | `fast` | Batch weekly | One change live at a time |
| C | Pre-billing notice and cancellation-confirmation emails | `reasoning` | Once | Disclosure must state renewal amount and date |
| C | Save-offer sequencing at cancel (pause / downgrade / 50% x 2 months) | `reasoning` | Once | Wording must never obstruct the cancel action |
| C | Read the week's refund reasons, propose the policy tweak | `reasoning` | Weekly | Policy changes are a human decision and a named tripwire |

**Correction, verified against source after the first draft of this document.** An earlier revision claimed
`funnel_sequences` carries `angle`, `headline`, `sequence_json` and `ad_spend_minor`. It carries **none of those**.
Its real columns (`funnel_worker.py:261`, created by inline DDL, not by `schema.sql`) are `visitor_key,
funnel_type ∈ (tripwire, trial), offer_id, landing_url, status, scheduled_at, opt_in_at, trial_days,
trial_amount_minor, rebill_amount_minor, currency, payload jsonb, last_error`, plus identity and timestamps.
`grep ad_spend_minor` across all `.sql` and `.py` returns **zero hits**. So there is no first-class column for an
ad angle, a headline, or spend: variant identity goes in `payload::jsonb` (free-form) or needs a migration, and
spend lives in `transactions` from gate-posted intents, rolling into `hustle_metrics.cost_minor`. "Spend stays
manual" therefore means exactly what it says: *a human spends it*, and there is no column pretending otherwise.

**ADJUSTMENT (new constraint):** a strict one-change-at-a-time rule is required for lane C during validation.
The reason is arithmetic, not preference: at the volumes the gate demands (~5,000 trials at 4% conversion to reach
200 paid conversions; splitting 400 trials four ways leaves ~30 events per arm, a 95% interval of roughly ±7
points against an effect of 3-5 points), a multi-arm test at validation volume produces a *story*, not a result.
Run sequential single-variable tests with a pre-registered primary metric and a fixed n, and do not let an agent
pick the winner until arms carry enough events to be compared.

Logging target for test results: a `hustle_metrics` row per day per lane from day 1, with variant identity in
`funnel_sequences.payload` or `trial_events.offer_id`. Note the rollup is keyed `(day, hustle)` with no
per-visitor dimension, so cohort metrics such as **month-2 retention must come from `trial_events`**, not from
any rollup. Report from **one** rollup
per lane and say which: `engine.hustle_metrics` (Stack A) and `engine.daily_metrics` (Stack B) are different
tables in different schemas, nothing reconciles them, and two dashboards quoting two numbers is how a lane looks
profitable while bleeding.

### 2.3 Other bounded assist tasks

- **Policy drafting:** dispute/replacement policy (A: 48-72h window, 2 strikes, credit-back), SLA/support
  boundaries and client comms (B), refund and cancellation policy (C). The chat agent's own system prompt
  (`finance_agent.py:2103`) is a decent tone template for policy language, because it already encodes "the model's
  description of itself must match what it can actually do". Drafts need a lawyer only where a human would pay
  for one; chargeback representment language does not need counsel, it needs specificity.
- **Sheet tidying:** normalize messy prospect and permit records into the schema `arbitrage_targets` expects
  (`hustle, source, external_ref, title, url, cost_minor, retail_value_minor, margin_pct, arbitrage_type,
  status, confidence, evidence, evidence_count, metadata`). Identity for lane A records: **(jurisdiction, filing
  id, parcel)**, which is also the durable dedupe key via `(hustle, source, external_ref)` UNIQUE. Do not lean on
  `seen()` for that: its state lives in `agent_logs` with no TTL, so a pruned log silently forgets.
- **Read-only probes and dry runs:** the pattern already exists in `funnel_worker.py:784` (probe, then execute),
  and `route_spend` always inserts a `transactions` row plus an `agent_logs` audit row before answering. So
  **agent-prepared intents may be posted to `/spend/gate` in dry-run now**, as rehearsal: they populate the
  ledger and the Telegram card path without moving money, and they exercise idempotency keys before they matter.
  While `is_dry_run()` is true, the gate answers `auto_approved/initiated/execute=false`: recorded, not executed.
- **Transcription to rows:** turn call notes into structured rows. Typed by an agent, judged by a human.
- **Telegram read surface:** `/status`, `/tick`, `/pending` and the plain-English chat agent are read-mostly and
  are safe during validation, including in the middle of the night, precisely because the writer tools are
  withheld by construction.

### 2.4 Strict boundaries: what stays human, even though an agent could do it

| Never delegated during validation | Mechanism, quoted from source |
|---|---|
| Spending money | `route_spend` (`finance_agent.py:779`) tiers: ≤ `$FINANCE_TIER1_MAX_MINOR`/100 auto-approved, ≤ tier2 queued for Telegram, above that **blocked** (`:864`). Spend above tier 1 is a button press by the owner |
| Live outbound to consumers | No worker dials. Lane A sells public-record facts and the buyer initiates, or we archive form+timestamp per submission at capture time (consent Tier 2). **Never** sell a scraped contact for a buyer to cold-call with no disclosure anywhere |
| Flipping dry-run | `engine.settings` key `engine.dry_run = false`, **not** `ENGINE_DRY_RUN=false`, which does nothing on the spend path (`:808` vs `:1060`). It fails closed on error, missing row and unparseable JSON: a DB blip means no spend, which is the intended direction |
| Enabling `SUPERVISOR_ENABLED` | Defaults OFF; two auto-deciding loops double-decide and **nothing in code or SQL enforces the single-writer rule** (convention only). The two loops are also asymmetric: standalone `supervisor.py` only *expires* pending rows, while `supervisor_tick()` (`:1263`) is the one that micro-approves ≤ $5 and escalates. Its sibling polls while the webhook is registered, producing the known 409 Conflict traceback. Resolve before phase 1, not during |
| Refunds, card issuance, cancellations | The chat agent's writers are exactly `approve_transaction` / `reject_transaction` (`:2133-2135`), gated by `can_write` (`:2760`). Keep them off. A model that confidently cancels a subscription is a refund we cannot claw back |
| Lane B licensing signature | Clause checklist (§1.B) verified by a human reading, including the "we asked in writing whether our tactic is permitted" line |
| Lane C policy changes | Any save-offer, retry, or dunning change is a human edit. `:94`'s discipline, honour refunds when a customer produces invoices, is cheaper than a dispute and only works if someone wants it to |
| Ratio interpretation | **ADJUSTMENT (new):** agents may *populate* ratios into `hustle_metrics` and *draft* the alert, but "should we keep spending" is never an agent call. Kill switches stay a human decision |

Practical hard caps while validating, chosen to be inside every existing guardrail rather than beside them:

- Lane C paid traffic: a daily figure set in the calendar, spent by hand. **≤ the card envelope, which is
  $50/day (`CARD_DAILY_CAP_MINOR=5000`) and $500/month (`CARD_MONTHLY_CAP_MINOR=50000`) per `agent_handle`.**
  Note that the issuer **fails open** (`CardIssuerClient.check_limit`, `:644`), so the local cap is the real cap.
- Ad-spend intents: post them to the gate as tier-2 even in dry-run, so a human sees `rationale` and
  `expected_roi_note` and the habit of writing real reasoning into the card is built during validation.
- LLM calls: prefer `fast` by default, since NIM's free tier is the point of the provider split; reserve
  `reasoning` for the judgement rows above. Cost is not enforced by the router.
- Before any lane pushes money through button taps: set `TELEGRAM_WEBHOOK_SECRET` (ships empty at
  `.env.example:94`, unset on the VPS, so unsigned mode is live). Acceptable while the surface is read-only,
  unacceptable once approvals are the money path. `WEBHOOK_SIGNING_SECRET` (`.env.example:102`) is a separate
  knob for Stripe-side webhooks; both need real values.

### 2.5 Trap table: agent failure modes that cost money in these three lanes

| Trap | Lane | Concrete consequence | Required control |
|---|---|---|---|
| Fabricated citations | B | A plausible ToS quote that is not in the ToS. We sign believing resale is permitted, and discover otherwise at scale | Verbatim quote + section citation, then a human opens the document |
| Wrong lead identity | A | Duplicate or wrong-parcel records sold as distinct leads; buyers experience it as junk | Dedupe on **(jurisdiction, filing id, parcel)**; the DB UNIQUE constraint is the net, not `seen()` |
| Hallucinated legal comfort | C | "This is compliant" where no rule is settled. The corpus's own limitation is that it could not corroborate some platform-policy boundaries from source | Treat legal assertions as hypotheses, and expect a structural argument, not a citation |
| Copy drift from product truth | A, B, C | Promise in the ad outruns the product in the pack/app/dashboard; that gap *is* the chargeback and the deception theory | Diff final copy against a written product-truth list before publish |
| Manufactured benchmark | A, B, C | A neat "typical conversion rate" or "usual markup" that is not real, becoming a fake threshold that gates a real decision | Thresholds come from this plan's tables and our own ledger, never from the model |
| Premature automation | all | An agent starts choosing instead of drafting, and a bad decision repeats on a schedule | The §1.4 gates are the only thing that moves an activity from assisted to automated |
| Silent schema drift | A, B | Rows the `arbitrage_targets` INSERT rejects (or accepts wrongly), discovered when someone finally runs the worker | Validate rows against the live table definition before claiming a batch is stored |
| Retroactive evidence | A | Re-fetching "the page as of today" after a dispute, when the page has changed | Capture hash + payload + UTC timestamp at fetch time, in the same step as the fetch |

---

## 3. Graduating from assisted validation to full agent automation

A step graduates when its decision is expressible as a rule **and** its lane passes the §1.4 gate. Automation
then means moving that step from "assists a human, human executes" to "executes within an envelope, human
reviews". Three things are true of every graduated step: it gets a registered handle, a unique job name, an
idempotency key, and an envelope it cannot exceed.

### 3.1 Target mapping (from launch plan §5.2, Stack A only)

| Lane | Handle | Stack | Owns after graduation | Forbidden | New build required |
|---|---|---|---|---|---|
| A | `worker:leadsource` (new) | A | Permit/record scraping, dedupe, evidence pack, `arbitrage_targets` rows | Consumer dialling, buyer billing | `lead_evidence` + `lead_disputes` |
| A | `worker:leadqa` (new) | A | Accept/reject by rule, replacement-window clock, buyer credit ledger | Charging a card | Billing stays manual/invoice in phase 1 |
| B | `worker:whitelabel` (exists, `agency_worker.py:193`) | A | Prospect intake, provisioning, renewal ticks, support routing | Price below the MAP floor | MAP floor as a `settings` key |
| B | `worker:licensor_watch` (new, read-only) | A | Upstream ToS/pricing page-hash capture | Any mutation | None; rows into `agent_logs` |
| C | `worker:funnel` (exists, `funnel_worker.py:162`) | A | Trial lifecycle, save-offer sequencing, pre-billing notices, dispute refunds | Card issuance, uncapped ad spend | Refund/retry policy config |
| All | `finance-agent` | A | Tier classification, envelope, ledger posts, Telegram cards | Scraping, copy, provisioning | nothing |
| All | `supervisor` | A | Alert cards, expiry sweep, heartbeat | Auto-approving (it only expires) | nothing |
| All | `telegram` | A | Read/report, owner-only decision relay | Initiating spend | nothing |

Lane A gets **new** handles rather than riding `worker:digital_arbitrage` because that worker is provably
discovery-only (zero references to `request_spend`/`SpendTicket`/`SpendGateway` in 1221 lines; it runs happily
with `FINANCE_AGENT_URL` unset). That is exactly the safety property a scraping loop wants. Do not dilute it by
bolting billing onto the same handle: the card envelope is keyed on `agent_handle`, so separate handles keep a
discovery runaway from eating the billing budget, and vice versa.

### 3.2 How the four collision primitives actually behave (each one, the way it breaks)

1. **Unique handles.** `agents.handle UNIQUE` (`0001_engine_core.sql:111`), and `request_approval` raises
   `unknown agent` (`:522`). Register new handles in `engine.agents` **before** deploying them, on both stacks;
   an unregistered agent cannot spend at all, which is the good failure mode.
   *Breaks when:* two lanes share a handle. `check_card_limit()` sums `engine.financial_ledger` filtered on
   `agent_handle` + `currency` over calendar day and month (`finance_agent.py:561-574`; cap entry types `charge,
   fee, expense, dispute, chargeback, transfer_out`), so lane C's traffic starves lane B's tooling spend from one
   envelope. Also note the live discovery worker's `HANDLE` is `worker:digital_arbitrage`
   (`digital_arbitrage_worker.py:376`), **not** `worker:arbitrage`; using the wrong name silently splits one
   budget into two phantom envelopes.
2. **Leases per (handle, job).** `WorkerStore.claim()` (`workers/common.py:496`) is a two-statement transaction
   with `for update skip locked`, logs `worker.lease.contended`, and fails closed (returns False on error).
   *Breaks now, and this is the load-bearing correction:* **zero production workers call it today** (all three
   use only `cursor_get`/`cursor_put`), so the horizontal-scaling guarantee in `EXECUTION_PLAN.md` §1.2 is not
   live. Either wire `claim()` into the new lane workers, or run exactly one replica per lane. Job names are
   free-form with no enforced convention; use `leadsource:permits:scan`, `whitelabel:traillock:scan`,
   `funnel:lifecycle`, one per lane-venture pair, because two ventures sharing a lane share a tick cadence.
3. **Two dedupe nets.** `seen()`/`mark_seen()` write `agent_logs` rows under `event='worker.item.seen'`
   (`common.py:608/629`), and `seen()` fails closed by returning **True** on error, so a DB blip skips an item
   rather than duplicating it. Plus `transactions_idempotency_uq` (`schema.sql:426`), `transactions_external_uq`
   (`:422`), `financial_ledger_external_uq` (`:317`). *Breaks when:* `agent_logs` is pruned, since seen-state has
   no TTL. Treat `arbitrage_targets (hustle, source, external_ref)` UNIQUE (`0001:285`) as the durable net. Lane
   C identity is the Stripe event id.
4. **Per-agent envelopes.** Local budget is evaluated first, so a runaway loop stops without touching the network,
   and the tighter of local and issuer wins. *Breaks when:* the issuer API is down and fails open (`:644`),
   leaving the local cap as the only protection. Default caps are tight on purpose: $50/day, $500/month.

### 3.3 Per-lane graduation mechanics

**Lane A, assisted → automated.**
Sequence: register `worker:leadsource` and `worker:leadqa` in `engine.agents`; land the §5.4 evidence/dispute
migration *before* scraping at volume; then automate discovery (read-only, free) while **billing stays manual on
invoice**. Checkout never graduates before the cash gate. The `worker:leadqa` accept/reject path is the rule the
ten contractor calls produced, and the gate demands ≥ 90% agreement between two independent passes on the same
records, because "it looks like a good lead" is not automatable while "permit filed ≤ 30 days ago, no GC on
record, valuation band X" is. The 48-72h replacement-window clock is what `lead_disputes.window_ends_at` is for;
counting `entry_type='dispute'` rows tells us the kill mechanism is running but cannot hold the proof that stops
it, which is why `risk_flag` alone is not enough.

**Lane B, assisted → automated.**
Phase 1 automation is renewal reminders, provisioning steps, prospect intake, and the read-only ToS page-hash
watcher: **$0 card spend**, tier-1 tooling only. MAP floor lives in `engine.settings` as a real key so a config
error fails visibly rather than silently repricing a client. Two handles must not be created on both stacks:
`worker:whitelabel` exists in `backend/workers/` *and* `backend/engine/workers/`, and both trees write the one
shared `engine` schema. Lane B graduates first because it moves no card money (our own Stripe, disclosed price,
real product), funds the other two, and its failure mode (an angry licensor) is recoverable by contract rather
than fatal.

**Lane C, assisted → automated, in two stages that must not be merged.**
Stage 1 (`phase 3`): lifecycle state, pre-billing notices, save-offer sequencing, **$0 charges issued by the
agent**; advance on 30 days with ratios flat and no false "cancelled". Stage 2 (`phase 4`): auto-refund on
request, dispute refunds, dunning-stop, micro cap only (≤ $5, the `supervisor_tick` micro-approval path at
`:1263`); **every tier-2 stays Telegram-approved**; advance on 60 days with fraud ratio ≤ 0.5% sustained. Lane C
goes last because its automated actions touch the one thing that cannot be undone: charging and un-charging
consumers on a processor relationship with a ratio ceiling.

**Cross-lane, before any of that.** Venture identity is still absent: no `engine.ventures` table, no `venture_id`
column in any `.py` or `.sql`; the only separation dimensions today are `hustle_kind` (three values),
`agent_handle`, and `account text default 'operating'`. Multiple B2B products cannot be separated in PnL, so the
`ventures` table plus `venture_id` lands **before** three lanes accumulate history (launch plan §5.4), including
the `hustle_metrics` PK change to `(day, hustle, venture_id)` and `add_metrics()`'s `ON CONFLICT` target in the
same commit. Also still owed before live money: the one-word `record_result(provider=` → `external_provider`
fixes (`engine/workers/arbitrage_worker.py:222`, `whitelabel_worker.py:90`, currently `TypeError` on every
execution), real values for `FINANCE_AGENT_URL` / `FINANCE_AGENT_TOKEN`, and `TELEGRAM_WEBHOOK_SECRET`.

### 3.4 The graduate checklist, per action

An action graduates only when all eight are true:

1. The decision is written as a rule a non-founder could apply identically (the §1.4 gate).
2. Its lane has hit cash, margin, safety and concentration numbers with evidence in the ledger.
3. Unique `agent_handle`, registered in `engine.agents` **before** deploy, on the stack that will run it.
4. Unique `job` name per lane-venture pair.
5. A stable `idempotency_key` and `external_id`/`external_provider`. Without these, a crash between approval and
   execution becomes a second charge. Existing funnel workers already use stable keys (`funnel:step:{id}`,
   `funnel:{identity_hash}`); `route_spend` checks `find_existing(...)` before classifying (`:800`) and re-checks
   after insert (`:849`), returning `already_decided` + `duplicate=true` on replay.
6. A card envelope in `settings`, tightest for lane C, lane B's tooling on its own handle.
7. Real `rationale` and `expected_roi_note` on every intent, so the Telegram card is a real decision rather than
   a number.
8. **It runs in dry-run first, then its first live action is human-approved.** Going live means
   `engine.settings.engine.dry_run = false`. Probe-then-execute, as `funnel_worker.py:784` already does.

Known behaviour to expect, not bugs to chase: `TxStore.expire_due()` (`supervisor.py:477`) mutates
`approval_status → 'expired'` **regardless** of `ENGINE_DRY_RUN`, so pending test intents older than the expiry
window disappear even in rehearsal. Know the window before judging a "lost" approval.

### 3.5 Rollout order (from launch plan §6, ordered by blast radius of the first automated mistake)

| Phase | Lane | Automated | Ceiling | Advance when |
|---|---|---|---|---|
| 0 | all manual, agents assisting per §2 | Nothing. Evidence + metrics populated by hand | $0 automated | §1.4 gates |
| 1 | **B** whitelabel | Renewals, provisioning steps, intake, ToS hash watcher | $0 card; tier-1 tooling ($30 auto on Stack A) | 30 days, zero double-provision, no MAP breach |
| 2 | **A** lead discovery | Scraping, dedupe, evidence hash, QA rule, pack assembly | Discovery free; billing stays manual/invoice | ≥ 90% QA-rule agreement, zero junk leads delivered |
| 3 | **C** lifecycle read-only | Trial state, pre-billing notices, save-offer sequencing | $0 charges issued by the agent | 30 days, ratios flat, no false "cancelled" |
| 4 | **C** billing actions | Auto-refund on request, dispute refunds, dunning-stop | Micro cap ($5); every tier-2 Telegram-approved | 60 days, fraud ≤ 0.5% sustained |
| 5 | scale-out | Replicate handles/jobs per venture, add `worker:buyer` for real checkout | Per-venture envelopes | Nothing colliding, **leases actually contending** |

End state, in one line: **agents run everything inside envelopes a human set, on rules a human wrote down, for
lanes a human proved with cash.** Phase 5's advance condition is the honest test that the machinery is real:
if two replicas have never produced a `worker.lease.contended` row, the collision control has never been tested,
and `EXECUTION_PLAN.md` §1.2's guarantee is still theoretical.
