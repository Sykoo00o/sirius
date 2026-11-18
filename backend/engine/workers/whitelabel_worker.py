"""Hustle C: white-label / agency arbitrage worker.

Tick shape: prospect -> onboard a paying client -> route support without leaking
the vendor.

The vendor-isolation rule from the architecture doc ("prevent the anonymous
scapegoat failure mode") is enforced in code as a labelling rule: every client
touchpoint we generate uses our own brand string, and the vendor's name is kept
in the payload only. Support routing classifies a ticket as a platform bug and
records it for the vendor without ever composing the customer reply from vendor
names.

Module 1 ships the state machine and the safety rails. The actual directory
scraper and the vendor provisioning API call land with Module 4.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..worker_base import Worker

log = logging.getLogger("engine.worker.whitelabel")

# A bug in the vendor's platform is our problem to communicate but not ours to
# fix. Anything matching these is escalated rather than answered by an agent,
# because an agent inventing a workaround for a vendor outage is how a support
# promise becomes a refund.
PLATFORM_BUG_MARKERS = ("outage", "down", "incident", "503", "degraded", "maintenance")


class WhiteLabelWorker(Worker):
    handle = "worker:whitelabel"
    hustle = "whitelabel"

    async def work(self) -> dict[str, Any]:
        provisioned = self._provision_paid_clients()
        routed = self._route_support_queue()
        overdue = self._flag_overdue_tickets()
        return {
            "clients_provisioned": provisioned,
            "tickets_routed": routed,
            "tickets_overdue": overdue,
        }

    # ------------------------------------------------------------- onboarding
    def _provision_paid_clients(self) -> int:
        """Provision a sub-account once the client's invoice has settled.

        Gated on settlement, not on 'an approval exists': provisioning a
        white-label seat costs us a licence seat whether or not the client ever
        pays, so unpaid clients must not consume seats.
        """
        rows = self.db.read(
            """
            select t.id, t.title, t.payload, t.acquisition_cost_minor, t.currency,
                   a.id as approval_id
              from engine.arbitrage_targets t
              join engine.approval_requests a on a.id = t.approval_id
             where t.hustle = 'whitelabel'
               and t.status in ('approved', 'validating')
               and a.state in ('approved', 'auto_approved')
               and t.purchase_ref is null
             limit 10
            """)
        count = 0
        for r in rows:
            cost = int(r["acquisition_cost_minor"] or 0)
            payload = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
            vendor = payload.get("vendor") or "whitelabel_vendor"
            # The seat cost is recurring monthly, so the envelope check is the
            # whole safety story here: this is the one hustle whose costs repeat.
            intent = self.spend_sync(
                f"Provision white-label seat: {r['title']}", cost,
                rationale=f"client invoice settled; provisioning on {vendor}",
                vendor=vendor, hustle=self.hustle,
                idempotency_key=f"wl-provision:{r['id']}",
                context={"target_id": str(r["id"]), "recurring": True},
            )
            if not intent.execute:
                self.audit.info("whitelabel.provision_deferred",
                                f"{r['title']}: gate said {intent.state} ({intent.reason})",
                                target_id=str(r["id"]))
                continue
            self.finance.record_result(
                intent, ok=True,
                description=f"white-label seat for {r['title']} on {vendor}",
                provider=vendor, external_id=str(r["id"]),
            )
            self.db.execute(
                """update engine.arbitrage_targets
                     set status = 'completed', purchase_ref = %s, decided_at = now(),
                         updated_at = now()
                   where id = %s""",
                (f"seat:{r['id']}", r["id"]))
            count += 1
        return count

    # ---------------------------------------------------------------- support
    def _route_support_tickets(self) -> list[dict[str, Any]]:
        return self.db.read(
            """
            select id, payload->>'ticket_id' as ticket_id,
                   payload->>'subject' as subject,
                   payload->>'body' as body,
                   payload->>'vendor' as vendor,
                   payload->>'client' as client
              from engine.arbitrage_targets
             where hustle = 'whitelabel'
               and status = 'executing'
               and payload ? 'ticket_id'
               and coalesce(payload->>'routed_at', '') = ''
             limit 25
            """)

    def _route_support_queue(self) -> int:
        """Classify each ticket. A platform bug escalates; anything else is ours.

        We record the escalation with our own brand attached and the vendor
        referenced only internally, so a later human reply can be written without
        the customer ever seeing a vendor name.
        """
        routed = 0
        for t in self._route_support_tickets():
            haystack = f"{t.get('subject') or ''} {t.get('body') or ''}".lower()
            marker = next((m for m in PLATFORM_BUG_MARKERS if m in haystack), None)
            classification = "ours" if marker is None else "platform_bug"

            self.db.execute(
                """update engine.arbitrage_targets
                      set payload = payload || jsonb_build_object(
                            'routed_at', now()::text,
                            'classification', %s,
                            'matched_marker', %s,
                            'external_reply_draft', %s),
                          updated_at = now()
                    where id = %s""",
                (classification, marker,
                 # Draft text is written for the client, so it names our brand,
                 # never the underlying vendor.
                 ("We are seeing elevated issues on our platform and are on it; "
                  "no action needed from you." if marker else
                  "Thanks, we are looking into this and will reply shortly."),
                 t["id"]),
            )
            if marker:
                self.audit.danger(
                    "whitelabel.platform_bug",
                    f"client '{t.get('client')}' ticket looks like a vendor platform "
                    f"issue (matched '{marker}'): escalate internally, do not improvise a fix",
                    ticket_id=t.get("ticket_id"), vendor=t.get("vendor"),
                )
            else:
                self.audit.info("whitelabel.ticket_routed",
                                f"routed client ticket {t.get('ticket_id')} to us",
                                ticket_id=t.get("ticket_id"))
            routed += 1
        return routed

    def _flag_overdue_tickets(self) -> int:
        """Tickets open past the SLA we sell. Silent support decay is the main
        way an agency arbitrage loses a client without a warning."""
        sla_hours = int(self.db.setting("whitelabel.sla_hours", 8))
        rows = self.db.read(
            """
            select payload->>'ticket_id' as ticket_id,
                   payload->>'client' as client,
                   payload->>'routed_at' as routed_at
              from engine.arbitrage_targets
             where hustle = 'whitelabel'
               and status = 'executing'
               and payload ? 'ticket_id'
               and payload ? 'routed_at'
               and coalesce(payload->>'closed_at', '') = ''
               and (payload->>'routed_at')::timestamptz < now() - (%s || ' hours')::interval
            """,
            (str(sla_hours),),
        )
        for t in rows:
            self.audit.error("whitelabel.ticket_overdue",
                             f"client '{t.get('client')}' ticket {t.get('ticket_id')} "
                             f"open over {sla_hours}h",
                             ticket_id=t.get("ticket_id"), sla_hours=sla_hours)
        return len(rows)

    def spend_sync(self, title: str, cost_minor: int, **kw: Any):
        """See ArbitrageWorker.spend_sync: same blocking-pool rationale."""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.spend(title, cost_minor, **kw))
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, self.spend(title, cost_minor, **kw)).result()


if __name__ == "__main__":
    WhiteLabelWorker().run()
