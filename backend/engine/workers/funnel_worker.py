"""Hustle A: negative-option / free-trial funnel worker.

Scope in Module 1: the compliance-critical half. The landing-page generator and
traffic optimiser come later; what cannot wait is the cancellation path, because
a negative-option funnel without working one-click cancellation is a chargeback
factory, and chargebacks cost the merchant account, not just the sale.

Each tick:
 1. Pull unprocessed Stripe events for the funnel merchant account.
 2. Mirror subscription state so the dashboard has MRR without a Stripe call.
 3. Guarantee a cancellation route exists for every live subscription, and
    alarm if it does not.
 4. Auto-refund a dispute under the configured threshold, to keep the ratio the
    card networks watch below their flags.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..finance_gatekeeper import fmt_minor
from ..worker_base import Worker

log = logging.getLogger("engine.worker.funnel")

# Events that change what we owe a customer, in processing order.
HANDLED_EVENTS = {
    "invoice.payment_succeeded",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "charge.dispute.created",
    "charge.dispute.closed",
}


class FunnelWorker(Worker):
    handle = "worker:funnel"
    hustle = "funnel"

    async def work(self) -> dict[str, Any]:
        events = self.db.read(
            """
            select id, event_type, external_id, payload
              from engine.webhook_events
             where provider = 'stripe'
               and processed_at is null
               and event_type = any(%s)
             order by received_at
             limit 25
            """,
            (list(HANDLED_EVENTS),),
        )
        handled = 0
        refunds = 0
        missing_cancel = 0

        for ev in events:
            try:
                outcome = self._handle_event(ev)
                handled += 1
                refunds += int(outcome.get("refunded", 0))
            except Exception as exc:
                # Mark the event failed rather than leaving it unprocessed: an
                # event that throws will throw identically forever, and an
                # unprocessed backlog hides the one event that is genuinely new.
                self.db.execute(
                    "update engine.webhook_events set status='failed', error=%s, "
                    "processed_at=now() where id=%s",
                    (str(exc)[:1000], ev["id"]),
                )
                self.audit.error("funnel.event_failed",
                                 f"{ev['event_type']} failed: {exc}", event_id=ev["id"])
                continue
            self.db.execute(
                "update engine.webhook_events set status='processed', processed_at=now() "
                "where id=%s", (ev["id"],))

        missing_cancel = self._audit_cancellation_paths()

        return {
            "events_handled": handled,
            "disputes_refunded": refunds,
            "subs_missing_cancel_route": missing_cancel,
        }

    # ---------------------------------------------------------------- events
    def _handle_event(self, ev: dict[str, Any]) -> dict[str, Any]:
        payload = ev["payload"] if isinstance(ev["payload"], dict) else json.loads(ev["payload"])
        obj = payload.get("data", {}).get("object", {})
        event_type = ev["event_type"] or ""

        if event_type.startswith("customer.subscription."):
            self._mirror_subscription(obj, event_type)
            return {}
        if event_type == "invoice.payment_succeeded":
            return self._record_revenue(obj, ev["external_id"])
        if event_type == "charge.dispute.created":
            return self._on_dispute(obj, ev["external_id"])
        if event_type == "charge.dispute.closed":
            self.audit.info("funnel.dispute_closed",
                            f"dispute {obj.get('id')} closed as {obj.get('status')}",
                            dispute_id=obj.get("id"))
            return {}
        return {}

    def _mirror_subscription(self, obj: dict[str, Any], event_type: str) -> None:
        """Upsert the subscription row. Trial end and cancel time are what the
        compliance engine and the MRR figure both depend on."""
        funnel_id = self._funnel_for(obj)
        if not funnel_id:
            # A subscription we cannot attribute to a funnel is real money with no
            # owner in the dashboard. Say so instead of dropping the row.
            self.audit.error("funnel.orphan_subscription",
                             f"subscription {obj.get('id')} matches no funnel",
                             stripe_id=obj.get("id"), metadata=obj.get("metadata"))
            return
        mrr = int(obj.get("items", {}).get("data", [{}])[0].get("amount", 0) or 0) \
            if obj.get("items", {}).get("data") else int(obj.get("plan", {}).get("amount", 0) or 0)
        self.db.execute(
            """
            insert into engine.funnel_subscriptions
              (funnel_id, stripe_customer_id, stripe_subscription_id, status,
               trial_ends_at, canceled_at, cancel_reason, mrr_minor, currency, raw, updated_at)
            values (%s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s,
                    %s::jsonb, now())
            on conflict (stripe_subscription_id) do update
              set status = excluded.status,
                  trial_ends_at = excluded.trial_ends_at,
                  canceled_at = excluded.canceled_at,
                  cancel_reason = excluded.cancel_reason,
                  mrr_minor = excluded.mrr_minor,
                  raw = excluded.raw,
                  updated_at = now()
            """,
            (
                funnel_id, obj.get("customer"), obj.get("id"), obj.get("status"),
                int(obj.get("trial_end") or 0) or None,
                int(obj.get("canceled_at") or 0) or None,
                obj.get("cancellation_details", {}).get("reason"),
                mrr if obj.get("status") in ("active", "trialing") else 0,
                (obj.get("currency") or "usd").upper(),
                json.dumps(obj, default=str),
            ),
        )
        if event_type == "customer.subscription.deleted":
            self.audit.info("funnel.subscription_canceled",
                            f"subscription {obj.get('id')} canceled "
                            f"({obj.get('cancellation_details', {}).get('reason', 'unknown')})",
                            stripe_id=obj.get("id"))

    def _funnel_for(self, obj: dict[str, Any]) -> str | None:
        """Resolve a Stripe subscription to a funnel row.

        Preference order is deliberate: explicit metadata is ours and exact; the
        price lookup depends on a Stripe read we may not have a key for.
        """
        meta = obj.get("metadata") or {}
        if meta.get("funnel_slug"):
            row = self.db.read_one(
                "select id from engine.funnels where slug = %s", (meta["funnel_slug"],))
            if row:
                return str(row["id"])
        price = None
        data = obj.get("items", {}).get("data") or []
        if data:
            price = data[0].get("price", {})
        if price and price.get("id"):
            row = self.db.read_one(
                "select id from engine.funnels where metrics->>'stripe_price_id' = %s",
                (price["id"],))
            if row:
                return str(row["id"])
        return None

    def _record_revenue(self, obj: dict[str, Any], event_id: str | None) -> dict[str, Any]:
        """Book collected cash. Amount is the invoice's paid total, in minors."""
        amount = int(obj.get("amount_paid") or obj.get("amount_due") or 0)
        if amount <= 0:
            return {}
        currency = (obj.get("currency") or "usd").upper()
        entry = self.db.call(
            "post_ledger_entry", "revenue", amount,
            f"stripe invoice {obj.get('number') or obj.get('id')} paid",
            currency, "operating", "funnel", None, None, "stripe",
            obj.get("payment_intent") or obj.get("id"),
            f"stripe:{event_id or obj.get('id')}:revenue",
            json.dumps({"invoice": obj.get("id"), "customer": obj.get("customer")}),
        )
        self.audit.spend(f"revenue {fmt_minor(amount, currency)} collected",
                         ledger_id=entry, invoice=obj.get("id"))
        return {"revenue_minor": amount}

    def _on_dispute(self, obj: dict[str, Any], event_id: str | None) -> dict[str, Any]:
        """Log the dispute, and buy it off if it is small enough that fighting
        costs more than the refund. Chargebacks damage the ratio, not just the
        balance, so a $12 dispute is worth refunding even when we are right."""
        amount = int(obj.get("amount") or 0)
        currency = (obj.get("currency") or "usd").upper()
        self.db.call(
            "post_ledger_entry", "dispute", amount,
            f"stripe dispute {obj.get('id')} ({obj.get('reason')})",
            currency, "operating", "funnel", None, None, "stripe", obj.get("id"),
            f"stripe:{event_id or obj.get('id')}:dispute",
            json.dumps({"reason": obj.get("reason"), "charge": obj.get("charge")}),
        )
        threshold = int(self.db.setting("funnel.auto_refund_dispute_minor", 10000))
        if amount > threshold:
            self.audit.danger("funnel.dispute_too_big_to_auto_refund",
                              f"dispute {fmt_minor(amount, currency)} exceeds the "
                              f"{fmt_minor(threshold)} auto-refund line; needs a human",
                              dispute_id=obj.get("id"), reason=obj.get("reason"))
            return {}

        intent = self.finance.preflight(
            self.handle, f"Auto-refund dispute {obj.get('id')}", amount,
            rationale=f"dispute reason {obj.get('reason')}; under auto-refund threshold",
            hustle=self.hustle, vendor="stripe",
            idempotency_key=f"dispute-refund:{obj.get('id')}",
        )
        if not intent.execute:
            # In dry-run the refund is simulated; the ledger dispute entry above
            # already happened, which is correct: the dispute is real either way.
            self.audit.info("funnel.dispute_not_auto_refunded",
                            f"dispute refund gate said {intent.state}: {intent.reason}",
                            dispute_id=obj.get("id"))
            return {}

        self.finance.record_result(
            intent, ok=True, description=f"dispute {obj.get('id')} refunded to protect ratio",
            external_provider="stripe", external_id=obj.get("id"), entry_type="refund",
            dedupe_key=f"stripe:{event_id or obj.get('id')}:refund",
        )
        self.db.execute(
            "update engine.funnel_subscriptions set disputed = true "
            "where stripe_subscription_id = %s", (obj.get("subscription"),))
        return {"refunded": 1}

    # ----------------------------------------------------------- compliance
    def _audit_cancellation_paths(self) -> int:
        """Count live subscriptions whose funnel has no cancellation route.

        Section 2 of the architecture doc calls one-click cancellation mandatory.
        A live trial pointing at a funnel with no cancel_url is the exact state
        that produces a 'I could not cancel' chargeback, so this is worth a
        heartbeat metric and an alarm row on every tick.
        """
        rows = self.db.read(
            """
            select count(*) as n
              from engine.funnel_subscriptions s
              join engine.funnels f on f.id = s.funnel_id
             where s.status in ('active', 'trialing')
               and (f.cancel_url is null or f.cancel_url = '')
            """)
        missing = int(rows[0]["n"]) if rows else 0
        if missing:
            self.audit.danger(
                "funnel.cancel_route_missing",
                f"{missing} live subscription(s) on a funnel with no one-click cancel URL",
                count=missing,
            )
        return missing


if __name__ == "__main__":
    FunnelWorker().run()
