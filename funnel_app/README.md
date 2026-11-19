# funnel_app: Lane C, Rank 4 (personalised routine generator)

A standalone pre-paywall funnel, kept separate from the B2B apps on its own port (8090), its
own SQLite file and its own deploy. It shares code with nothing that bills.

Phase 1 scope, and nothing beyond it: a landing page, a twelve-question quiz, a genuine
week-one artefact shown *before* the card details, and a checkout that rehearses without
charging. `FUNNEL_CHARGES_ENABLED` defaults to false; `SUPERVISOR_ENABLED` is untouched and
no ad spend is connected.

## Run it

```bash
cd funnel_app
cp .env.example .env                     # then read it; one line decides whether money moves
../backend/.venv/bin/python -m uvicorn main:app --port 8090
```

Then open `http://127.0.0.1:8090`. Three pages are worth reading before anything else:

* `/health`, the pre-flight page: the copy scan, the four mandated disclosures, an offline
  render of the artefact, the meal-template audit, and live event counts.
* `/dev/responses`, the captured rows. Linked from no funnel page on purpose.
* `/policy/refund`, which must match the Phase 0 spec wording, not improvise it.

## Tests

```bash
../backend/.venv/bin/python -m pytest tests -q
```

`tests/test_copy_guardrails.py` is the one that protects the licence: no claim may reach the
page, and every disclosure must. `tests/test_funnel_flow.py` walks the funnel twice, once with
each legal form encoding, and asserts on the rendered artefact rather than status codes alone.

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

## What is deliberately not here

No charges, no Stripe, no ad pixels, no scheduler, no account system. The quiz has no login
because a mandatory account before the artefact would defeat the thing being tested.

## Deploy notes

SQLite is right for Phase 1: one writer, no network hop, and the row shape already matches
`trial_events` so the move to Postgres is a migration rather than a redesign. Turn `secure`
cookies on with TLS once the funnel has a certificate (`session.py`, `cookie_kwargs`); they
default off so local HTTP works.

## Three metrics Phase 1 cannot produce

Named here so nobody reads the funnel as complete. `storage.py` counts visitors per stage and
nothing else, and these three need a processor webhook, which is Phase 2 work:

* **Fraud-not-chargeback rate.** Needs the processor's risk verdict on the trial charge.
* **Refund count.** Needs refund events, which do not exist while charges are off.
* **Prepaid/virtual-card BIN share.** The single most important number for this niche, since a
  virtual-card survivability below roughly 50 percent kills the offer regardless of how the
  funnel performs, and it needs the card metadata a payment token carries.
