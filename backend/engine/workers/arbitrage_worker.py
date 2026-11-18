"""Hustle B: digital arbitrage and scalping worker (zero inventory).

Tick shape: discover -> validate -> gate -> (wait for a human) -> buy and relist.

The validation gate is deterministic arithmetic, not a model opinion. Cost,
resale floor, required margin and minimum evidence are config, so a threshold
change is a settings edit rather than a redeploy of prompt text. The LLM is used
only where it helps (naming a product, reading a messy listing), never to decide
whether the maths clears.

The gate deliberately does not buy in the same tick it validates. A tier2
purchase waits for a human tap; the next tick picks it up once the approval
exists. That split is what makes the pause/resume semantics in Worker meaningful.
"""

from __future__ import annotations

import logging
from typing import Any

from ..finance_gatekeeper import fmt_minor
from ..worker_base import Worker

log = logging.getLogger("engine.worker.arbitrage")

# Validation thresholds. Read live from settings; these are the defaults.
DEFAULT_RULES = {
    "min_margin_pct": 25.0,     # gross margin floor before any fees
    "min_evidence": 2,          # independent sources confirming the resale floor
    "max_acquisition_minor": 25000,  # never let a single speculative buy exceed this
    "fees_pct": 15.0,           # marketplace + payment haircut off the sale price
}


class ArbitrageWorker(Worker):
    handle = "worker:arbitrage"
    hustle = "arbitrage"

    def rules(self) -> dict[str, float]:
        rules = dict(DEFAULT_RULES)
        for key in DEFAULT_RULES:
            value = self.db.setting(f"arbitrage.{key}", None)
            if value is not None:
                try:
                    rules[key] = float(value)
                except (TypeError, ValueError):
                    self.audit.error("arbitrage.bad_rule",
                                     f"setting arbitrage.{key}={value!r} is not a number; "
                                     f"using {DEFAULT_RULES[key]}")
        return rules

    async def work(self) -> dict[str, Any]:
        rules = self.rules()
        validated = self._validate_open(rules)
        ready = self._gate_validated(rules)
        resumed = self._execute_approved()
        return {
            "targets_validated": validated,
            "queued_for_approval": ready,
            "purchases_executed": resumed,
        }

    # -------------------------------------------------------------- validate
    def _validate_open(self, rules: dict[str, float]) -> int:
        """Score every discovered target. Fee-aware margin, not naive margin."""
        rows = self.db.read(
            """
            select id, title, acquisition_cost_minor, resale_floor_minor, evidence_count,
                   source, url, payload
              from engine.arbitrage_targets
             where status = 'discovered'
               and hustle = 'arbitrage'
             order by discovered_at
             limit 20
            """)
        scored = 0
        for r in rows:
            cost = r.get("acquisition_cost_minor")
            floor = r.get("resale_floor_minor")
            if not cost or not floor:
                # No numbers means nothing to compute, not a zero-margin reject:
                # leave it discovered and say why, so a scraper bug is visible.
                self.db.execute(
                    "update engine.arbitrage_targets set scoring_rationale = %s, "
                    "updated_at = now() where id = %s",
                    (f"waiting on data: cost={cost} floor={floor}", r["id"]))
                continue

            cost_i, floor_i = int(cost), int(floor)
            # Net proceeds after the marketplace haircut, then margin on cost.
            net = int(floor_i * (100 - rules["fees_pct"]) / 100)
            margin_pct = round((net - cost_i) / cost_i * 100, 2)
            evidence = int(r.get("evidence_count") or 0)

            reasons: list[str] = []
            if cost_i > rules["max_acquisition_minor"]:
                reasons.append(
                    f"cost {fmt_minor(cost_i)} over single-buy limit "
                    f"{fmt_minor(int(rules['max_acquisition_minor']))}")
            if margin_pct < rules["min_margin_pct"]:
                reasons.append(
                    f"net margin {margin_pct}% under {rules['min_margin_pct']}% floor")
            if evidence < int(rules["min_evidence"]):
                reasons.append(f"{evidence} evidence source(s), needs {int(rules['min_evidence'])}")

            if reasons:
                self.db.execute(
                    """update engine.arbitrage_targets
                         set status = 'rejected', expected_margin_pct = %s,
                             scoring_rationale = %s, decided_at = now(), updated_at = now()
                       where id = %s""",
                    (margin_pct, "rejected: " + "; ".join(reasons), r["id"]))
            else:
                self.db.execute(
                    """update engine.arbitrage_targets
                         set status = 'validating', expected_margin_pct = %s,
                             liquidity_score = %s, scoring_rationale = %s, updated_at = now()
                       where id = %s""",
                    (margin_pct, min(100.0, evidence * 25.0),
                     f"net margin {margin_pct}% after {rules['fees_pct']}% fees; "
                     f"{evidence} sources", r["id"]))
                self.audit.info("arbitrage.target_cleared",
                                f"{r['title']}: {margin_pct}% net margin on "
                                f"{fmt_minor(cost_i)}", target_id=str(r["id"]))
            scored += 1
        return scored

    # ------------------------------------------------------------------ gate
    def _gate_validated(self, rules: dict[str, float]) -> int:
        """Ask the finance gate to authorise each cleared target."""
        rows = self.db.read(
            """
            select id, title, acquisition_cost_minor, currency, resale_floor_minor,
                   expected_margin_pct, url, source
              from engine.arbitrage_targets
             where status = 'validating' and approval_id is null and hustle = 'arbitrage'
             order by expected_margin_pct desc nulls last
             limit 10
            """)
        queued = 0
        for r in rows:
            cost = int(r["acquisition_cost_minor"] or 0)
            if cost <= 0:
                continue
            floor = int(r.get("resale_floor_minor") or 0)
            roi = f"{fmt_minor(floor)} floor, {r['expected_margin_pct']}% net after fees"
            # Idempotency key is the target id, so a worker restart mid-gate
            # cannot queue the same ticket twice and buy it twice.
            intent = self.spend_sync(
                f"Buy {r['title']}", cost,
                rationale=f"sourced from {r['source']}; {roi}",
                source_url=r.get("url"), expected_roi=roi,
                vendor=r.get("source"),
                idempotency_key=f"arb-buy:{r['id']}",
                context={"target_id": str(r["id"])},
            )
            self.db.execute(
                "update engine.arbitrage_targets set approval_id = %s, updated_at = now() "
                "where id = %s",
                (intent.approval_id or None, r["id"]))
            queued += 1
        return queued

    def spend_sync(self, title: str, cost_minor: int, **kw: Any):
        """Call the async spend gate from sync code.

        The whole worker is synchronous because its I/O is database I/O through a
        blocking pool driver. Rather than pretend otherwise, the one async
        dependency (the spend gate) is driven from a private loop. That loop is
        not shared with anything holding a connection, so there is no cross-loop
        pool hazard.
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        coro = self.spend(title, cost_minor, **kw)
        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return asyncio.run(coro)

    # ------------------------------------------------------------- execution
    def _execute_approved(self) -> int:
        """Act on targets whose approval a human has since granted.

        Execution is intentionally a recorded intent here, not a real checkout:
        the Playwright checkout and the relist call are Module 4. What Module 1
        must prove is that an approval turns into exactly one ledger entry.
        """
        rows = self.db.read(
            """
            select t.id, t.title, t.acquisition_cost_minor, t.currency, t.purchase_ref,
                   a.id as approval_id, a.state, a.cost_minor, a.tier
              from engine.arbitrage_targets t
              join engine.approval_requests a on a.id = t.approval_id
             where t.hustle = 'arbitrage'
               and t.status in ('validating', 'approved')
               and a.state in ('approved', 'auto_approved')
             limit 10
            """)
        executed = 0
        for r in rows:
            if r.get("purchase_ref"):
                continue  # already bought; a duplicate here is a real loss
            cost = int(r["cost_minor"])
            intent = self.finance.request_spend(
                self.handle, f"Execute purchase: {r['title']}", cost,
                rationale=f"approval {r['approval_id']} granted; executing",
                hustle=self.hustle, vendor=None,
                idempotency_key=f"arb-exec:{r['id']}",
                context={"target_id": str(r["id"]), "original_approval": str(r["approval_id"])},
            )
            if not intent.execute:
                continue
            self.finance.record_result(
                intent, ok=True, description=f"acquired {r['title']}",
                provider="arbitrage_checkout", external_id=str(r["id"]),
            )
            self.db.execute(
                """update engine.arbitrage_targets
                     set status = 'completed', purchase_ref = %s, decided_at = now(),
                         updated_at = now()
                   where id = %s""",
                (f"sim:{r['id']}", r["id"]))
            self.audit.spend(f"acquired {r['title']} for {fmt_minor(cost)}",
                             target_id=str(r["id"]), approval_id=str(r["approval_id"]))
            executed += 1
        return executed


if __name__ == "__main__":
    ArbitrageWorker().run()
