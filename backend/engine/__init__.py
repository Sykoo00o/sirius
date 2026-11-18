"""Ghost X2 multi-agent grey-hat business engine.

Module 1: supervisor orchestration, Supabase-backed state, the financial ledger
and the HITL approval rails.

Money is always a BIGINT in minor units plus an ISO currency code. Floats appear
in this codebase only for LLM cost accounting, never in a path that moves cash.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
