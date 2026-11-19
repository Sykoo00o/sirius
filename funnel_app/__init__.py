"""Lane C Rank 4 standalone funnel: personalized 4-week routine plan generator.

Deliberately standalone, like `backend/finance_agent.py` and `backend/llm_router.py`:
stdlib + httpx + FastAPI, no import of the `engine` package, so this funnel can be
deployed, restarted and blown up without touching the B2B apps. Lane C rides the
FUNNEL processor identity only, and in Phase 1 it takes no money at all: the
checkout screen is a prototype, `FUNNEL_CHARGES_ENABLED` is false, and no recurring
charge exists to fail.
"""
