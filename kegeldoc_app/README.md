# kegeldoc_app: kegel plans for men and women (a fork of the Lane C funnel)

A standalone pre-paywall funnel for **kegeldoc** ("sexual health"), forked from `funnel_app`
(Routine Lab). It keeps that app's whole backend, which is the part that took the work: the
Stripe checkout with charges off by default, the member dashboard with a stated renewal date, the
one-click cancel that executes before any save offer, the exit survey, the lifecycle emails, the
event ledger and the daily metrics export. What changed is the product layer: the quiz, the plan
builder, the copy, the claims guardrail and the page designs (ported from `kegel_site/`).

Own port (8091), own SQLite files, own `.env`, own systemd units (`deploy/kegeldoc_app/`). It
imports nothing from `funnel_app`, so either app can be deployed or stopped alone.

## Run it

```bash
cd kegeldoc_app
cp .env.example .env                     # then read it; one line decides whether money moves
../backend/.venv/bin/python -m uvicorn main:app --port 8091
```

Open `http://127.0.0.1:8091`. As with Routine Lab, `/health` is the pre-flight page (copy scan,
the four disclosures, an offline render of the artefact, the plan-table audit, billing
readiness), `/dev/responses` shows captured rows, and the whole journey is walkable in rehearsal.

Do not put `FUNNEL_*` lines in the repo-root `.env`: both apps read the same variable names and
fall back to that file, so a line there would configure Routine Lab and kegeldoc at once.

## Tests

```bash
../backend/.venv/bin/python -m pytest tests -q
```

`test_billing.py`, `test_telemetry.py` and `test_config_env_discovery.py` are Routine Lab's suites
running against this fork, with the walk in `conftest.py` replaced by the 38-screen men's flow.
`test_plan_builder.py`, `test_design_pattern.py` and the quiz parts of `test_funnel_flow.py` and
`test_copy_guardrails.py` are written for kegeldoc.

## What is different from Routine Lab

| Module | kegeldoc version |
| --- | --- |
| `quiz.py` | A bank of screens with per-visitor branching (`flow_for`): 38-39 for men, 35-36 for women, matched to the reference quiz's length. Kinds: single, multi, 1-5 scale, study fact, plan timeline. Information screens store `seen` when acknowledged, so resume and the forward-jump guard work unchanged. |
| `profile.py` | Answers to `Profile`, validated against the bank. A stale `birth_when` is ignored once `births` says No. |
| `plan_builder.py` | Deterministic plan: gentle start for pain, recent birth, recent prostate treatment or surgery; level from the experience, feel, squeeze and hold tests; a daily drill per goal (urge drill for frequent urgency, down-training breath for pain); a safety note whenever a clinician should come first; lifestyle notes. |
| `claims.py` | Same API, sexual-health vocabulary. Referral words ("doctor", "physiotherapist", "treatment" as history) are allowed; cure and treatment verbs, endorsement claims ("doctor-approved", "clinically proven"), condition names used as promises, drug comparisons and outcome guarantees are banned. |
| `funnel_copy.py` | kegeldoc wording. Same disclosure machinery: amounts from settings, four mandated sentences. |
| `llm_flavour.py` | Coach note brief and rules for a pelvic floor plan; output still scanned and discarded on a hit. |
| `templates/` | Landing, quiz, summary and preview ported from `kegel_site/` (body class `kd`, `static/kegeldoc.css`). Account, checkout and ops pages are Routine Lab's, recoloured through the Tailwind tokens and `static/app.css`. |

Unchanged from Routine Lab: `billing.py`, `billing_store.py`, `stripe_billing.py` (product name
defaults to "kegeldoc membership"), `telemetry.py`, `save_offer.py`, `mailer.py`, `storage.py`
(offer id `kegeldoc-kegel-v1`), `session.py` (cookie `kd_vid`), `config.py` (brand, port and
public URL defaults). Read `funnel_app/README.md` for how those work and why.

## Content rules

* Study screens cite real published sources by author, journal and year. No invented
  percentages and no reviews: the reference quiz uses both, and the guardrail bans their shapes.
* The name is a brand. The terms page says no doctor reviews individual plans, and the guardrail
  blocks any copy implying one does.
* The quiz asks about sexual and bladder health. The privacy page says those answers are treated
  as sensitive and used only to build the plan and to count completions.

## Metrics export

The shared `scripts/export_lane_c_metrics.py` reads Routine Lab by default. For kegeldoc:

```bash
FUNNEL_APP_DIR=kegeldoc_app python3 scripts/export_lane_c_metrics.py --day yesterday
```

`deploy/kegeldoc_app/kegeldoc-metrics.service` sets that variable for the daily timer.
