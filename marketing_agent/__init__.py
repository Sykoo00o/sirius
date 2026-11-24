"""Marketing agent (handle `marketing`): draft-only creative briefs, a compliance gate, and
read-only funnel and ad telemetry evaluation.

Standalone like `funnel_app`: stdlib only, no import of `backend/engine`, and the funnel's own
`claims.py` / `quiz.py` are loaded by file path. Every external action defaults off; see
`config.MarketingSettings` and `docs/MARKETING_AGENT_DESIGN.md`.
"""
