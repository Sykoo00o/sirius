"""Agency worker (Hustle C, schema.sql stack): client onboarding and support routing.

Name vs. deployment
-------------------
docker-compose calls this service `worker-whitelabel`. The engine stack has its
own `engine/workers/whitelabel_worker.py`, built on migrations/0001_engine_core.sql.
Two files with near-identical names write two different schemas, so this module
keeps the CLAUDE.md name (agency_worker.py) and the two stay distinguishable at a
distance. The HANDLE stays 'worker:whitelabel' and the hustle stays 'whitelabel'
because that is what the dashboards, the card envelope, and hustle_metrics know.

Three jobs, one loop
--------------------
1. Webhook intake. Client onboarding events (Stripe invoice paid, CRM "new sale",
   helpdesk "new ticket") arrive from places that cannot be trusted to deliver
   once, or in order, or at all after a bad night. They land in this worker's own
   queue table with a dedupe key and are drained on the tick.
2. Onboarding. A settled invoice entitles the client to a white-label sub-account.
   Provisioning calls the vendor API, which costs money, so it goes through the
   spend gate before the call, not after.
3. Support routing. Classify a ticket, answer what can be answered with our own
   brand string, escalate what cannot. The escalation is recorded without ever
   putting the vendor's name in front of the client.

Webhook intake lives in Postgres, not in an HTTP endpoint
---------------------------------------------------------
The tempting design is a FastAPI route that calls the vendor synchronously. That
loses events three ways: the pod restarts mid-request, the vendor API is down and
Stripe's retry window closes first, or the endpoint takes nine seconds and the
processor marks the webhook failed. So ingestion is only ever an insert: the
signature check has to be fast because the sender will not wait, and everything
expensive and fallible is deferred to a drain that retries with backoff, survives
restarts, and can be replayed from the table.

The consequence worth stating: `receive_webhook()` is the ONLY function here that
runs inside a request, and it performs no network call to any third party. If a
future edit adds an httpx call to it, this whole paragraph is the reason to take
it back out.

Dedupe is a unique index, not a SELECT
--------------------------------------
`on conflict do nothing returning id` rather than check-then-insert. Two deliveries
of the same webhook arriving concurrently both pass a pre-check, and "onboard this
client twice" means two vendor sub-accounts billed to us. Same reasoning as
funnel_worker.enqueue().

Money, and why provisioning asks first
--------------------------------------
A provisioned seat is a recurring cost we commit to for the client's lifetime,
which is a different thing from sending an SMS. So `provision_account` requests
the spend BEFORE calling the vendor and does not call the vendor at all unless the
gate says execute. A queued ticket becomes a "not yet provisioned" state, which is
recoverable; a vendor account created against a rejected charge is a refund ticket
and an angry reseller.

Vendor isolation, enforced in code
----------------------------------
The doc's "anonymous scapegoat" failure mode is a labelling rule, applied in two
directions:
* Outbound to the client: replies compose from AGENCY_BRAND only. The vendor's
  name never reaches a client-visible string (assert_guard below).
* Outbound to the vendor: escalations carry the technical facts and no client
  branding, so we do not hand a vendor an advertisement while asking them to fix
  an outage.
And one internal rule: a platform bug is escalated, never answered, because an
agent inventing a workaround for a vendor outage is how a support promise becomes
a refund.

Metric mapping
--------------
hustle_metrics has two columns reserved by name for this hustle, and this worker
writes exactly those two plus money:

    clients_onboarded  = sub-accounts provisioned (flow, per tick)
    support_tickets    = tickets received (flow, per tick)
    revenue_minor      = settled renewal amounts
    cost_minor         = seat costs the spend gate released
    active_subscriptions / cancellations = provisioned accounts / payment failures
    mrr_minor, new_subscriptions          = never written by this worker

Every key is in common.WorkerStore.add_metrics' additive allow-list, which is what
makes that method the only legal way to write these. Three points about the mapping:

* mrr_minor and new_subscriptions are deliberately untouched. They are the
  negative-option funnel's subscription columns and this hustle's row would be
  adding a different definition of "subscription" to them. The revenue signal a
  reader wants from the agency row is MRR-per-client, which is
  clients_onboarded x the client table's mrr_minor, not this column.
* `escalations` is a count this worker produces that hustle_metrics has no column
  for. It is logged and dropped rather than coerced into dispute_count (a
  chargeback-ratio input): a vendor outage is not a payment dispute. The escalations
  live in agency_escalations, where they are countable.
* `cancellations` is overloaded on purpose. hustle_metrics documents it as a funnel
  trial cancellation; the agency uses it for "payment failed, MRR at risk". The
  semantics differ, and the alternative, leaving a client whose card just failed out
  of every daily counter, is worse. Read it as "subscriptions lost today" on the
  whitelabel row and the meaning is exact on both hustles.

Env (all read via common.load_dotenv; no secrets hardcoded)
-----------------------------------------------------------
WHITELABEL_BRAND                Display name the client sees. Default 'Our Platform'.
WHITELABEL_VENDOR_API_URL       Vendor provisioning + escalation base URL. Unset
                                means nothing can be provisioned, which is reported
                                loudly at boot rather than logged at debug.
WHITELABEL_VENDOR_API_TOKEN     Bearer token for the above.
WHITELABEL_VENDOR_NAME          The name the CLIENT must never see ('HostedHQ',
                                'Zendesk'). This is the string assert_no_vendor_leak
                                tests outbound text against, so leaving it unset
                                leaves the white-label isolation rule unenforced. It
                                is never sent TO the client and never sent to the
                                vendor either.
WHITELABEL_SEAT_COST_MINOR      Recurring cost per provisioned seat, minor units.
                                Default 1900 (USD 19.00).
AGENCY_WEBHOOK_SECRET           HMAC-SHA256 secret for receive_webhook(). Unset
                                accepts signatures as valid and says so once.
AGENCY_ESCALATION_WEBHOOK_URL   Optional. Where vendor escalations are posted so
                                the operator's Telegram/Slack relay can pick them
                                up (Module 4 owns that relay).
AGENCY_ESCALATE_MIN_PRIORITY    Default 'high'. Lower priorities are logged and
                                auto-answered instead of pinging a human.
AGENCY_DEFAULT_PLAN             Plan slug provisioned when an event omits one.
AGENCY_TICK_RETENTION_DAYS      Closed-ticket cleanup horizon. Default 30.
AGENCY_TICK_SECONDS / WORKER_TICK_SECONDS   Default 60.
WORKER_BATCH_SIZE               Events drained per tick. Default 25.
WORKER_FRESHNESS_HOURS          Default 6.0, same premise as the spend freshness
                                window: an event this old acts on a market that
                                has moved.
WORKER_SCHEMA                   Installed schema, validated by common.py.

Run:  python -m workers.agency_worker [--once] [--dry-run]
      python -m workers.agency_worker --simulate-invoice --client acme.example
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

try:  # optional at import time so `--help` works without the driver
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised only in a stripped image
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

try:
    from .common import (
        Lead,
        SpendGateway,
        SpendGatewayError,
        SpendTicket,
        WorkerStore,
        _float_env,
        _int_env,
        build_database_url,
        build_gateway,
        env,
        fmt_minor,
        load_dotenv,
    )
except ImportError:  # pragma: no cover - `python agency_worker.py` from this dir
    from common import (  # type: ignore[no-redef]
        Lead,
        SpendGateway,
        SpendGatewayError,
        SpendTicket,
        WorkerStore,
        _float_env,
        _int_env,
        build_database_url,
        build_gateway,
        env,
        fmt_minor,
        load_dotenv,
    )

log = logging.getLogger("workers.agency")

HANDLE = "worker:whitelabel"
#: engine.hustle_kind has exactly three values and this hustle is 'whitelabel'.
HUSTLE = "whitelabel"

# ---------------------------------------------------------------------------
# Queue kinds
# ---------------------------------------------------------------------------

CLIENT_SOURCE = "client_onboarding"
SUPPORT_SOURCE = "support_ticket"
RENEWAL_SOURCE = "billing_renewal"
#: Drain order. Onboarding first because it is the one branch that bills us, so it
#: should not sit behind a flood of tickets during a vendor incident.
DRAIN_ORDER = (CLIENT_SOURCE, RENEWAL_SOURCE, SUPPORT_SOURCE)

#: Ticket classifications, also the `route` column values on the ticket table.
ROUTE_AUTO = "auto_reply"
#: Bug in the vendor's platform: our problem to communicate, not ours to fix.
ROUTE_PLATFORM_BUG = "platform_bug"
#: Needs a human but is not on fire: billing oddities, scope creep, angry prose.
ROUTE_HUMAN = "needs_human"
#: Resolvable from the runbook with our own wording and no vendor contact.
ROUTE_KNOWN_ANSWER = "known_answer"
#: Nothing matched. Not a terminal state: unresolved tickets are re-read by the
#: next tick rather than silently parked at auto_reply.
ROUTE_UNCLASSIFIED = "unclassified"

#: Matched against the ticket subject and body, lower-cased substring. A platform
#: bug is escalated rather than answered: see the module docstring.
PLATFORM_BUG_MARKERS = ("outage", "down", "incident", "503", "degraded",
                        "maintenance", "not loading", "broken", "error when")
#: Urgency markers, applied after the bug test so an outage trumps a threat.
URGENT_MARKERS = ("urgent", "asap", "refund", "cancel", "emergency",
                  "critical", "not working", "help!")
#: Phrases we can answer verbatim, mapped to the runbook article they cite.
KNOWN_ANSWER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("password reset", "kb/reset-password"),
    ("forgot my password", "kb/reset-password"),
    ("how do i invite", "kb/invite-team-members"),
    ("change my plan", "kb/change-plan"),
    ("download invoice", "kb/invoices"),
    ("where is my report", "kb/reports"),
)

#: Priorities, worst-last. Comparisons are positional against this tuple, so an
#: unknown value behaves as the lowest priority rather than raising.
PRIORITIES = ("low", "normal", "high", "urgent")

#: Placeholder event id for a webhook that has not been inserted yet, used while a
#: client identity is still unknown.
PROVISIONAL_EVENT_ID = -1


#: Retries per event before it is parked as 'failed'. The vendor API and the mail
#: relay both blip for minutes; three drains at increasing backoff covers a blip,
#: and past that the cause is structural and needs reading, not waiting.
MAX_ATTEMPTS = 3

#: Auto-reply texts. Every one of these is client-visible, so none may contain a
#: vendor name: they are written to be true regardless of who runs the platform.
AUTO_REPLY_TEXT = {
    ROUTE_KNOWN_ANSWER: "Thanks for reaching out. Here is how to sort that on "
                        "{brand}: {answer}. Reply here if it is still not right.",
    ROUTE_AUTO: "Thanks for contacting {brand} support. We have logged your "
                "request and will follow up shortly.",
}

DEFAULT_BRAND = "Our Platform"
DEFAULT_SEAT_COST_MINOR = 1900
DEFAULT_ESCALATE_MIN_PRIORITY = "high"
DEFAULT_RETENTION_DAYS = 30


class VendorError(RuntimeError):
    """The vendor API refused or could not be reached. Carries retryability."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class WebhookAuthError(RuntimeError):
    """Signature missing or wrong. Caller answers 401, never 500."""


class SignatureNotConfigured(WebhookAuthError):
    """No secret configured, so nothing can be verified."""


class ClaimRaceError(RuntimeError):
    """We fetched rows we then failed to mark claimed, so we own nothing.

    Raised rather than logged because a claim that does not hold does not fail
    visibly: every queue drains as no-ops, every metric reads zero, and the worker
    looks healthy. A tick that raises at least reaches the error streak that pages
    someone after three in a row.
    """


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _ident(name: str) -> str:
    """Validate a schema name before interpolating it into DDL.

    Mirrors the guard in funnel_worker.py: `create table if not exists {s}` takes
    an identifier, not a bindable value, so an unvalidated env-supplied name is an
    injection path. Validated here independently of common.py's WORKER_SCHEMA check
    because a second validator on the string that reaches SQL text is cheap.
    """
    cleaned = name.strip().lower()
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", cleaned):
        raise ValueError(f"unsafe schema name {name!r}")
    return cleaned


def assert_no_vendor_leak(text: str, vendor: str) -> None:
    """Refuse a client-visible string that names the underlying vendor.

    Not a style rule. The white-label margin depends on the client believing the
    software is ours, and the failure this guards is the quiet one: a template that
    reads "the {vendor} status page" because a developer filled in a placeholder.
    Logging the leak is not enough because by then the string exists; this raises,
    so the send does not happen and the ticket retries.
    """
    if vendor and vendor.lower() in text.lower():
        raise VendorError(
            f"client-visible text names the vendor ({vendor!r}); refusing to send",
            retryable=False)


def norm_priority(raw: Any) -> str:
    """Clamp any caller's priority into PRIORITIES.

    An unknown spelling becomes 'normal' rather than raising: priority comes from
    third-party ticket payloads, and rejecting a ticket because Zendesk called it
    'p2' would drop customer mail over a vocabulary mismatch.
    """
    value = str(raw or "").strip().lower()
    if value in PRIORITIES:
        return value
    aliases = {"p0": "urgent", "p1": "high", "p2": "normal", "p3": "low",
               "highest": "urgent", "medium": "normal", "critical": "urgent",
               "low": "low", "none": "low"}
    return aliases.get(value, "normal")


def priority_rank(priority: str) -> int:
    """Position in PRIORITIES. Unknown sorts lowest so it cannot trigger a page."""
    try:
        return PRIORITIES.index(priority)
    except ValueError:
        return 0


def norm_currency(raw: Any) -> str:
    """Upper-case 3-letter currency code, falling back to USD.

    Deliberately the same shape as funnel_worker.norm_currency rather than an
    import from that module: workers importing each other would let the funnel's
    restart behaviour creep into this one through a refactor.
    """
    cleaned = re.sub(r"[^A-Za-z]", "", str(raw or ""))[:3].upper()
    return cleaned if len(cleaned) == 3 else "USD"


def utc_now() -> datetime:
    """Naive UTC, for binding into a `timestamp with time zone` column.

    Every read here normalises with `if tzinfo is None: replace(tzinfo=UTC)`, so the
    driver is happy either way, but handing it naive-UTC makes each bind an
    unambiguous instant that lands on the server exactly as given instead of being
    interpreted through the session TimeZone. common.py calls the same value `now`
    and hands it to psycopg raw (claims.py:496) with the same assumption; this just
    names it so the assumption is stated once instead of implied three times.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def utc_when(raw: Any, fallback: datetime) -> datetime:
    """Parse a sender's timestamp to naive UTC, or `fallback`.

    unix epoch, 'Z'-suffixed ISO, and offset ISO all accepted because both spellings
    are extremely common and picking one rejects half the world. Naive out, to match
    utc_now() and therefore utc_next().
    """
    moment: datetime | None = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            moment = datetime.fromtimestamp(float(raw), UTC)
        except (OverflowError, OSError, ValueError):
            moment = None
    else:
        text = str(raw or "").strip()
        if text:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                moment = None
    if moment is None:
        return fallback
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(tzinfo=None)


def clip(raw: Any, width: int) -> str | None:
    """Trim to `width`, mapping empty to NULL.

    The `or None` half matters more than the trim: an empty company string from a
    thin CRM payload would otherwise store '' and defeat the COALESCE in
    upsert_client's update branch, which keeps an existing value only when the
    incoming one is NULL. An empty string is not NULL, so a resend with a blank
    field would wipe a company name a human had corrected by hand.
    """
    if raw in (None, ""):
        return None
    return str(raw)[:width]


def mask_email(raw: Any) -> str:
    """Redact an email for log lines: 'acme.example' keeps recognisability, drops PII.

    agent_logs is the most widely-read table in this schema and it is where the
    per-client trail lives, so it gets the handle, not the mailbox. The full value
    stays in the client row's payload, which is where a human resolving a billing
    question is entitled to find it.
    """
    value = str(raw or "").strip()
    if "@" not in value:
        return value[:40]
    local, _, domain = value.partition("@")
    return f"{local[:1]}***@{domain[:40]}" if local else f"@{domain[:40]}"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class AgencyWorker:
    """Client onboarding and support dispatcher for Hustle C."""

    HANDLE = HANDLE
    HUSTLE = HUSTLE

    JOB_QUEUE = "agency_queue_tick"
    JOB_METRICS = "agency_metrics_refresh"

    def __init__(self, database_url: str, gateway: SpendGateway, *,
                 tick_seconds: int = 60, batch_size: int = 25,
                 freshness_hours: float = 6.0, dry_run: bool = False) -> None:
        self.store = WorkerStore(database_url)
        self.database_url = database_url
        self.gateway = gateway
        self.tick_seconds = max(5, int(tick_seconds))
        self.batch_size = max(1, int(batch_size))
        self.max_age_seconds = int(freshness_hours * 3600)
        self.dry_run = bool(dry_run)
        self._errors = 0
        self._stop: Any = None

        self.schema = _ident(env("WORKER_SCHEMA", "engine"))
        self.queue_table = f"{self.schema}.agency_webhook_events"
        self.client_table = f"{self.schema}.agency_clients"
        self.ticket_table = f"{self.schema}.agency_support_tickets"
        self.escalation_table = f"{self.schema}.agency_escalations"

        self.brand = env("WHITELABEL_BRAND") or DEFAULT_BRAND
        self.default_plan = env("AGENCY_DEFAULT_PLAN") or "starter"
        self.seat_cost_minor = _int_env("WHITELABEL_SEAT_COST_MINOR",
                                        DEFAULT_SEAT_COST_MINOR)
        self.retention_days = max(1, _int_env("AGENCY_TICK_RETENTION_DAYS",
                                             DEFAULT_RETENTION_DAYS))
        self.escalate_min = norm_priority(env("AGENCY_ESCALATE_MIN_PRIORITY")
                                          or DEFAULT_ESCALATE_MIN_PRIORITY)
        # Compare ranks, not strings: 'high' >= 'normal' is a lexicographic answer
        # and it is wrong for every pair except the equal ones.
        self.escalate_rank = max(1, priority_rank(self.escalate_min))

        self.vendor_url = env("WHITELABEL_VENDOR_API_URL").rstrip("/")
        self.vendor_token = env("WHITELABEL_VENDOR_API_TOKEN")
        # The only place the vendor's real name is allowed to live. Left empty by
        # default, which makes the leak guard inert until an operator names the
        # vendor: a guard that silently passes everything would be worse than no
        # guard, so run_forever() warns when a brand is set with no vendor to hide.
        self.vendor_name = env("WHITELABEL_VENDOR_NAME")
        self.escalation_url = env("AGENCY_ESCALATION_WEBHOOK_URL")
        self.http_timeout = httpx.Timeout(20.0, connect=5.0)
        # One client-secret-shaped string per process, used as the HMAC key when no
        # operator configured one. Keeps --simulate-invoice end-to-end runnable.
        self.webhook_secret = env("AGENCY_WEBHOOK_SECRET")

    def _client_visible(self, text: str) -> str:
        """Fill the brand placeholder and enforce the isolation rule."""
        filled = text.format(brand=self.brand)
        assert_no_vendor_leak(filled, self.vendor_name)
        return filled

    # ------------------------------------------------------------------ schema

    def ensure_tables(self) -> None:
        """Create the four tables this worker owns. Idempotent.

        Owned here rather than in supabase/schema.sql because that file is a
        migration the operator applies deliberately, while these are this process's
        own working state, and because this is the pattern funnel_worker.py and
        digital_arbitrage_worker.py already set. The `create schema if not exists`
        that both of them lead with is deliberately NOT copied here, because for
        this stack it is a hazard rather than a convenience: this file's tables carry
        no cross-schema references, but agent_logs and hustle_metrics live in
        schema.sql's schema, and they are written through search_path, so a
        WORKER_SCHEMA pointing somewhere schema.sql was never applied puts ALL four
        of this worker's tables in one place and the audit trail in another. Nothing
        raises. Provisioning works, the metrics row never appears, and the symptom is
        a dashboard that shows a hustle with no activity. So: refuse unless the
        schema.sql anchor table is actually reachable, and say what to set it to.

        Why a webhook-events table at all, when the architecture doc puts Stripe
        webhooks on a Cloudflare Worker: the edge Worker forwards to us, and the
        only durable handoff between a stateless edge function and this process is a
        row. Insert-and-return is also the only way to answer a webhook fast enough
        that the sender does not retry, and the only way an event survives a restart
        of the thing that consumes it.
        """
        self.assert_schema_installed()
        sql = f"""
            -- Intake. Every external event lands here before anything acts on it.
            create table if not exists {self.queue_table} (
              id            bigint generated always as identity primary key,
              kind          text        not null
                            check (kind in ('client_onboarding','support_ticket',
                                            'billing_renewal')),
              -- Sender's own id for this delivery (event id, invoice id, ticket id).
              -- The dedupe key is derived from kind + this, so a redelivery of the
              -- same event is a no-op insert rather than a second sub-account.
              external_id   text        not null,
              dedupe_key    text        not null,
              payload       jsonb       not null default '{{}}'::jsonb,
              status        text        not null default 'pending'
                            check (status in ('pending','claimed','done','failed',
                                              'skipped_stale')),
              -- Business time: when it happened out in the world. Distinct from
              -- received_at because a queue that fills during an outage delivers
              -- yesterday's events today, and freshness cuts apply to the former.
              occurred_at   timestamptz not null default now(),
              received_at   timestamptz not null default now(),
              available_at  timestamptz not null default now(),
              attempts      integer     not null default 0,
              last_error    text,
              -- Delivery metadata kept out of payload so payload stays purely the
              -- sender's body: an audit of "who signed what" reads cleaner when the
              -- signature is not mixed into the signed document.
              source_tag    text,
              signature_ok  boolean     not null default false
            );
            create unique index if not exists agency_event_dedupe_uq
              on {self.queue_table} (kind, dedupe_key);
            -- Partial so the processed majority leaves the index entirely; the drain
            -- scans pending-and-due and nothing else, forever.
            create index if not exists agency_event_claim_idx
              on {self.queue_table} (kind, available_at)
              where status = 'pending';

            -- One row per client, keyed by the sender's id, so a re-delivered or
            -- late-arriving invoice for a known client updates rather than forks.
            create table if not exists {self.client_table} (
              id                 bigint generated always as identity primary key,
              external_ref       text        not null unique,
              company            text,
              contact_email      text,
              plan_slug          text        not null default 'starter',
              -- 'pending' = paid, not yet provisioned. Distinct from 'failed'
              -- because pending is expected to heal on a retry and failed needs a
              -- human, and the dashboard should not show a vendor outage as a
              -- client we lost.
              status             text        not null default 'pending'
                                 check (status in ('pending','provisioned','suspended',
                                                   'churned','failed')),
              -- Vendor's own id for the sub-account. Our reference into their
              -- system, and the thing a support engineer needs first.
              vendor_account_id  text,
              mrr_minor          bigint      not null default 0 check (mrr_minor >= 0),
              currency           text        not null default 'USD'
                                 check (length(currency) = 3 and currency ~ '^[A-Z]+$'),
              -- Seat count and invoice id are kept because provisioning is
              -- idempotent on (external_ref) but not on seat count: a client who
              -- upgrades twice must not get two accounts.
              seats              integer     not null default 1 check (seats >= 1),
              last_invoice_id    text,
              provisioned_at     timestamptz,
              created_at         timestamptz not null default now(),
              updated_at         timestamptz not null default now(),
              payload            jsonb       not null default '{{}}'::jsonb
            );
            create index if not exists agency_client_status_idx
              on {self.client_table} (status) where status in ('pending','provisioned');

            create table if not exists {self.ticket_table} (
              id             bigint generated always as identity primary key,
              external_ref   text        not null unique,
              client_id      bigint      references {self.client_table}(id) on delete set null,
              subject        text,
              body           text,
              requester      text,
              priority       text        not null default 'normal'
                             check (priority in ('low','normal','high','urgent')),
              -- Routing decision, kept on the row so a week later someone can ask
              -- "why did the bot answer this one" and get an answer from the data
              -- rather than from the model's memory.
              route          text        not null default 'unclassified'
                             check (route in ('auto_reply','platform_bug','needs_human',
                                              'known_answer','unclassified')),
              status         text        not null default 'open'
                             check (status in ('open','replied','escalated','closed')),
              sent_reply     text,
              -- Which runbook article answered it, when one did: a pattern of every
              -- auto-reply citing the same article is a documentation bug worth
              -- fixing at the source.
              kb_article     text,
              created_at     timestamptz not null default now(),
              handled_at     timestamptz,
              payload        jsonb       not null default '{{}}'::jsonb
            );
            create index if not exists agency_ticket_open_idx
              on {self.ticket_table} (created_at) where status = 'open';

            -- Vendor-bound escalations, written separately from the ticket so the
            -- vendor-facing text and the client-facing text are two rows and can
            -- never overwrite each other.
            create table if not exists {self.escalation_table} (
              id             bigint generated always as identity primary key,
              ticket_id      bigint      references {self.ticket_table}(id) on delete cascade,
              client_id      bigint      references {self.client_table}(id) on delete set null,
              reason         text        not null default 'platform_bug',
              severity       text        not null default 'normal'
                             check (severity in ('low','normal','high','urgent')),
              -- Technical facts only. Client branding is deliberately absent here
              -- too: see the module docstring.
              detail         text,
              vendor_account_id text,
              acknowledged   boolean     not null default false,
              created_at     timestamptz not null default now()
            );
            create index if not exists agency_escalation_open_idx
              on {self.escalation_table} (created_at) where not acknowledged;
        """
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)

    # ---------------------------------------------------------- webhook intake
    # The only section that runs inside an HTTP request. No third-party call.

    def verify_or_record(self, kind: str, body: bytes, signature: str,
                        timestamp: str | None) -> tuple[bool, str]:
        """(signature_ok, reason) without deciding whether an unverified event is usable.

        Separate from signature_valid() because those are two decisions and merging
        them is what made an unset secret catastrophic: verification answered only
        yes/no, so "no secret configured" had to be reported as a failure, and a
        failed signature means reject, so a fresh deploy that had not set
        AGENCY_WEBHOOK_SECRET yet rejected every event in the world. Ingested-zero
        looked like a worker that was simply idle. Here the distinction is preserved,
        so an unverified-but-storable event stays storable with a reason attached, and
        only an operator who sets reject_unverified=True turns it into an error.
        """
        if not self.webhook_secret:
            return False, "secret_unconfigured"
        if not signature:
            return False, "signature_missing"
        if not self.signature_valid(body, signature, timestamp):
            return False, "signature_mismatch"
        return True, ""

    def signature_valid(self, body: bytes | str, signature: str,
                        timestamp: str | None = None) -> bool:
        """Constant-time HMAC check over the raw body.

        Raw body, never a re-serialised dict: JSON that went through a parse and a
        dump has key order and float formatting changed, and the sender signed the
        bytes, not our opinion of them. The timestamp participates in the signed
        string when the sender provides one, which is what makes a captured
        signature useless after the replay window rather than useful forever.
        """
        if not self.webhook_secret:
            raise SignatureNotConfigured(
                "AGENCY_WEBHOOK_SECRET is unset; cannot verify a signature")
        payload = body if isinstance(body, bytes) else body.encode()
        signed = f"{timestamp or ''}.{payload.decode('utf-8', 'replace')}".encode()
        expected = hmac.new(self.webhook_secret.encode(), signed, hashlib.sha256)
        candidate = signature.removeprefix("sha256=").strip()
        # compare_digest rather than ==: a timing-visible signature comparison on
        # an unauthenticated endpoint is a real, if slow, oracle.
        return hmac.compare_digest(expected.hexdigest(), candidate)

    @staticmethod
    def client_ref_for(kind: str, note: dict[str, Any], external_id: str) -> str:
        """Which identifier names the CLIENT behind this event.

        Deliberately not the same string as `lead.identity`. Identity names the
        PAYMENT for an onboarding (so two invoices for one client are two events,
        and one invoice delivered twice is one event), while the client row must be
        keyed by the client. Keying both off `identity` is the mistake this method
        exists to prevent: it means a client who pays twice gets two rows in
        agency_clients, and then three things break at once. Support tickets stop
        joining to the client, `select vendor_account_id` finds nothing so
        escalations lose the account a vendor engineer needs first, and
        clients_onboarded counts the same business twice on its second invoice.

        Falls back to `external_id` for a minimal payload, which reproduces the old
        behaviour only in the narrow case that actually wants it: a sender that
        identifies a client solely by invoice id.

        A RENEWAL whose client_ref resolves to an invoice id (because the payload
        named no client) describes an unknown client, and that is accepted rather
        than fatal. The alternative is worse: guessing which existing row was meant
        merges one client's revenue into another's MRR, and a wrong merge is silent
        while a spurious row is visible. It is visible in exactly two places, which
        is what makes the fallback tolerable: the client row appears under an
        in_-prefixed external_ref with a payload that carries the raw event, and
        agency.client.renewed logs the client_id, so a row named after an invoice id
        is unmistakably an unmapped client rather than a real one. The right eventual
        fix sits upstream in the webhook normaliser, not here.
        """
        if kind == SUPPORT_SOURCE:
            return str(note.get("client_id") or note.get("external_ref") or external_id)
        return str(note.get("client_id") or external_id)

    def receive_webhook(self, kind: str, body: bytes | str,
                        signature: str = "", timestamp: str | None = None,
                        *, source_tag: str = "edge", now: datetime | None = None,
                        reject_unverified: bool = True) -> int | None:
        """Verify, parse and enqueue one delivery. Returns the event id, None if duplicate.

        Raises WebhookAuthError for anything that should answer 401 and ValueError for
        anything that should answer 400 (unparseable body, unrecognisable kind). The
        distinction matters to the caller: a 401 on a secret mismatch tells the sender
        to fix its configuration, while a 400 tells it that even with a perfect secret
        this body would never work.

        The three unverified cases are not equivalent, and collapsing them into one
        boolean is what nearly made the first deploy of this module lose data:

        * `secret_unconfigured`, accepted. An unset AGENCY_WEBHOOK_SECRET is our
          misconfiguration and it is cured by setting one. The event is stored
          unverified, so it survives and stays reviewable, while provisioning is still
          held back because pending() will not act on an unverified row. Dropping real
          invoices and customer tickets because an env var was missing is worse than
          holding them, and it was the failure that made a worker ingesting nothing
          look exactly like a worker with nothing to do.
        * `signature_missing`, rejected. Once a secret IS configured, an unsigned
          delivery to that route is either a forgery attempt or a caller that has not
          been told, and both want a 401 rather than storage.
        * `signature_mismatch`, rejected. Wrong, or a replay.

        reject_unverified=False relaxes only the first case, which makes it the flag a
        local smoke test sets, and the reason it exists instead of a plain
        accept-everything default.
        """
        if kind not in DRAIN_ORDER:
            raise ValueError(f"unknown webhook kind {kind!r}; expected one of "
                             f"{', '.join(DRAIN_ORDER)}")
        body_bytes = bytes(body) if isinstance(body, (bytes, bytearray)) \
            else body.encode()
        signature_ok, reason = self.verify_or_record(kind, body_bytes, signature,
                                                     timestamp)
        if not signature_ok and reason != "secret_unconfigured":
            raise WebhookAuthError(f"webhook {reason}")
        if not signature_ok and reject_unverified:
            raise SignatureNotConfigured(
                "AGENCY_WEBHOOK_SECRET is unset and reject_unverified=True; pass "
                "reject_unverified=False to store the event unverified")
        text = body_bytes.decode("utf-8", "replace")
        try:
            payload = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"webhook body is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("webhook body must be a JSON object")

        received = (utc_now() if now is None
                    else (now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
                          ).replace(tzinfo=None))
        row_id = self.enqueue(kind, self.external_id_for(kind, payload), payload,
                              occurred_at=self.occurred_at_for(payload, received),
                              signature_ok=signature_ok, source_tag=source_tag)
        # Log by kind and event, never by body: the body of a support ticket
        # contains whatever a customer typed, and agent_logs is full-text scanned.
        self.log("debug", "agency.webhook.received", f"{kind} accepted",
                 source=source_tag, duplicate=row_id is None,
                 **({"unverified": reason} if not signature_ok else {}))
        return row_id

    @staticmethod
    def external_id_for(kind: str, payload: dict[str, Any]) -> str:
        """The sender's own identifier for this event.

        Checked in a specific order because the choices disagree about what they
        name. An invoice id names a payment; an event id names a delivery. For
        onboarding we want the payment (two invoices for one client are two events,
        and one invoice delivered twice is one event), and for a ticket we want the
        ticket (re-opening the same ticket must not create a second row). A caller
        that passes neither gets a hash of the body, which dedupes exact redelivery
        and nothing else, and is logged as such.
        """
        if kind == CLIENT_SOURCE:
            keys = ("invoice_id", "subscription_id", "client_id", "event_id", "id")
        elif kind == SUPPORT_SOURCE:
            keys = ("ticket_id", "external_id", "message_id", "id")
        else:
            keys = ("invoice_id", "renewal_id", "subscription_id", "event_id", "id")
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)[:200]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return f"anon-{digest}"

    @staticmethod
    def occurred_at_for(payload: dict[str, Any], received: datetime) -> datetime:
        """Business time, if the sender states one.

        A freshness cut measured from receipt rather than from occurrence would let
        an event that sat in a dead queue for nine hours through as fresh, which is
        the exact failure DEFAULT_SPEND_FRESHNESS_HOURS exists to prevent. First
        key that parses wins; a payload carrying both `created` (Unix, Stripe's
        spelling) and `occurred_at` (ISO, the CRM spelling) is normal, and the first
        is what the sender put on the wire deliberately.
        """
        for key in ("occurred_at", "created", "timestamp", "created_at"):
            raw = payload.get(key)
            if raw in (None, "", 0):
                continue
            moment = utc_when(raw, received)
            if moment is not received:
                return moment
        return received

    def enqueue(self, kind: str, external_id: str, payload: dict[str, Any], *,
                occurred_at: datetime | None = None, signature_ok: bool = True,
                source_tag: str = "edge") -> int | None:
        """Queue one event. None if that (kind, external_id) is already queued.

        ON CONFLICT DO NOTHING rather than a pre-check: see the module docstring on
        why check-then-insert is not acceptable for onboarding events.
        """
        dedupe = f"{kind}:{external_id}"
        sql = (f"insert into {self.queue_table}"  # noqa: S608 - validated constant
               " (kind, external_id, dedupe_key, payload, occurred_at, signature_ok,"
               " source_tag)"
               " values (%s, %s, %s, %s::jsonb, coalesce(%s, now()), %s, %s)"
               " on conflict (kind, dedupe_key) do nothing returning id")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (kind, external_id[:200], dedupe[:300],
                                  json.dumps(payload, default=str),
                                  occurred_at.astimezone(UTC) if occurred_at else None,
                                  signature_ok, source_tag[:60]))
                row = cur.fetchone()
                return int(row["id"]) if row else None
        except Exception as exc:  # intake must not die on a loggable bad write
            log.warning("enqueue(%s/%s) failed: %s", kind, external_id[:60], exc)
            raise

    # ------------------------------------------------------------------ claims

    def pending(self, kind: str, limit: int,
                now: datetime) -> tuple[list[Lead], list[Lead]]:
        """Claim up to `limit` pending events. Returns (runnable, parked).

        Two lists rather than one, because a parked row has to be counted as received
        without being dispatched: see the unverified branch below for why it cannot
        simply be returned as work.

        `for update skip locked` carries no volatile predicate here: the age cut
        runs in Python instead, because a `now()` comparison under a locking clause
        makes "excluded by the predicate" and "someone else holds it" return the
        same empty result (measured, and documented at length in common.py's
        claim()). Ordered by received_at rather than occurred_at so the stale rows a
        backlog left behind cannot occupy the head of every batch forever; the age
        cut still applies, just after the claim rather than before it.

        Split across two statements in one autocommit-OFF transaction rather than
        the single `with picked as (select ... for update skip locked) update ...`
        that funnel_worker.py and digital_arbitrage_worker.py use. I first justified
        the split by arguing that a CTE's locking clause releases its locks once the
        CTE is done being evaluated, so the outer UPDATE's ROW SHARE cannot conflict
        with a sibling's ROW EXCLUSIVE and two replicas can claim the same row. That
        argument is wrong about the consequence, and saying so is worth the space
        because it is exactly the kind of confident, plausible-sounding locking claim
        that outlives its checking: PostgreSQL holds the CTE's FOR UPDATE lock until
        the end of the STATEMENT, so the outer UPDATE in that same statement is still
        covered and funnel_worker's one-statement claim is safe for its purpose.
        Verified on PostgreSQL 16 with two connections racing the same four rows:
        replica A picked and stamped all four, replica B's identical pick returned
        nothing, and the committed result was four claimed rows, zero double-claims.

        What the split buys instead is narrower and real: the transaction stays open
        across fetch and stamp, so the claim and the row count are one fact, and
        `fetch vs stamped` is checkable. That check caught a bug the single-statement
        form cannot express, which is what actually earned this shape.
        """
        # No `status` in the column list: 'pending' is guaranteed by the WHERE clause,
        # and the one thing a caller might want from it, "did another replica get
        # here first", is answered by the lock instead of by a column that cannot
        # change while the row is held.
        pick = f"""
            select id, kind, external_id, payload, occurred_at, attempts, signature_ok
              from {self.queue_table}
             where kind = %s and status = 'pending'
               and available_at <= now()
             order by received_at
             limit %s
             -- This lock holds until COMMIT, not until the statement ends, which is
             -- why the transaction stays open across fetch and stamp and why
             -- ClaimRaceError below can be raised with nothing cleaned up: on rollback
             -- the lock is released and the status is still 'pending', so the rows were
             -- never ours and no handler ran. That is also what makes this claim safe
             -- against a second replica, verified on PostgreSQL 16 (racer's pick
             -- returned zero rows while this transaction was open).
             for update skip locked
        """  # noqa: S608 - table name is a validated constant; all values are bind params
        # Two statements, one transaction: the fetch locks, the stamp records. See
        # the docstring for why the stamp carries no status predicate.
        stamp = (f"update {self.queue_table} set status = 'claimed',"  # noqa: S608
                 " attempts = attempts + 1 where id = any(%s)")
        # Set True on every path that resolved the transaction (commit or deliberate
        # rollback). The finally releases a claim that ended up unstamped; see there.
        # The precise thing being tracked is psycopg's TransSummary: after a read-write
        # command the connection sits in INTRANS, and exiting the context manager in
        # that state auto-rolls-back AND leaves an open transaction for whatever runs
        # next on the connection, which is strictly worse than rolling back on purpose.
        committed = False
        parked: list[Lead] = []
        leads: list[Lead] = []
        cutoff = now - timedelta(seconds=self.max_age_seconds)
        # NOT self.store._connect(): that is autocommit, and the lock must outlive
        # the fetch. See the docstring. row_factory is passed HERE rather than relying
        # on the one WorkerStore passes: psycopg resolves a cursor's row factory from
        # its CONNECTION, so a connection opened without it yields tuples and a
        # `cursor.row_factory = dict_row` assignment afterwards is silently ignored on
        # the default cursor class. Measured on psycopg 3.3.9: without this argument
        # the fetch below returns tuples and every row["payload"] raises TypeError.
        with psycopg.connect(self.database_url, autocommit=False, row_factory=dict_row,
                             options=self.store._options()) as conn, conn.cursor() as cur:
            try:
                cur.execute(pick, (kind, limit))
                rows = cur.fetchall()
                if rows:
                    # The stamp is unconditional, and it must stay that way. Adding
                    # `and status = 'claimed'` to it looks like the belt to the lock's
                    # braces and is in fact a contradiction: the SELECT left every row
                    # 'pending', nothing else in this transaction can have changed it,
                    # so an UPDATE restricted to 'claimed' matches zero rows and claims
                    # nothing while the fetched rows are still handed back as if they
                    # were ours. Measured on PostgreSQL 16: guarded rowcount 0,
                    # unguarded rowcount N, same `for update skip locked` in both runs.
                    # That is the general shape of this mistake: the claim IS the
                    # pending->claimed transition, so asserting its post-condition as
                    # its precondition turns the statement into a silent no-op.
                    cur.execute(stamp, ([int(r["id"]) for r in rows],))
                    if cur.rowcount != len(rows):
                        # Unreachable while the lock holds, which is the point of
                        # checking. Raise rather than log: the failure mode of a claim
                        # that does not hold is a queue that drains as no-ops with
                        # every metric reading zero and a worker that looks healthy.
                        # Rolling back first also means these rows are genuinely not
                        # ours, since the rollback releases both the lock and the
                        # status change.
                        conn.rollback()
                        committed = True  # nothing left to release
                        raise ClaimRaceError(
                            f"{kind}: fetched {len(rows)} row(s) but stamped "
                            f"{cur.rowcount}; the claim is not holding")
                # Two hold sets, one statement each, because they need different
                # terminal statuses.
                too_old: list[int] = []
                unverified: list[int] = []
                for row in rows:
                    # To naive, so it compares against cutoff. The asymmetry is the
                    # trap worth stating: psycopg hands back AWARE datetimes for a
                    # `timestamp with time zone` column and NAIVE ones for a plain
                    # `timestamp`, and everything this file writes through utc_now() is
                    # naive-UTC on the way in and comes back aware. Mixing the two
                    # raises TypeError at the comparison, not a wrong answer, so one of
                    # the two sides has to be normalised on every read rather than
                    # assumed.
                    occurred = row["occurred_at"]
                    if occurred.tzinfo is not None:
                        occurred = occurred.astimezone(UTC).replace(tzinfo=None)
                    if occurred < cutoff:
                        too_old.append(int(row["id"]))
                        continue
                    payload = row["payload"] or {}
                    if not row["signature_ok"]:
                        # An unsigned event is not a failed one, but it must not be
                        # acted on as if it were authenticated either: it is held for
                        # a human rather than provisioned on sight. Held, not
                        # 'skipped_stale', and the status CHECK enforces the
                        # distinction, because "this event is suspect" and "this
                        # event is too old to act on" are different facts to report
                        # and different things for a replica to conclude from.
                        unverified.append(int(row["id"]))
                        self.log("warning", "agency.event.unverified",
                                 f"{row['kind']}:{str(row['external_id'])[:60]} accepted "
                                 "without a valid signature; held for review",
                                 event_id=int(row["id"]))
                        continue
                    leads.append(Lead(
                        source=row["kind"],
                        identity=str(row["external_id"]),
                        created_at=occurred,
                        expected_value_minor=int(payload.get("amount_minor")
                                                 or payload.get("mrr_minor") or 0),
                        currency=norm_currency(payload.get("currency")),
                        payload={"event_id": int(row["id"]),
                                 "attempts": int(row["attempts"]),
                                 "client_ref": self.client_ref_for(
                                     row["kind"], payload, str(row["external_id"])),
                                 **payload},
                    ))
                if too_old:
                    cur.execute(f"update {self.queue_table} set status = 'skipped_stale'"  # noqa: S608
                                " where id = any(%s) and status = 'claimed'"
                                " returning id", (too_old,))
                    log.info("%s held back %d stale %s event(s)",
                             HANDLE, len(cur.fetchall()), kind)
                if unverified:
                    # Parked as 'failed' rather than retried: re-offering it every tick
                    # would re-emit this warning forever, and a human has to decide
                    # whether the missing secret is a misconfiguration or a forgery.
                    #
                    # Returned as leads on the side, and that is not decoration. Once
                    # this statement commits the row is 'failed', and finish()'s status
                    # CHECK permits only pending/done/failed, so finish() can never
                    # write a row that is already 'failed'. Without these leads the
                    # caller's finish(lead, ok=False) would look like a failed write,
                    # log agency.event.unstampable, and count an attempted provision, an
                    # escalation or a customer reply that never happened. With them,
                    # those callers see a status outside their dispatch table and stop
                    # before spending anything.
                    cur.execute(f"update {self.queue_table} set status = 'failed',"  # noqa: S608
                                " last_error = 'held: signature not verified'"
                                " where id = any(%s) and status = 'claimed'"
                                " returning id, kind, external_id, occurred_at",
                                (unverified,))
                    held_rows = cur.fetchall()
                    log.info("%s parked %d unverified %s event(s) for review",
                             HANDLE, len(held_rows), kind)
                    for row in held_rows:
                        parked.append(Lead(source=row["kind"],
                                           identity=str(row["external_id"]),
                                           created_at=row["occurred_at"],
                                           payload={"event_id": int(row["id"]),
                                                    "status": "failed"}))
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                committed = True
                raise
            finally:
                if not committed:
                    # Defensive, not a live path: every exit above sets the flag. It
                    # is here because the failure it prevents is invisible, and because
                    # psycopg keeps a connection in TransSummary.INTRANS after a
                    # read-write command EVEN WITH autocommit=True and auto-rolls-back
                    # at close. Leaving the context in that open state would drop the
                    # claim and leave the next command on this connection to roll back
                    # a transaction nobody intended to start, so an unstamped claim is
                    # released deliberately instead of accidentally.
                    conn.rollback()
        # Unverified events are returned separately, not prepended to the work list,
        # so a caller cannot mistake a parked row for a runnable one. tick_once()
        # counts them as received and dispatches nothing.
        return leads, parked

    def finish(self, lead: Lead, *, ok: bool, error: str = "") -> None:
        """Stamp an event and set its next retry time.

        Backoff on a slope (attempt n waits 2^n minutes, capped at a day) because
        the likely cause of a failure here is a vendor API that is unhappy for a few
        minutes, and a flat retry interval turns that into hundreds of identical
        failing calls. After MAX_ATTEMPTS the row is 'failed' and stops being
        retried: at that point the cause is structural and re-running it every tick
        forever only hides the fact that nobody looked.

        A held row returns to 'pending' for its backoff window and to nothing else.
        That is why pending() can filter on status = 'pending' at fetch time and still
        reach every retry: an unclaimed row is by definition unowned, so 'claimed'
        stays reserved for rows actively being worked and never doubles as a waiting
        room. The window is written in SQL rather than computed in Python for the same
        reason claim() puts its lease expiry there: reading the clock and stamping the
        deadline in one statement makes a retry due when the database says so, not
        when this process thinks so, which matters the moment two replicas disagree
        about the time by more than nothing.
        """
        note = lead.note or {}
        if note.get("status") in ("failed", "skipped_stale"):
            # Already terminal, decided inside pending(). Nothing to stamp and nothing
            # to retry, and reporting this as a failed write would be actively wrong:
            # a parked row was written successfully, it just does not need stamping.
            return
        event_id = note.get("event_id")
        if event_id in (None, PROVISIONAL_EVENT_ID):
            # A handler ran on a lead that predates its own insert. Nothing to stamp
            # and nothing to retry, so on a failure say so: silently returning here is
            # how a wiring mistake becomes "the queue drains and nothing happens".
            if not ok:
                self.log("warning", "agency.event.unstampable",
                         f"cannot record failure for {lead.source}:"
                         f"{lead.identity[:60]}: no event id ({error[:160]})",
                         is_audit=True)
            return
        attempts = int((lead.note or {}).get("attempts") or 1)
        exhausted = not ok and attempts >= MAX_ATTEMPTS
        status = "done" if ok else "failed" if exhausted else "pending"
        # Window written on every outcome, including 'done'. A completed row is never
        # read by pending() again, so a stale window there is inert, and writing it
        # unconditionally keeps this one statement instead of two branches that can
        # drift apart.
        sql = (f"update {self.queue_table} set status = %s, last_error = %s,"  # noqa: S608
               " available_at = now() + make_interval(mins =>"
               "   least(1440, power(2, greatest(attempts, 1))::int))"
               " where id = %s")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (status, error[:500] or None, event_id))
        except Exception as exc:  # the row stays claimed; a later sweep re-offers it
            log.warning("finish(event_id=%s) failed: %s", event_id, exc)
        if exhausted:
            self.log("error", "agency.event.exhausted",
                     f"{lead.source}:{lead.identity[:60]} failed after "
                     f"{attempts} attempt(s): {error[:200]}",
                     event_id=event_id, is_audit=True)

    # ------------------------------------------------------------- onboarding

    def upsert_client(self, *, external_ref: str, company: str | None,
                      contact_email: str | None, plan_slug: str,
                      mrr_minor: int, currency: str, seats: int = 1,
                      invoice_id: str | None = None,
                      payload: dict[str, Any] | None = None) -> tuple[int, str, bool]:
        """Insert or update the client row. Returns (id, status, newly_created).

        ON CONFLICT updates the commercial fields but never `status`, with one
        exception: a client currently 'churned' or 'suspended' who produces a new
        invoice is a reactivation and goes back to 'pending'. That is the only
        status a conflicting row may acquire, and it is allowed because paying is
        evidence. Specifically NOT allowed: overwriting 'provisioned' with
        'pending', which would make an upgrade invoice re-trigger provisioning for
        an account that already exists.
        """
        sql = (f"insert into {self.client_table}"  # noqa: S608 - validated constant
               " (external_ref, company, contact_email, plan_slug, mrr_minor, currency,"
               " seats, last_invoice_id, status, payload)"
               " values (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s::jsonb)"
               " on conflict (external_ref) do update set"
               "   company        = coalesce(excluded.company,"
               f"                            {self.client_table}.company),"
               "   contact_email  = coalesce(excluded.contact_email,"
               f"                               {self.client_table}.contact_email),"
               "   plan_slug      = excluded.plan_slug,"
               "   mrr_minor      = excluded.mrr_minor,"
               "   currency       = excluded.currency,"
               "   seats          = excluded.seats,"
               "   last_invoice_id = coalesce(excluded.last_invoice_id,"
               f"                                {self.client_table}.last_invoice_id),"
               "   status = case when "
               f"     {self.client_table}.status in ('churned','suspended')"
               "            then 'pending'"
               f"     else {self.client_table}.status end,"
               "   updated_at = now()"
               " returning id, status, (xmax = 0) as inserted")
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (external_ref[:200], clip(company, 200), clip(contact_email, 200),
                              plan_slug[:80], int(mrr_minor),
                              norm_currency(currency), max(1, int(seats)),
                              clip(invoice_id, 200),
                              json.dumps(payload or {}, default=str)))
            row = cur.fetchone() or {}
        client_id = int(row["id"])
        # `(xmax = 0)` is the standard "this was an INSERT, not an UPDATE" probe.
        # Needed because newly-created clients are the only ones whose onboarding
        # log line is an audit fact; an update to a known client is routine traffic.
        return client_id, str(row.get("status") or "pending"), bool(row.get("inserted"))

    def provision_account(self, lead: Lead, client_id: int) -> dict[str, Any]:
        """Provision the vendor sub-account, gated first. Returns metric deltas.

        Sequence, in the order that matters: read the client row, ask the gate, and
        only then call the vendor. A seat is a recurring cost committed for the
        client's lifetime, so paying before asking would put the spend outside the
        envelope that exists to bound it.

        Returns {} when the gate queues or blocks, because in both cases nothing was
        bought and counting an onboarding that did not happen would make the
        dashboard disagree with the vendor's own account list.
        """
        note = lead.note or {}
        plan = str(note.get("plan_slug") or note.get("plan") or self.default_plan)[:80]
        seats = max(1, int(note.get("seats") or 1))
        amount = int(note.get("seat_cost_minor") or self.seat_cost_minor) * seats
        currency = lead.currency

        # State precondition, checked here rather than by row counts. The client row
        # says whether an account already exists, and unlike a claim's fetch-vs-stamp
        # count, which can only prove that nobody ELSE took the row, this is the only
        # check that catches the case where the very same client arrives twice: a
        # second invoice for one client is a legitimate new event that claims its own
        # row perfectly, and provisioning it again opens a second vendor sub-account
        # that our ledger does not show and the vendor's invoice next month does.
        # Two unrelated guards both fail to catch it. The gate dedupes by
        # idempotency_key, which is derived from client_id, but a DUPLICATE key is
        # reported as transaction_id=.../duplicate=True with execute=True, so it
        # reaches the vendor call anyway: the gate is an accounting dedupe, not a
        # business one. And upsert_client cannot carry this check either, because
        # updating commercial fields and provisioning are separate facts, one of which
        # costs money.
        if self.is_provisioned(client_id):
            self.log("debug", "agency.provision.skipped_existing",
                     f"client {client_id} already holds account "
                     f"{self.vendor_account_for(client_id)[:40]}", client_id=client_id)
            return {}

        if self.dry_run:
            # A dry run that skips the gate proves nothing about whether this would
            # clear tier 1, so it asks and reports rather than assuming.
            outcome = self.request_spend(SpendTicket(
                title=f"agency: provision {plan} (dry run)", amount_minor=amount,
                currency=currency, vendor="whitelabel-vendor",
                rationale="dry run: vendor not called, nothing charged",
                idempotency_key=f"agency:provision:{client_id}"))
            self.log("info", "agency.provision.dry_run",
                     f"would provision client {client_id} "
                     f"({outcome.tier}/{outcome.approval_status})", client_id=client_id)
            return {}

        outcome = self.request_spend(SpendTicket(
            title=f"agency: provision {plan} x{seats}",
            amount_minor=amount, currency=currency,
            rationale="white-label sub-account for a settled invoice; recurring cost",
            expected_roi_note="client MRR covers the seat cost",
            vendor="whitelabel-vendor",
            idempotency_key=f"agency:provision:{client_id}",
            metadata={"client_id": client_id, "plan": plan, "seats": seats}))
        if not outcome.execute:
            self.log("info", "agency.provision.deferred",
                     f"client {client_id} awaits the spend gate "
                     f"({outcome.approval_status})", client_id=client_id,
                     transaction_id=outcome.transaction_id)
            return {}

        vendor_account_id = self.vendor_provision(
            client_ref=self.client_ref_for(CLIENT_SOURCE, note, str(lead.identity)),
            plan=plan, seats=seats,
            contact_email=note.get("contact_email") or note.get("email"),
            idempotency_key=str(outcome.transaction_id or f"client:{client_id}"))
        self.mark_provisioned(client_id, vendor_account_id)
        self.log("info", "agency.client.provisioned",
                 f"client {client_id} on plan {plan} x{seats}, account "
                 f"{str(vendor_account_id)[:40]}", is_audit=True, client_id=client_id,
                 vendor_account_id=vendor_account_id, cost_minor=amount,
                 transaction_id=outcome.transaction_id)
        return {"clients_onboarded": 1, "cost_minor": amount,
                "active_subscriptions": 1}

    def vendor_provision(self, *, client_ref: str, plan: str, seats: int,
                         contact_email: Any, idempotency_key: str) -> str:
        """POST the sub-account creation. Returns the vendor's account id.

        The idempotency key is forwarded so that a retry after a timeout does not
        create a second sub-account on their side. This matters more here than for
        most calls: a duplicate account is invisible in our ledger (we paid once)
        and very visible on the vendor's invoice next month.

        The body deliberately carries no `brand`, and no `WHITELABEL_VENDOR_NAME`
        either. `self.brand` is OUR display name and nothing here needs it; the two
        places the brand legitimately travels are the mail From line and the
        client-facing reply, which is exactly where `_client_visible` and
        `mailer_send` put it. Naming our own brand to the vendor invites them to
        provision their own white-label defaults around it, which is the configuration
        drift that makes a client see a third name on their own login screen, and the
        failure is invisible until the client mentions it in a ticket. Symmetric with
        the isolation rule: the client's copy never names the vendor, and the vendor's
        copy never needs to know us. The parameter is `client_ref`, not
        `external_ref`, because that is what it is: our handle on the client, not a
        vendor concept.
        """
        if not self.vendor_url:
            raise VendorError(
                "WHITELABEL_VENDOR_API_URL is unset; cannot provision", retryable=False)
        headers = {"Content-Type": "application/json",
                   "Idempotency-Key": idempotency_key[:200]}
        if self.vendor_token:
            headers["Authorization"] = f"Bearer {self.vendor_token}"
        body = {"external_ref": client_ref, "plan": plan, "seats": int(seats),
                "contact_email": contact_email or None}
        try:
            with httpx.Client(timeout=self.http_timeout) as client:
                response = client.post(f"{self.vendor_url}/accounts", json=body,
                                       headers=headers)
        except httpx.TransportError as exc:
            # A transport error after the request went out is ambiguous: the
            # account may exist. Retryable, because the idempotency key makes the
            # retry safe and giving up guarantees a client with no account.
            raise VendorError(f"vendor provision transport error: {exc}") from exc
        if response.status_code in (408, 409, 429) or response.status_code >= 500:
            raise VendorError(f"vendor provision http {response.status_code}: "
                              f"{response.text[:200]}")
        if response.status_code >= 400:
            # 4xx is a rejected shape. Repeating it every tick just delays the
            # human who has to fix the payload.
            raise VendorError(f"vendor provision http {response.status_code}: "
                              f"{response.text[:200]}", retryable=False)
        try:
            data = response.json()
        except ValueError as exc:
            raise VendorError("vendor provision returned non-JSON", retryable=False) from exc
        for key in ("account_id", "id", "subaccount_id", "account"):
            value = data.get(key) if isinstance(data, dict) else None
            if value:
                return str(value)[:120]
        raise VendorError("vendor provision response carried no account id",
                          retryable=False)

    def vendor_account_for(self, client_id: int) -> str:
        sql = f"select vendor_account_id from {self.client_table} where id = %s"  # noqa: S608
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (client_id,))
                return str((cur.fetchone() or {}).get("vendor_account_id") or "")
        except Exception as exc:  # a log detail, not a decision
            log.warning("vendor_account_for(%s) failed: %s", client_id, exc)
            return ""

    def is_provisioned(self, client_id: int) -> bool:
        """Does this client already hold a vendor sub-account?

        Keys on `vendor_account_id` rather than on `status = 'provisioned'` so that a
        suspend-then-pay cycle cannot re-provision: a suspended client still HAS the
        sub-account, and creating a second one for them duplicates the seat rather
        than restoring it. The two columns disagree on purpose, and this is the
        question this caller asks.
        """
        sql = (f"select 1 as hit from {self.client_table}"  # noqa: S608
               " where id = %s and vendor_account_id is not null"
               "   and vendor_account_id <> ''")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (client_id,))
                return cur.fetchone() is not None
        except Exception as exc:
            # Fail CLOSED, against the grain of a pure read. Here a database we cannot
            # read is a database on whose state we cannot rely, and the two options
            # are asymmetric: skipping one tick of one client costs a delay, while
            # blindly opening a second sub-account costs money forever. common.py's
            # seen() takes the same side of the same trade for the same reason.
            log.warning("is_provisioned(%s) failed, treating as provisioned: %s",
                        client_id, exc)
            return True

    def mark_provisioned(self, client_id: int, vendor_account_id: str) -> None:
        sql = (f"update {self.client_table} set status = 'provisioned',"  # noqa: S608
               " vendor_account_id = %s, provisioned_at = now(), updated_at = now()"
               " where id = %s")
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (vendor_account_id[:120], client_id))

    # ------------------------------------------------------------------ support

    def classify_ticket(self, subject: str, body: str) -> tuple[str, str | None]:
        """Decide the route for one ticket. Returns (route, kb_article).

        Ordering is the whole method, and it is deliberate:

        1. A platform bug first, because it must never be auto-answered. A cheerful
           canned reply to a customer reporting our vendor's outage reads as denial
           and converts a ticket into a churn event.
        2. A known answer second, because if the runbook covers it, escalating it is
           a waste of the human's time.
        3. Urgency next, which routes to a human without claiming the bot solved it.

        Inverting 1 and 2 is the subtle bug: "the report export is broken, how do I
        download invoices" matches a known-answer phrase and would auto-close a real
        bug. The bug test therefore runs first on purpose, and stays first.

        Returns UNCLASSIFIED rather than a guess. Unclassified tickets stay 'open'
        and are re-read by the next tick, which is recoverable, as opposed to a
        confidently wrong auto-reply, which is not.
        """
        haystack = f"{subject} {body}".lower()
        if any(marker in haystack for marker in PLATFORM_BUG_MARKERS):
            return ROUTE_PLATFORM_BUG, None
        for phrase, article in KNOWN_ANSWER_PATTERNS:
            if phrase in haystack:
                return ROUTE_KNOWN_ANSWER, article
        if any(marker in haystack for marker in URGENT_MARKERS):
            return ROUTE_HUMAN, None
        return ROUTE_UNCLASSIFIED, None

    def handle_ticket(self, lead: Lead) -> dict[str, Any]:
        """Persist a ticket, classify it, and act on the classification.

        Returns metric deltas. `support_tickets` counts tickets RECEIVED, not
        tickets solved: it is a workload gauge, and counting only the ones we
        handled would make a bad day look like a quiet one, which is the opposite
        of what the operator needs to see.

        Counted from the CLAIMED event, not from `ticket_id is not None`, and the
        difference is a real undercount. insert_ticket returns None on a conflict,
        which is how a duplicate ticket delivery says "already here" on this exact
        happy path, so counting only fresh rows would credit one ticket to two
        tickets. Dedupe is a per-`(kind, external_id)` unique index rather than a
        per-customer-per-tick tally, so a genuine inflow spike still counts every
        row. The only inflation left is a resend that arrives as a NEW event id
        naming an existing ticket: an unguessable edge case, and under-counting real
        volume would mislead the operator more than that occasionally inflates it.
        """
        note = lead.note or {}
        external_ref = str(lead.identity)[:200]
        subject = str(note.get("subject") or note.get("title") or "")[:500]
        body = str(note.get("body") or note.get("message") or note.get("text") or "")[:4000]
        requester = str(note.get("requester_email") or note.get("email") or "")[:200]
        priority = norm_priority(note.get("priority"))
        route, article = self.classify_ticket(subject, body)

        ticket_id = self.insert_ticket(
            external_ref=external_ref, subject=subject, body=body,
            requester=requester, priority=priority, route=route,
            client_external_ref=self.client_ref_for(SUPPORT_SOURCE, note,
                                                    str(lead.identity)),
            payload=dict(note))
        deltas = {"support_tickets": 1}
        if ticket_id is None:
            # A redelivery of a ticket already on file. Counted above, not replied
            # to again: nothing below may send.
            self.log("debug", "agency.ticket.duplicate",
                     f"ticket {external_ref[:40]} already on file",
                     ticket_ref=external_ref[:80])
            return deltas
        if route == ROUTE_UNCLASSIFIED:
            # Nothing was sent, so nothing is replied. Staying 'open' is the point:
            # a later tick, or a human, picks this up.
            self.log("warning", "agency.ticket.unclassified",
                     f"ticket {external_ref[:40]} matched no rule; left open",
                     requester=mask_email(requester), ticket_id=ticket_id)
            return deltas

        if route == ROUTE_PLATFORM_BUG:
            self.escalate(ticket_id, external_ref, priority, subject, body)
            # The client still gets acknowledgement, from us, in our voice, saying
            # only that we are on it. Silence during an outage is what generates the
            # second and third ticket from the same customer.
            text = self._client_visible(
                f"We are aware of an issue affecting {self.brand} and our team is "
                f"on it. We will update you on this request (ref {external_ref}) "
                "as soon as it is resolved.")
            self.send_reply(ticket_id, requester, subject, text, "escalated")
            deltas["escalations"] = 1
            return deltas

        if route == ROUTE_HUMAN:
            self.mark_ticket(ticket_id, route=route, status="open",
                             reply=None, kb_article=None)
            if priority_rank(priority) >= self.escalate_rank:
                # High priority but not a vendor bug: ping the operator, who owns
                # the reply. Branding is irrelevant here because the operator sees
                # this, not the client, so it carries the requester verbatim.
                self.notify_operator(
                    f"[{priority}] ticket {external_ref} from "
                    f"{mask_email(requester)}: {subject[:80]}")
            self.log("info", "agency.ticket.held_for_human",
                     f"ticket {external_ref[:40]} needs a person",
                     requester=mask_email(requester), priority=priority)
            return deltas

        reply = self._client_visible(AUTO_REPLY_TEXT[route].format(
            brand=self.brand, answer=f"see {article}" if article else ""))
        self.send_reply(ticket_id, requester, subject, reply, "replied",
                        kb_article=article)
        self.log("info", "agency.ticket.auto_replied",
                 f"ticket {external_ref[:40]} answered from {article}",
                 requester=mask_email(requester), ticket_id=ticket_id,
                 kb_article=article)
        return deltas

    def client_id_for(self, external_ref: Any) -> int | None:
        if not external_ref:
            return None
        sql = f"select id from {self.client_table} where external_ref = %s"  # noqa: S608
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (str(external_ref)[:200],))
                row = cur.fetchone()
                return int(row["id"]) if row else None
        except Exception as exc:
            log.warning("client_id_for(%s) failed: %s", str(external_ref)[:60], exc)
            return None

    def insert_ticket(self, *, external_ref: str, subject: str, body: str,
                      requester: str, priority: str, route: str,
                      client_external_ref: Any, payload: dict[str, Any]) -> int | None:
        """Insert (or re-open in place) a ticket. Returns its id, or None on conflict.

        A re-delivery of a ticket we have already classified updates the body but
        keeps the existing route and status, so a webhook resend cannot downgrade a
        resolved ticket back to 'open' and cannot make the bot reply to the same
        customer twice. Re-opening is a human action on this table, not something a
        duplicate delivery gets to do.
        """
        client_id = self.client_id_for(client_external_ref)
        sql = (f"insert into {self.ticket_table}"  # noqa: S608 - validated constant
               " (external_ref, client_id, subject, body, requester, priority, route,"
               " payload)"
               " values (%s, %s, %s, %s, %s, %s::text, %s::text, %s::jsonb)"
               " on conflict (external_ref) do nothing"
               " returning id")
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (external_ref, client_id, subject, body, requester,
                              priority, route, json.dumps(payload, default=str)))
            row = cur.fetchone()
            return int(row["id"]) if row else None

    def mark_ticket(self, ticket_id: int | None, *, route: str | None = None,
                    status: str, reply: str | None, kb_article: str | None) -> None:
        if not ticket_id:
            return
        sets = ["status = %s::text", "handled_at = now()"]
        params: list[Any] = [status]
        if route is not None:
            sets.append("route = %s::text")
            params.append(route)
        if reply is not None:
            sets.append("sent_reply = %s")
            params.append(reply[:2000])
        if kb_article is not None:
            sets.append("kb_article = %s")
            params.append(kb_article[:120])
        params.append(ticket_id)
        sql = (f"update {self.ticket_table} set {', '.join(sets)} where id = %s"  # noqa: S608
               f" and status = 'open'")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, tuple(params))
        except Exception as exc:  # the reply already went out; stamping can retry
            log.warning("mark_ticket(%s) failed: %s", ticket_id, exc)

    def send_reply(self, ticket_id: int | None, requester: str, subject: str,
                   text: str, status: str, *, kb_article: str | None = None) -> None:
        """Send one client-visible reply and record it.

        The leak guard runs before the transport, not inside it, so that a bad
        template cannot reach the customer on a path that happens to skip an
        assertion in the mailer. Raising here leaves the ticket 'open' and the event
        retries, which is the desired outcome: no reply is far better than a reply
        that names the vendor.
        """
        if self.dry_run:
            self.log("info", "agency.reply.dry_run",
                     f"would send to {mask_email(requester)}: {text[:80]}",
                     ticket_id=ticket_id)
            return
        if not requester:
            # No address to reply to is not a failure to retry forever: record what
            # we would have said and close it, or every tick re-sends into nowhere.
            self.log("warning", "agency.reply.no_recipient",
                     f"no requester on ticket {ticket_id}; reply recorded, not sent",
                     ticket_id=ticket_id, is_audit=True)
            self.mark_ticket(ticket_id, status=status, reply=text, kb_article=kb_article)
            return
        self.mailer_send(to=requester,
                         subject=f"[{self.brand}] {subject[:80] or 'your request'}",
                         body=text, ref=str(ticket_id))
        self.mark_ticket(ticket_id, status=status, reply=text, kb_article=kb_article)

    def mailer_send(self, *, to: str, subject: str, body: str, ref: str) -> None:
        """Hand one reply to the SMTP relay.

        Module 4 owns the relay, so this reads its coordinates rather than being
        given them: the wiring is one env var to change, and the failure mode of a
        relay that is not configured yet is a raised error and a retried event
        rather than a silently dropped customer reply.
        """
        host, port, sender = env("SMTP_HOST"), env("SMTP_PORT", "587"), env("SMTP_FROM")
        if not host or not sender:
            raise VendorError(
                "SMTP_HOST/SMTP_FROM are unset; refusing to drop a client reply. "
                "Configure the relay or run with --dry-run.")
        import smtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["From"] = f"{self.brand} Support <{sender}>"
        message["To"] = to
        message["Subject"] = subject
        message["X-Entity-Ref-ID"] = ref[:120]
        message.set_content(body)
        with smtplib.SMTP(host, int(port), timeout=20) as smtp:
            smtp.ehlo()
            if env("SMTP_STARTTLS", "1") not in ("0", "false", "no"):
                smtp.starttls()
            if env("SMTP_USER"):
                smtp.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
            smtp.send_message(message)

    def escalate(self, ticket_id: int | None, external_ref: str, priority: str,
                 subject: str, body: str) -> None:
        """Record a vendor-bound escalation and ping the operator relay.

        Deliberately two writes with different audiences: this row goes to whoever
        talks to the vendor, and it carries technical facts and the client's account
        reference, never our marketing name. The client's own acknowledgement is a
        separate send in handle_ticket(), which is what keeps the two texts from
        collapsing into one message that has to serve both readers.
        """
        client_id = self.client_id_for(external_ref)
        # Subject and body are verbatim customer text, so the guard applies to them
        # too. The isolation rule binds client-visible strings, and this is the
        # escalation copy a client can end up CC'd on. A customer naming us in their
        # own ticket must not bounce the escalation: their words are not our
        # template, so a leak here logs rather than raises.
        if self.vendor_name and self.vendor_name.lower() in f"{subject} {body}".lower():
            self.log("info", "agency.escalation.customer_names_vendor",
                     f"ticket {external_ref[:40]} mentions the vendor in customer text",
                     ticket_ref=external_ref[:80])
        vendor_account_id = ""
        if client_id:
            sql = f"select vendor_account_id from {self.client_table} where id = %s"  # noqa: S608
            try:
                with self.store._connect() as conn, conn.cursor() as cur:
                    cur.execute(sql, (client_id,))
                    vendor_account_id = str((cur.fetchone() or {}).get(
                        "vendor_account_id") or "")
            except Exception as exc:
                # Soft on purpose, unlike is_provisioned(), which fails closed on the
                # same read of the same column. The answers differ because what is done
                # with them does: here a failure costs one missing field on an
                # escalation that is still written and still paged, and raising would
                # abandon a customer mid-incident to protect the completeness of an
                # internal note that a retry cannot improve anyway. There, the same
                # failure would trigger a paid vendor call. Detail degrades; money
                # does not.
                log.warning("escalate: vendor account lookup failed: %s", exc)
        detail = f"ref={external_ref} account={vendor_account_id or 'unknown'}\n" \
                 f"{subject}\n{body[:1500]}"
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"insert into {self.escalation_table}"  # noqa: S608
                    " (ticket_id, client_id, reason, severity, detail, vendor_account_id)"
                    " values (%s, %s, 'platform_bug', %s::text, %s, %s)",
                    (ticket_id, client_id, priority if priority in PRIORITIES else "normal",
                     detail[:4000], vendor_account_id or None))
        except Exception as exc:  # the ticket stays open, so a retry re-escalates
            raise VendorError(f"escalation write failed: {exc}") from exc
        self.mark_ticket(ticket_id, route=ROUTE_PLATFORM_BUG, status="escalated",
                         reply=None, kb_article=None)
        self.notify_operator(
            f"platform bug reported on {external_ref} (severity {priority}); "
            f"vendor account {vendor_account_id or 'unknown'}")

    def notify_operator(self, text: str) -> None:
        """Best-effort push to the operator relay (Telegram/Slack land in Module 4).

        Failure here is logged, not raised. The escalation row is already committed,
        so the fact is durable; failing the whole ticket because a chat webhook
        timed out would re-run an onboarding that already succeeded.
        """
        if not self.escalation_url:
            log.info("%s escalation (no relay configured): %s", HANDLE, text[:200])
            return
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
                client.post(self.escalation_url, json={"text": text[:1000]})
        except Exception as exc:
            log.warning("operator relay post failed: %s", exc)

    # -------------------------------------------------------------- renewals

    def handle_renewal(self, lead: Lead) -> dict[str, Any]:
        """Record a renewal or a payment failure against the client row.

        Renewals are recognised by the payload, not by the route: an
        'invoice.paid' for a client we already provisioned is a retention signal,
        while a 'charge_failed' is the earliest warning of a cancellation and the
        one event worth telling a human about immediately, because a dunning email
        sent today saves the MRR that expires in seven days.
        """
        note = lead.note or {}
        status_hint = str(note.get("status") or note.get("event") or "").lower()
        client_id, current, _ = self.upsert_client(
            external_ref=self.client_ref_for(RENEWAL_SOURCE, note, str(lead.identity)),
            company=note.get("company"), contact_email=note.get("email"),
            plan_slug=str(note.get("plan_slug") or self.default_plan),
            mrr_minor=int(note.get("amount_minor") or lead.expected_value_minor or 0),
            currency=lead.currency, seats=max(1, int(note.get("seats") or 1)),
            invoice_id=note.get("invoice_id"), payload=dict(note))
        failing = any(word in status_hint for word in ("fail", "unpaid", "past_due",
                                                       "failed", "delinquent"))
        if failing:
            # 'provisioned' only. A payment failure on a client we have never
            # provisioned is not a suspension: writing 'pending' there would strand
            # them outside every partial index, out of the provision-account retry
            # path, and without any log line that says why the client who just paid
            # nothing has no account. Leaving 'pending' untouched says exactly what
            # is true, namely not provisioned yet.
            sql = (f"update {self.client_table} set status = 'suspended',"  # noqa: S608
                   " updated_at = now() where id = %s and status = 'provisioned'"
                   " returning id")
            suspended = False
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (client_id,))
                suspended = bool(cur.fetchall())
            self.notify_operator(
                f"payment failed for client {client_id}"
                f" ({str(lead.identity)[:40]}); MRR at risk")
            self.log("warning", "agency.client.payment_failed",
                     f"client {client_id} payment failed"
                     f"{' ; suspended' if suspended else ' (was not provisioned)'}",
                     is_audit=True, client_id=client_id, current_status=current,
                     suspended=suspended)
            return {"cancellations": 1}
        # 'pending' means paid-but-unprovisioned, which is what an onboarding event
        # is for and what makes the account get created. A renewal must never write
        # it, or a renewal arriving before its client was provisioned would look
        # like a fresh onboarding and provision a second account next tick.
        self.log("info", "agency.client.renewed",
                 f"client {client_id} renewed on plan "
                 f"{note.get('plan_slug') or self.default_plan} (status {current})",
                 is_audit=True, client_id=client_id, current_status=current)
        return {"revenue_minor": int(note.get("amount_minor")
                                     or lead.expected_value_minor or 0)}

    # ------------------------------------------------------------------- spend

    def request_spend(self, ticket: SpendTicket) -> Any:
        """Ask the gate. Raises SpendGatewayError if the gate is unreachable.

        Copied from BaseWorker rather than inherited, because this class does not
        subclass it (see run()'s docstring). Kept thin on purpose: the gate owns the
        tier rule, and a second copy here that also decided tiers would be a second
        policy that can drift.
        """
        if not self.gateway.configured:
            raise SpendGatewayError(
                "FINANCE_AGENT_URL is unset; refusing to provision ungated. "
                "Set FINANCE_AGENT_URL=http://finance-gatekeeper:3200")
        outcome = self.gateway.request_spend(ticket, self.HANDLE, self.HUSTLE)
        self.log("warning" if outcome.blocked else "info", "worker.spend.gated",
                 f"{ticket.title} {fmt_minor(ticket.amount_minor, ticket.currency)} "
                 f"-> {outcome.tier}/{outcome.approval_status} ({outcome.reason})",
                 is_audit=True, transaction_id=outcome.transaction_id,
                 amount_minor=ticket.amount_minor, tier=outcome.tier,
                 approval_status=outcome.approval_status, duplicate=outcome.duplicate)
        return outcome

    def log(self, level: str, event: str, message: str, *, is_audit: bool = False,
            **extra: Any) -> None:
        self.store.log(self.HANDLE, level, event, message, hustle=self.HUSTLE,
                       is_audit=is_audit, extra=extra or None)

    # -------------------------------------------------------------------- tick

    def tick_once(self) -> dict[str, Any]:
        """Drain every queue once. Returns metric deltas. Raises on unexpected failure.

        Metrics are applied per kind, inside the loop, rather than once at the end.
        A tick that dies halfway through a client batch must still book the
        onboardings it actually did, since those correspond to vendor accounts that
        now exist. Aggregating at the end and then raising loses exactly that record,
        which is the one thing a reconciliation looks for.
        """
        self.ensure_tables()
        started = time.monotonic()
        now = utc_now()
        metrics: dict[str, int] = {}
        counts = {"collected": 0, "done": 0, "failed": 0, "parked": 0}

        for kind in DRAIN_ORDER:
            leads, parked = self.pending(kind, self.batch_size, now)
            counts["collected"] += len(leads) + len(parked)
            counts["parked"] += len(parked)
            deltas: dict[str, int] = {}
            # A parked (unverified) ticket was still RECEIVED, so it belongs in the
            # workload gauge, and it is deliberately NOT dispatched: counting it is
            # a statement about inbound volume, dispatching it is a decision to spend
            # money on someone whose signature we could not check. Two different
            # questions, and only the first one is answered yes here.
            if kind == SUPPORT_SOURCE and parked:
                deltas["support_tickets"] = len(parked)
            for lead in leads:
                handler = {CLIENT_SOURCE: self.handle_onboarding,
                           SUPPORT_SOURCE: self.handle_ticket,
                           RENEWAL_SOURCE: self.handle_renewal}[kind]
                try:
                    result = handler(lead)
                except VendorError as exc:
                    # Expected failure (vendor down, mailer unconfigured). Retry on
                    # the backoff curve; non-retryable ones park immediately.
                    self.finish(lead, ok=False, error=f"VendorError: {exc}")
                    counts["failed"] += 1
                    self.log("warning", "agency.lead.vendor_error",
                             f"{kind}:{lead.identity[:60]}: {exc}",
                             retryable=exc.retryable)
                    continue
                except (SpendGatewayError, httpx.HTTPError) as exc:
                    # Cannot ask the gate means cannot spend, so this is a hold, not
                    # a bug. The event goes back to pending and the tick continues,
                    # because one unreachable gate should not stop ticket routing.
                    self.finish(lead, ok=False, error=f"{type(exc).__name__}: {exc}")
                    counts["failed"] += 1
                    self.log("warning", "agency.lead.held",
                             f"{kind}:{lead.identity[:60]} held: {exc}")
                    continue
                except Exception as exc:
                    self.finish(lead, ok=False, error=f"{type(exc).__name__}: {exc}")
                    counts["failed"] += 1
                    self.log("error", "worker.lead.failed",
                             f"{kind}:{lead.identity[:60]}: {type(exc).__name__}: {exc}")
                    continue
                self.finish(lead, ok=True)
                counts["done"] += 1
                for key, value in (result or {}).items():
                    deltas[key] = deltas.get(key, 0) + int(value)
            # `escalations` counts vendor escalations, which hustle_metrics has no
            # column for, and dispute_count is not it (that is a chargeback-ratio
            # input). Dropped rather than coerced: add_metrics' allow-list would reject
            # the whole batch, which is the right outcome but a noisy way to get it.
            # The escalations themselves are in agency_escalations, countable there.
            deltas.pop("escalations", None)
            if deltas:
                self.store.add_metrics(self.HANDLE, self.HUSTLE, deltas)
                for key, value in deltas.items():
                    metrics[key] = metrics.get(key, 0) + value

        purged = self.purge_old_tickets()
        duration_ms = int((time.monotonic() - started) * 1000)
        # Logged, not returned: this dict's remaining keys are hustle_metrics column
        # names, and run_once() prints it straight to stdout for cron smoke tests,
        # where a key that is not a column reads like one that failed to write.
        # `parked` in this line is the count of events held for human review (an
        # unverifiable signature), not the count of tickets closed by the purge below,
        # which is `purged`. Two different parking lots and the names are deliberately
        # unlike: conflating them reads as "the queue drained into nothing".
        self.log("info", "worker.tick",
                 f"collected={counts['collected']} done={counts['done']} "
                 f"failed={counts['failed']} parked={counts['parked']} "
                 f"purged={purged}",
                 **counts, purged=purged, duration_ms=duration_ms)
        return metrics

    def handle_onboarding(self, lead: Lead) -> dict[str, Any]:
        """Paid invoice -> client row -> provisioned sub-account.

        Split from handle_client so the two decisions stay separately testable: the
        row exists whether or not the money was approved, and the audit question
        "did we have a client record before we spent" is common enough to want its
        own function boundary.
        """
        note = lead.note or {}
        client_id, _, newly_created = self.upsert_client(
            external_ref=self.client_ref_for(CLIENT_SOURCE, note, str(lead.identity)),
            company=note.get("company"), contact_email=note.get("email"),
            plan_slug=str(note.get("plan_slug") or note.get("plan") or self.default_plan),
            mrr_minor=int(note.get("amount_minor") or lead.expected_value_minor or 0),
            currency=lead.currency, seats=max(1, int(note.get("seats") or 1)),
            invoice_id=note.get("invoice_id"), payload=dict(note))
        if newly_created:
            # Audit-flagged: an onboarding is the moment a stranger becomes a payer,
            # and the operator's approval feed filters on is_audit.
            self.log("info", "agency.client.created",
                     f"new client from invoice {str(lead.identity)[:40]} "
                     f"({mask_email(note.get('email'))})", is_audit=True,
                     client_id=client_id)
        return self.provision_account(lead, client_id)

    def purge_old_tickets(self) -> int:
        """Close tickets left open past the retention horizon.

        Returns the count. A ticket that nobody answered in a month has been
        answered somewhere else or abandoned, and leaving it 'open' forever keeps it
        in the open-tickets index and in every "what is still pending" count, so
        over a few months that dashboard number stops meaning anything.

        `status = 'open'` is in the WHERE clause, not just the SELECT, so a ticket
        answered by a concurrent replica is not clobbered by this sweep.
        """
        sql = (f"update {self.ticket_table} set status = 'closed',"  # noqa: S608
               " handled_at = now()"
               " where status = 'open' and created_at < now() - make_interval(days => %s)"
               " returning id")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (self.retention_days,))
                return len(cur.fetchall())
        except Exception as exc:  # housekeeping; the queue drain matters more
            log.warning("purge_old_tickets failed: %s", exc)
            return 0

    # --------------------------------------------------------------------- run

    def run_forever(self) -> None:
        """Plain-thread tick loop, not BaseWorker's.

        The base loop is asyncio-native. This worker has no concurrency to gain
        from it: webhooks arrive by insert and every drain is a serial pass over
        rows that must be claimed in order, so the tick would spend its life hopping
        to a thread to escape blocking psycopg and httpx calls. Same shape as
        digital_arbitrage_worker.py's loop, for the same reason. The main thread
        parks on a stop Event, which is precisely where a SIGTERM lands.
        """
        import signal
        import threading

        self._stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Only the main thread may install handlers; a caller embedding this
            # worker gets the Event-based stop instead and must set it itself.
            with contextlib.suppress(ValueError):
                signal.signal(sig, lambda *_: self._stop.set())

        self.ensure_tables()
        if not self.gateway.configured:
            # Loud at boot, not at the first provisioning attempt, which may be hours away.
            log.warning("FINANCE_AGENT_URL unset: onboarding cannot provision")
            self.log("warning", "worker.gate.unconfigured",
                     "FINANCE_AGENT_URL unset; provisioning is refused until set")
        if not self.vendor_url:
            log.warning("WHITELABEL_VENDOR_API_URL unset: events will queue but no "
                        "account can be provisioned")
        if not self.vendor_name:
            # The leak guard needs a name to guard against. With none set it passes
            # every string, so the isolation rule is unenforced rather than
            # satisfied, and the only way anyone finds out is a client who mentions
            # the vendor's name in a ticket.
            log.warning("WHITELABEL_VENDOR_NAME unset: the vendor-isolation guard "
                        "cannot fire, so nothing checks client text for the vendor's "
                        "name; set it to the name the client must never see")
        if not self.webhook_secret:
            # Once at boot, not once per webhook: a per-event warning buries the
            # real traffic and trains everyone to skip it. And this is not a
            # permissive state: receive_webhook() raises SignatureNotConfigured
            # without a secret, so the actual consequence is that intake rejects
            # every delivery until someone sets it. Saying so beats a mystery 500.
            log.warning("AGENCY_WEBHOOK_SECRET unset: webhook intake will reject "
                        "every delivery until it is set")
        log.info("worker %s starting | hustle=%s tick=%ss batch=%s freshness=%ss "
                 "brand=%s dry_run=%s", self.HANDLE, self.HUSTLE, self.tick_seconds,
                 self.batch_size, self.max_age_seconds, self.brand, self.dry_run)

        try:
            while not self._stop.is_set():
                try:
                    self.tick_once()
                    self._errors = 0
                except Exception as exc:
                    self._errors += 1
                    level = logging.ERROR if self._errors >= 3 else logging.WARNING
                    log.log(level, "tick failed (%d in a row): %s", self._errors, exc)
                    # A failing tick still reports. A silent worker is
                    # indistinguishable from an idle one, and only one of those
                    # needs somebody awake.
                    self.log("error" if level == logging.ERROR else "warning",
                             "worker.tick.failed", f"{type(exc).__name__}: {exc}",
                             consecutive_errors=self._errors)
                self._stop.wait(max(5, self.tick_seconds))
        finally:
            self.close()

    def assert_schema_installed(self) -> None:
        """Fail loudly if this worker's tables would land apart from agent_logs.

        Called from ensure_tables(), which every entry point reaches before any
        write, and cheap: one catalog lookup, once per tick, against a schema that is
        in the relcache from the first statement onward.

        `to_regclass` rather than `select 1 from agent_logs limit 1`: no table scan,
        no dependence on the anchor table having rows, and no privilege requirement.
        Resolving through search_path rather than hard-coding `engine.` is the point,
        since search_path is exactly what a WORKER_SCHEMA override changes. A schema
        that does not exist at all fails earlier, in the connection's
        `search_path` option, with PostgreSQL's own InvalidSchemaName; this check
        catches the quieter case, a reachable search_path whose schema was never
        migrated, where the connection succeeds and only the tables are missing.
        """
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute("select to_regclass('agent_logs') is not null as present")
                row = cur.fetchone()
        except Exception as exc:  # cannot read the catalog: do not presume, report
            raise RuntimeError(f"cannot check whether the '{self.schema}' schema is "
                               f"installed: {type(exc).__name__}: {exc}") from exc
        if not (row or {}).get("present"):
            raise RuntimeError(
                f"agent_logs is not reachable with WORKER_SCHEMA={self.schema!r}, so "
                f"this worker's four tables would be created where its audit trail and "
                f"hustle_metrics are not. Apply supabase/schema.sql to that schema, or "
                f"unset WORKER_SCHEMA to use the default 'engine'.")

    def close(self) -> None:
        """No-op today: WorkerStore opens a connection per statement.

        Named and called from the shutdown path anyway, so moving to a pooled
        connection is a one-line change here rather than a leak on every restart.
        """

    def run(self) -> None:
        # Ctrl-C is a supported way to stop, not an error: the finally block above
        # has already run close() by the time this returns.
        with contextlib.suppress(KeyboardInterrupt):
            self.run_forever()

    def run_once(self) -> dict[str, Any]:
        """One pass of everything, synchronously. For cron, smoke tests, CI."""
        metrics = self.tick_once()
        return {"worker": self.HANDLE, "metrics": metrics}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_worker(args: argparse.Namespace) -> AgencyWorker:
    return AgencyWorker(
        build_database_url(), build_gateway(),
        tick_seconds=args.tick_seconds or _int_env("WHITELABEL_TICK_SECONDS", 0)
        or _int_env("WORKER_TICK_SECONDS", 60),
        batch_size=args.batch_size or _int_env("WORKER_BATCH_SIZE", 25),
        freshness_hours=_float_env("WORKER_FRESHNESS_HOURS", 6.0),
        dry_run=args.dry_run)


def simulate_inbound(worker: AgencyWorker, args: argparse.Namespace) -> int:
    """Push one synthetic event through the real intake path.

    Exercises receive_webhook() rather than calling handle_onboarding() directly,
    because the parts most likely to be broken are the signature check and the
    dedupe, and a smoke test that skips them proves nothing about a deployment. A
    temporary secret is installed for the duration so the path under test is the
    strict one.
    """
    kind = args.simulate_kind
    payload: dict[str, Any] = {
        "event_id": f"smoke-{int(time.time())}",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if kind == CLIENT_SOURCE:
        payload.update({"invoice_id": f"in_smoke_{args.client}",
                        "client_id": args.client, "company": f"{args.client} Ltd",
                        "email": f"ops@{args.client}.example", "plan_slug": "starter",
                        "amount_minor": 4900, "currency": "USD", "seats": 1})
    elif kind == SUPPORT_SOURCE:
        payload.update({"ticket_id": f"tk_smoke_{int(time.time())}",
                        "client_id": args.client, "subject": "cannot log in",
                        "body": "getting a 503 when I try to load the dashboard",
                        "requester_email": f"ops@{args.client}.example",
                        "priority": "high"})
    else:
        payload.update({"invoice_id": f"in_smoke_renew_{args.client}",
                        "client_id": args.client, "status": "paid",
                        "amount_minor": 4900, "currency": "USD"})

    body = json.dumps(payload)
    secret = worker.webhook_secret or secrets.token_urlsafe(24)
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(secret.encode(),
                                     f"{timestamp}.{body}".encode(),
                                     hashlib.sha256).hexdigest()
    previous, worker.webhook_secret = worker.webhook_secret, secret
    try:
        event_id = worker.receive_webhook(kind, body, signature, timestamp)
    except Exception as exc:
        print(f"intake rejected the event: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        worker.webhook_secret = previous

    print(json.dumps({"queued": event_id, "kind": kind, "duplicate": event_id is None}))
    if args.draining:
        print(json.dumps(worker.run_once(), default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="workers.agency_worker",
        description="Agency worker: client onboarding webhooks, account setup, "
                    "support routing.",
        epilog="All configuration comes from the environment (.env is read for you).")
    parser.add_argument("--once", action="store_true",
                        help="drain every queue once and exit (cron / smoke test)")
    parser.add_argument("--tick-seconds", type=int, default=None,
                        help="override WHITELABEL_TICK_SECONDS / WORKER_TICK_SECONDS")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="events drained per kind per tick")
    parser.add_argument("--dry-run", action="store_true",
                        help="no vendor, mailer or relay calls; still asks the gate "
                             "so tier routing is proven")
    parser.add_argument("--simulate-inbound", action="store_true",
                        help="push one synthetic event through the real intake path")
    parser.add_argument("--kind", dest="simulate_kind", default=CLIENT_SOURCE,
                        choices=DRAIN_ORDER, help="event kind for --simulate-inbound")
    parser.add_argument("--client", default="acme",
                        help="client ref for --simulate-inbound")
    parser.add_argument("--no-drain", dest="draining", action="store_false",
                        help="with --simulate-inbound, queue only and do not drain")
    parser.add_argument("--check", action="store_true",
                        help="create the tables, print the config, exit")
    parser.set_defaults(draining=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not build_database_url():
        print("DATABASE_URL (or SUPABASE_DB_PASSWORD) is unset; nowhere to write. "
              f"Refusing to start {HANDLE}.", file=sys.stderr)
        return 2
    if psycopg is None:
        print("psycopg is not installed. pip install 'psycopg[binary]'", file=sys.stderr)
        return 2

    worker = build_worker(args)
    if args.check:
        worker.ensure_tables()
        print(json.dumps({"worker": HANDLE, "hustle": HUSTLE, "schema": worker.schema,
                          "brand": worker.brand, "vendor_api": bool(worker.vendor_url),
                          "webhook_secret": bool(worker.webhook_secret),
                          "gate": worker.gateway.base_url or None,
                          "smtp": bool(env("SMTP_HOST") and env("SMTP_FROM")),
                          "tables": [worker.queue_table, worker.client_table,
                                     worker.ticket_table, worker.escalation_table]}))
        return 0
    if args.simulate_inbound:
        return simulate_inbound(worker, args)
    if args.once:
        try:
            print(json.dumps(worker.run_once(), default=str))
        except Exception as exc:
            print(f"pass failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0
    worker.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
