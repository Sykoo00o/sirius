# Marketing Agent — Design & Safety Spec

Handle: **`marketing`** (the row `EXECUTION_PLAN.md` §1.1 already reserves: owns "copy, funnel
experiments, ad-spend intents"; must never be "executing its own charge").
Code: [`marketing_agent/`](../marketing_agent/). Status: **scaffold, draft-only**. Nothing in it
can publish an ad, pause an ad, spend money or send a message.

Companion docs: [`AGENTIC_SETUP_BLUEPRINT.md`](AGENTIC_SETUP_BLUEPRINT.md) (switches, isolation,
logging) and [`REUSABLE_FUNNEL_BUSINESS_BLUEPRINT.md`](REUSABLE_FUNNEL_BUSINESS_BLUEPRINT.md)
(the funnel this agent markets).

---

## 1. What already exists, and what this agent inherits

| Existing control | Where | How the marketing agent uses it |
|---|---|---|
| `engine.dry_run` DB row (fails closed to `True`) | `backend/engine/db.py`, `backend/finance_agent.py` | Not read: the agent has no spend path. Any future ad-spend intent must go through `route_spend` (`backend/finance_agent.py`), whose tiers and the dry-run row already govern it. |
| `SUPERVISOR_ENABLED` (absent = off) | `backend/finance_agent.py` | Not read, never set. |
| `FUNNEL_CHARGES_ENABLED` (default false) | `funnel_app/config.py` | Not read; the agent never touches the funnel's billing. |
| Copy guardrail `claims.violations()` | `funnel_app/claims.py`, `kegeldoc_app/claims.py` | **Loaded by file path from the target app** and run on every brief string. One forbidden-terms list per niche, shared by site and ads. |
| Disclosure sentence `claims.price_line()` | same | Any ad that names a price must carry exactly this sentence, built from the funnel's own price settings. |
| Reject-to-fallback LLM rule | `funnel_app/llm_flavour.py` | Flagged copy is rejected, never rewritten to pass. |
| `trial_events` + `agent_logs` shape, partial unique `idempotency_key` | `funnel_app/telemetry.py` | Same `agent_logs` columns and dedupe index in the agent's own SQLite. |
| Kill metrics + ceilings | `scripts/export_lane_c_metrics.py` | **Run as a subprocess** (`--json --offline --csv <agent state>`), so the agent reads exactly the same counts and breaches the operator sheet uses, and never writes the operator's CSV. |
| Isolation conventions | `deploy/funnel_app/` | Own directory, stdlib only, own state dir, loopback-free (no server at all in v1). |

## 2. Safety posture (all defaults "do less")

Four independent switches, all off by default, read in `marketing_agent/config.py`:

| Env var | Default | Gates |
|---|---|---|
| `MARKETING_DRY_RUN` | `true` | Master switch. While true, every external action is recorded as an intent and nothing else happens. Unparseable values count as `true`. |
| `MARKETING_AUTO_PAUSE` | `false` | Whether a pause *recommendation* may become a pause *call*. |
| `MARKETING_PUBLISH_ENABLED` | `false` | Whether an approved asset may be uploaded/launched. |
| `MARKETING_LLM_ENABLED` | `false` | Whether a model may draft hooks. Off = deterministic templates only. |

`MarketingSettings.can_pause_ads()` / `can_publish()` return `True` only when dry-run is off **and**
the specific flag is on **and** `META_ACCESS_TOKEN` and `META_AD_ACCOUNT_ID` are present. In v1 even
an armed setting reaches `meta.UnarmedMetaClient`, which raises `NotBuilt`: there is no HTTP client
for ad mutations in the codebase. Arming is a code change plus an env change, deliberately.

Human gates:

* A brief can only reach `APPROVED` through `approve` with a named `--actor`; that writes an
  `is_audit=1` row to `agent_logs`. The deterministic gate can produce `FLAGGED` or
  `PASSED_CHECKS`, never `APPROVED`.
* An optional LLM policy classifier may **add** findings; it cannot clear a deterministic flag.

## 3. Deliberate departures from `web2app_marketing_agentic_setup_blueprint.md`

That blueprint was used for the module split and data model. These parts were **not** adopted:

| Blueprint item | Decision | Reason |
|---|---|---|
| Stealth headless scraping of Meta Ad Library with randomised delays | Not built. `adspy.py` ingests a JSON file (e.g. an export from Meta's official Ad Library API, or notes typed by hand). | Bot-evasion against a platform's terms; the official API exists. |
| Storing competitor transcripts to rewrite | Only abstract patterns are stored (`hook_type`, `visual_metaphor`, `emotional_driver`, `quiz_entry_angle`, `days_active`). A gate rule rejects hooks sharing an 8-word run with any ingested reference text. | Copying competitor copy is a copyright and policy problem. |
| "Specialist explainer" hooks with a photoreal urologist / doctor avatar | Blocked by the `impersonation` rule (doctor, physician, urologist, specialist, scrubs, clinician, nurse, dermatologist in visual prompts or speech). | Presents an AI actor as a medical professional; the funnel's own `claims.py` already bans "doctor"/"physician" in copy. |
| Static "quiz-mirror" cards with clickable-style buttons | Cards are generated from the funnel's **real** first questions and render options as a plain list; a `fake_ui` rule rejects "tap/click below", "button", "play button", "notification" in card text or prompts. The ad platform's own CTA button is the only button. | Non-functional UI in ad images is a deceptive pattern and is rejected by ad review. |
| Compliance "auto-rewrite & re-score until ≥ 0.92" loop | Not built. Flagged briefs stay `FLAGGED`; a new brief must be drafted. | Rewriting until a classifier passes optimises for slipping past review, the opposite of the funnel's reject-not-scrub rule. |
| Auto-pause and auto-graduate to CBO, every 6 hours | `meta.evaluate()` returns recommendations (`keep`, `recommend_pause`, `recommend_winner`, `insufficient_data`, `halt_all_scaling`) recorded as intents. No mutation. | Spend changes are human decisions (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §2.4). |
| "Every word must drive curiosity" / "YOU" stamina framings | Personal-attribute and outcome rules plus the target app's forbidden terms. | Matches the funnel's disclosure posture. |

The ad-level thresholds (thumbstop 25%, outbound CTR 1.0%, day-0 ROAS 0.50/0.90, impression and spend
floors) are kept as configurable recommendation rules.

**Funnel health overrides creative winners:** when the funnel's own export reports a dispute (≤ 1%) or
fraud (≤ 0.5%) ceiling breach, or prepaid share above 20%, every recommendation becomes
`halt_all_scaling` with the export's reason. A "winning" ad on a funnel that is tripping a kill
metric is not a winner.

## 4. Module map

```text
marketing_agent/
├── __init__.py
├── __main__.py          # python -m marketing_agent ...
├── config.py            # MarketingSettings.from_env(): four switches, paths, thresholds
├── loader.py            # load <app>/claims.py and <app>/quiz.py by file path (no app import)
├── offer.py             # the funnel's price terms from its config defaults + .env (price keys only)
├── policy.py            # deterministic rules: app claims, personal attribute, timeframe,
│                        #   impersonation, fake UI, price disclosure, "free", copied text
├── briefs.py            # CreativeBrief / CoreBody / Hook / QuizCard, strict shape validation,
│                        #   deterministic quiz-card + hook drafting from the real quiz
├── compliance.py        # audit_brief(): deterministic layer + optional classifier (add-only)
├── store.py             # SQLite: briefs, audits, assets, patterns, ad_telemetry, recommendations, agent_logs
├── funnel_metrics.py    # runs scripts/export_lane_c_metrics.py --json --offline, parses breaches
├── meta.py              # AdStats, evaluate(), fixture insights source, UnarmedMetaClient
├── adspy.py             # CompetitorPattern ingest from JSON (no scraping)
├── cli.py               # status | brief draft | brief audit | brief approve | patterns ingest |
│                        #   telemetry evaluate | funnel-health
└── tests/
```

Stdlib only. Tests: `uv run --with pytest --no-project python -m pytest marketing_agent/tests -q`.

## 5. Data model (`MARKETING_DB_PATH`, default `marketing_agent/data/marketing.sqlite3`)

| Table | Key columns | Idempotency |
|---|---|---|
| `creative_briefs` | `brief_id` (sha256 of the canonical payload, 16 hex), `app`, `niche`, `concept_name`, `payload_json`, `status` (`DRAFT`→`FLAGGED`/`PASSED_CHECKS`→`APPROVED`), `created_at`, `updated_at` | PK on content hash: the same draft twice is one row |
| `compliance_audits` | `brief_id`, `status`, `findings_json`, `layers`, `created_at` | appended per audit run |
| `asset_variants` | `variant_code` (`{brief}:{body}:{hook}` / `{brief}:{card}`), `brief_id`, `asset_type`, `status` (`PLANNED`), `storage_url`, `meta_ad_id` | unique `variant_code` |
| `competitor_patterns` | `source_ref` unique, `niche`, `days_active`, `pattern_json`, `reference_text` | unique `source_ref` |
| `ad_telemetry` | `meta_ad_id`, `window_end`, spend/impressions/3s/p50/outbound clicks/purchases/revenue | unique (`meta_ad_id`, `window_end`) |
| `recommendations` | `meta_ad_id`, `window_end`, `action`, `reason`, `executed` (always 0 in v1) | unique (`meta_ad_id`, `window_end`) |
| `agent_logs` | identical columns to `funnel_app/telemetry.py` (`agent_handle` default `marketing`) | partial unique `idempotency_key` |

Brief lifecycle events logged: `marketing.brief_drafted`, `marketing.brief_audited`,
`marketing.brief_approved` (audit), `marketing.intent_recorded` (any action skipped by dry-run),
`marketing.funnel_halt`.

## 6. Brief contract

```json
{
  "app": "funnel_app",
  "niche": "routine",
  "concept_name": "quiz-preview-baseline",
  "destination_url": "https://<FUNNEL_PUBLIC_URL host>/",
  "core_body": {"body_id": "body_01", "spoken_script": "...", "ui_broll_cue": "screen recording of the real quiz and Week 1 preview"},
  "hooks": [{"hook_id": "hook_01", "archetype": "question", "spoken_text": "...", "on_screen_caption": "...", "visual_prompt": "..."}],
  "static_quiz_cards": [{"card_id": "card_goal", "question_key": "goal", "header_text": "...", "options": ["..."], "background_prompt": "..."}],
  "price_disclosure": "$1 today for 7 days, then $39/month until you cancel"
}
```

Validation (`briefs.validate_payload`): required keys and types, 1 body, 1–10 hooks, 0–5 cards,
`destination_url` must start with the funnel's `FUNNEL_PUBLIC_URL`, each card's `question_key` must exist
in the app's `quiz.QUESTIONS`, and its `options` must be a subset of that question's option labels
(the card mirrors the real screen, never an invented one). `price_disclosure`, when present, must equal
`claims.price_line()` for the funnel's configured prices.

## 7. Plugging into existing agents and metrics

* **Funnel → agent (read-only):** `funnel_metrics.snapshot(day)` subprocesses the export script with
  `FUNNEL_APP_DIR` and a private `--csv` under the agent's state, so `lp_view → quiz_started →
  quiz_completed → trial_started → trial_paid` counts, `bin_prepaid_share_pct`, refund, dispute and fraud
  ratios are the operator sheet's numbers. Exit 2 / breaches → `halt_all_scaling`.
* **Agent → engine (future):** ad-spend intents go to `POST /spend/gate` on the finance agent as
  tier-2 with `rationale` and `expected_roi_note` (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §2.4). Not
  wired in v1.
* **Attribution:** `utm_content=<variant_code>` on `destination_url` is the join key between an ad and
  `trial_events` (the funnel does not record UTMs today — a funnel change, listed below).
* **LLM:** when enabled, hooks are drafted through `backend/llm_router.py` loaded by file path, same as
  `llm_flavour.py`; output passes the same gate. Not wired in v1 (`MARKETING_LLM_ENABLED` has no effect
  beyond `status`).

## 8. Graduation path (each step is a separate, reviewed change)

1. **Now:** draft + audit + human approve; manual upload by the operator; daily
   `funnel-health` and `telemetry evaluate` on exported Insights CSV/JSON.
2. Read-only Meta Insights client (GET only) behind `META_ACCESS_TOKEN`; still recommendations only.
3. Telegram alert of recommendations via the existing finance-agent notify path.
4. Pause-only mutation, behind `MARKETING_DRY_RUN=false` + `MARKETING_AUTO_PAUSE=true`, after 48 h of
   recommendations matching what a human would have done (the pasted blueprint's own Phase 3 advice).
5. Publishing stays human until the graduate checklist in `VALIDATION_AND_AGENT_ASSIST_PLAN.md` §3.4 is met.

## 9. Open items outside this scaffold

* The funnel does not capture `utm_*` on `lp_view`; ad-to-trial attribution needs that added to
  `funnel_app/telemetry.py` `detail_json` (a funnel change, not done here).
* No systemd unit yet; when a schedule is wanted, copy the metrics-timer pattern with
  `StateDirectory=marketing-agent` and `MARKETING_DB_PATH=/var/lib/marketing-agent/marketing.sqlite3`.
* Register handle `marketing` in `engine.agents` before it ever calls the finance gate.
