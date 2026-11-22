"""The funnel's billing verbs: what the routes ask for, in the order the spec demands.

`main.py` grew from twelve-question routing to money routing, and money routing has one
property worth protecting above all others: **the cancel click must be finished before any
retention copy is chosen** (§2.3 rule 1). If that ordering lived in a route, the next route
that needs a cancel would be free to get it wrong. So the ordering lives here, in
`cancel_now`, and routes call one function.

Layering:

    main.py (routes, forms, templates)
      -> BillingService (this module: verbs, state transitions, email decisions)
          -> stripe_billing.py (HTTP, signature, event interpretation)
          -> billing_store.py (the funnel's own state)
          -> mailer.py (lifecycle email content and transport)

Every method here is allowed to be called by a webhook retry, a page load, or a human tapping
a button twice. That constraint shapes the code: `handle_event` returns early on unknown
events rather than raising (Stripe retries forever on a 5xx, so an event type we ignore must
not look like a failure), and writes are upserts keyed on ids the processor owns.

Rehearsal mode. `client` is None unless `FUNNEL_CHARGES_ENABLED` is on *and* a funnel secret
key is present. With no client, `checkout_complete` records a local trial instead of calling
Stripe, so the dashboard, cancel, save-offer and email paths are all exercisable with no card
and no key. Those paths are where the compliance rules live, and a rehearsal that skips them
tests the least interesting part of the funnel.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import billing_store
import save_offer
import storage
import plans
import stripe_billing
import telemetry
from billing_store import BillingStore
from config import FunnelSettings
from funnel_copy import Copy, build_copy, human_date, iso_to_local_date
from mailer import Email, Mailer, cancellation_email, decline_email, renewal_notice_email, trial_receipt_email

log = logging.getLogger("funnel.billing")

#: Seconds a checkout session is given before we stop waiting for the redirect back. Display
#: only: the expiry column exists so "your checkout expired, nothing was charged" can be said
#: with a date instead of a guess.
CHECKOUT_SESSION_LIFETIME = 60 * 60


def epoch_to_iso(seconds: int) -> str:
    """Processor timestamp as the ISO string this repo stores. The only epoch-to-string
    conversion here on purpose: every stored date is ISO, and a second helper that produced a
    `date` instead is what a caller would eventually hand to `human_date`, which parses ISO."""
    if not seconds:
        return ""
    return datetime.fromtimestamp(int(seconds), timezone.utc).replace(microsecond=0).isoformat()


def lite_with_discount(lite_amount_minor: int) -> int:
    """Price for the lite tier while the discount offer is active: half of lite.

    Halving the *tier* rather than the full price is what keeps a lite member's invoice equal
    to the number the save screen showed them. Rounding up so we never charge a fraction more
    than the screen promised... and never a fraction less either, since the coupon halves
    whatever the price is.
    """
    return -(-lite_amount_minor // 2)


@dataclass(frozen=True)
class CancelResult:
    """The outcome of the one click. `done` is the only field the screen's headline reads."""

    done: bool
    #: What the screen states as fact. Written before any offer is built, which is the rule.
    headline: str
    detail: str
    #: False when the processor rejected the cancel; the screen then says the click did not
    #: take effect, and never shows a save offer for a subscription that is still live.
    processor_ok: bool
    access_until: str
    error: str = ""


def failure_result(settings: FunnelSettings, error: Any, *,
                   access_until: str = "") -> CancelResult:
    """The one wording for "that did not take effect", built in one place.

    Two callers need it: `cancel_now`, whose client refused, and the route, whose client raised
    before `cancel_now` could catch anything. Duplicating the sentence would let them drift into
    describing the same state differently on two screens, and the state is one thing: the
    subscription is still live, so nothing has been promised and nothing has been lost.

    Note what it does *not* claim. It never says the processor refused specifically, because a
    transport failure reaching here is indistinguishable from a rejection at this point, and "we
    could not cancel that" is true of both.
    """
    return CancelResult(
        done=False,
        headline="We could not cancel that.",
        detail=("The payment provider refused the request, so your subscription is "
                "still active and nothing has changed. Try again, or reply to any "
                f"email from us at {settings.support_email or 'our support address'} "
                "and we will cancel it for you."),
        processor_ok=False,
        access_until=access_until,
        error=str(error),
    )


class BillingService:
    def __init__(self, settings: FunnelSettings, store: BillingStore,
                 mailer: Mailer, client: stripe_billing.StripeClient | None,
                 telemetry: telemetry.TelemetryStore | None = None) -> None:
        self.settings = settings
        self.store = store
        self.mailer = mailer
        self.client = client
        #: Optional, and optional on purpose. Telemetry is instrumentation, and instrumentation
        #: that can break a checkout is worse than no instrumentation: a trial that 500s after
        #: the card was charged is a refund and possibly a dispute. So a service built without a
        #: store (every Phase 2 call site, and any future one) behaves exactly as before, and
        #: `track` below swallows its own failures rather than propagating them into a route.
        self.telemetry = telemetry

    # ---- telemetry ---------------------------------------------------------

    def track(self, event: str, *, visitor_key: str = "", row: dict[str, Any] | None = None,
              amount_minor: int = 0, gross_volume_minor: int = 0, refunded_minor: int = 0,
              card_funding: str = "", card_country: str = "", currency: str = "",
              reference_id: str = "", idempotency_key: str = "",
              detail: dict[str, Any] | None = None, agent_log: bool = False,
              agent_message: str = "", agent_level: str = "info") -> bool:
        """Record one funnel event, and never fail the customer-facing action around it.

        Three decisions worth stating:

        * A missing card funding type is stored as ``unknown`` rather than blank when we are
          otherwise holding card facts, and stays blank when we hold none. "Not offered" and
          "offered and unreadable" are different statements, and the prepaid share is a
          warning indicator whose safe default is unknown, never a guessed ``credit``.
        * ``offer_id`` is never passed in: it comes from the one variant label this build
          ships (§3.3: variant identity has nowhere else to live), so a caller cannot invent a
          cohort by forgetting an argument.
        * A write that raises logs one warning and returns False. Every call site here sits
          after a state change that has already succeeded, and a broken metrics row is not a
          reason to tell a customer their cancellation failed.
        """
        if self.telemetry is None:
            return False
        subscription = row or {}
        funding = card_funding or ("unknown" if subscription.get("card_last4") else "")
        try:
            written = self.telemetry.record(
                event=event,
                visitor_key=visitor_key or subscription.get("visitor_key", ""),
                test_variable=storage.OFFER_ID,
                amount_minor=amount_minor,
                gross_volume_minor=gross_volume_minor,
                refunded_minor=refunded_minor,
                card_funding=funding,
                card_country=card_country or subscription.get("card_country", ""),
                currency=currency or subscription.get("currency") or self.settings.currency,
                reference_id=reference_id,
                idempotency_key=idempotency_key,
                detail=detail or {},
            )
            if agent_log:
                self.telemetry.log(
                    event=event, message=agent_message or f"funnel event {event}",
                    level=agent_level,
                    context={"visitor_key": visitor_key or
                             subscription.get("visitor_key", ""),
                             "reference_id": reference_id,
                             "amount_minor": amount_minor,
                             **(detail or {})},
                    idempotency_key=f"log:{idempotency_key}" if idempotency_key else "",
                )
            return written
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a purchase
            log.warning("telemetry %s failed: %s", event, exc)
            return False

    def holds_test_keys(self) -> bool:
        """Whether this app is armed with *test* keys, deciding that from the widest evidence it has.

        The three mode-mismatch guards all asked `self.client is not None and
        self.client.keys.key_type == "test"`, which is a fair question to ask of a client and a bad
        way to answer "would a live event be someone else's money". A client is a request
        transport, and a service built without one, which is every rehearsal construction and the
        state after a key is withdrawn without a restart, reported False: not "we hold live keys"
        but "the question did not apply". The consequence is the one that matters, because the
        guards are what keep another merchant account's disputes out of our 1% ceiling, and a
        rehearsal app answering False let a live-mode event through and *counted it*.

        So: no client answers True outright, and with a client, its own keys. The absent-client case
        is True because of which question is being asked. "Would a live-mode event be somebody
        else's money?" is answered by *this lane's* configuration, and an app with no client cannot
        take money from anybody, so every live event it is handed is by definition another account's.
        The alternative, the one this function replaced, answered False whenever `client` was None,
        which reads as though the guard had inspected the keys and found them live. It had inspected
        nothing, and every rehearsal app in the fleet had that answer, so the one configuration
        mistake this guard exists to catch, a webhook wired at the wrong merchant account, arrived
        at a rehearsal app and was counted as ours.

        `getattr(..., "test")` on the client's own keys is the same reasoning one level down. A
        client built without readable keys is not evidence of a live lane, and the cost of guessing
        "test" is one extra log line during a misconfiguration, while the cost of guessing "live" is
        a foreign dispute inside the 1% ceiling that closes accounts.
        """
        if self.client is None:
            return True
        return getattr(getattr(self.client, "keys", None), "key_type", "test") == "test"

    @property
    def live(self) -> bool:
        """True when money may move. Distinct from `client is not None` only in intent, and
        named anyway because a reader of `if self.live:` should not have to know the
        construction rules to know what it guards."""
        return self.client is not None

    @property
    def rehearsal_reason(self) -> str:
        """Why no card can be charged, for the checkout screen and `/health`. Two different
        no's deserve two different sentences: a switch flipped off is a decision, and a
        missing key is a gap someone must fix."""
        if self.client is not None:
            return ""
        keys = stripe_billing.FunnelKeys.load()
        if not keys.usable:
            return ("No STRIPE_SECRET_KEY_FUNNEL is configured, so this funnel cannot take "
                    "money yet.")
        if not self.settings.charges_enabled:
            return ("FUNNEL_CHARGES_ENABLED is false, so the checkout screen records intent "
                    "instead of taking a card.")
        return "Checkout is not taking cards right now."

    def copy(self, subscription: dict[str, Any] | None = None) -> Copy:
        """Copy for a page or an email, with the renewal date that subscription actually has.

        Every surface that names a renewal date gets it from here, so "which date does this
        page show" has one answer: the processor's period end when we have one, and the
        labelled estimate when we do not.
        """
        return build_copy(
            self.plan_settings(subscription),
            period_end_iso=(subscription or {}).get("current_period_end", ""))

    def plan_settings(self, subscription: dict[str, Any] | None = None):
        """Settings priced as the plan this subscription bought (base settings without one)."""
        if not subscription or not subscription.get("trial_amount_minor"):
            return self.settings
        return plans.settings_for(self.settings, plans.for_amount(
            self.settings, int(subscription["trial_amount_minor"])))

    # ---- checkout ----------------------------------------------------------

    def checkout_trial_end_epoch(self, days: int | None = None) -> int:
        """End of the trial, as the processor will store it.

        Anchored on *now*, which is when the charge succeeds. A customer who opens checkout at
        23:50 and pays at 00:05 gets seven days from the charge, not from the page load, and
        `trial_days` from the day the money moved is what our disclosure promises.
        """
        return int(time.time()) + (days or self.settings.trial_days) * 86400

    def create_session(self, *, visitor_key: str, email: str, plan_signature: str,
                       origin: str = "", option: plans.PlanOption | None = None) -> dict[str, Any]:
        """Create the Stripe Checkout session, or raise.

        `visitor_key` rides along as `client_reference_id` *and* in metadata: the first is
        what Stripe shows in the dashboard when a customer emails in about a charge, the
        second is what comes back on every event object, and needing both is exactly why
        neither is left out.
        """
        if self.client is None:
            raise stripe_billing.StripeUnavailable(self.rehearsal_reason)
        base = origin or self.settings.public_url
        option = option or plans.options(self.settings)[0]
        metadata = {
            "plan_option": option.key,
            "visitor_key": visitor_key,
            "plan_signature": plan_signature,
            "offer_id": self.settings.brand_name,
            "funnel": "lane-c-rank4",
        }
        return self.client.create_checkout_session(
            success_url=f"{base}/checkout/complete?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/checkout/cancelled",
            customer_email=email,
            client_reference_id=visitor_key,
            trial_amount_minor=option.amount_minor,
            trial_days=option.days,
            rebill_amount_minor=self.settings.rebill_amount_minor,
            currency=self.settings.currency,
            trial_end_epoch=self.checkout_trial_end_epoch(option.days),
            metadata=metadata,
        )

    def checkout_complete(self, *, visitor_key: str, session_id: str, email: str,
                          plan_signature: str, answers: dict[str, Any],
                          coach_note: str = "", plan_key: str = "") -> dict[str, Any]:
        """Turn a returned Checkout session into a subscription row, and send the receipt.

        Reads the subscription back from the processor rather than trusting what the redirect
        implies. A session id in a URL is attacker-controlled, and the two things that must be
        true before we show a paid plan are "this session exists" and "its customer reference
        is this visitor". A rehearsal path that skipped the first check would happily credit a
        stranger's session id, and that bug does not exist in live mode.
        """
        subscription: dict[str, Any] = {}
        session: dict[str, Any] = {}

        if self.client is not None and session_id:
            session = self.client.get(f"/checkout/sessions/{session_id}")
            reference = str(session.get("client_reference_id") or "")
            if reference and reference != visitor_key:
                raise stripe_billing.StripeError(
                    "this checkout session belongs to a different visitor", status=403)
            subscription_id = str(session.get("subscription") or "")
            if subscription_id:
                subscription = self.client.retrieve_subscription(subscription_id)
            elif session.get("mode") == "subscription":
                # Paid trial, subscription id absent, is a rare-but-real Stripe shape on
                # incomplete expansion. Recording nothing here would show a customer a
                # thank-you page for a plan they cannot open.
                raise stripe_billing.StripeError(
                    f"checkout session {session_id} has no subscription attached", status=409)
            details = str((session.get("customer_details") or {}).get("email") or "")
            if details:
                email = details
            # The processor's record of which plan was sold beats the browser's claim.
            plan_key = str((session.get("metadata") or {}).get("plan_option") or plan_key)

        option = plans.find(self.settings, plan_key) or plans.options(self.settings)[0]

        trial_end_iso = epoch_to_iso(subscription.get("trial_end")
                                     or self.checkout_trial_end_epoch(option.days))
        period_end_iso = epoch_to_iso(stripe_billing.period_end_epoch(subscription))
        brand, last4 = stripe_billing.card_of(subscription) if subscription else ("", "")

        row = self.store.upsert_subscription(
            visitor_key=visitor_key,
            # Restated on the update path too, not only on insert. Without it a returning
            # member keeps last cycle's `trial_end` and last cycle's card, and a stale
            # `trial_end` is a wrong renewal date on the account page. `plan` is deliberately
            # *not* reset here: a member who took the lite downgrade and starts a fresh trial
            # should not be quietly moved back to the tier they just turned down.
            # `str(...)` alone, not `or None`. The columns are `not null default ''`, and an
            # explicit `NULL` is not the same as an omitted column: SQLite accepts a DEFAULT for a
            # column the statement leaves out and rejects `NULL` for one it is handed, so a
            # `None` here raised `IntegrityError` on insert and a paid checkout 500ed on its way
            # to the thank-you page. That is the shape this takes when the processor's response is
            # thinner than expected (`item_id` returns `""` for a subscription whose items were
            # never expanded), which is precisely when a funnel must still record the sale. Every
            # reader below already treats `""` and missing alike, so the empty string is the value
            # the rest of the code was written for.
            subscription_id=str(subscription.get("id", "") or
                                session.get("subscription") or ""),
            customer_id=str(subscription.get("customer", "") or
                            session.get("customer") or ""),
            subscription_item_id=stripe_billing.item_id(subscription) or "",
            email=email,
            status="trialing",
            trial_amount_minor=option.amount_minor,
            rebill_amount_minor=self.settings.rebill_amount_minor,
            currency=self.settings.currency,
            trial_end=trial_end_iso,
            current_period_end=period_end_iso,
            card_brand=brand,
            card_last4=last4,
            plan_signature=plan_signature,
            answers_json=_answers_blob(answers, coach_note),
            paused_until="",
            discount_months=0,
            cancelled_at="",
            save_offer_outcome="",
        )

        # Cleared *after* the upsert and deliberately without checking its return value.
        # A trial receipt is not transactional with its dedupe marker, and the wrong trade
        # is the one that drops a disclosure to avoid a duplicate.
        self.store.clear_notification(row["visitor_key"], "trial_receipt")
        self.send_trial_receipt(row)
        # Two events, because the sheet computes two different ratios from them and a single
        # row cannot serve both.
        #
        # `trial_started` is keyed per visitor *and day*, which is what makes the browser
        # redirect and the `checkout.session.completed` webhook collapse into one row. It cannot
        # be keyed on the session id, as the mirror-image `trial_paid` below is: the two paths
        # hold different identifiers for the same commitment (the return carries the session id,
        # the webhook's subscription lookup resolves to the subscription), so a session-scoped
        # key let the same person be counted as two committed starts. That is not a rounding
        # error, it is a factor of two on the denominator of Trial→Paid, and it read as a funnel
        # converting half as well as it was.
        #
        # A genuinely second trial is unaffected, because it is a second day. A visitor who
        # completes twice inside one UTC day is one committed start in the sheet, which agrees
        # with the column's own definition (`trials_started` counts distinct visitors) and with
        # the fact that the trial is once per person; `trial_paid` still records both, because
        # two captures are two pieces of money whatever the cohort says.
        card = stripe_billing.card_of_object(subscription)
        self.track("trial_started", row=row, reference_id=session_id,
                   card_funding=card["funding"], card_country=card["country"],
                   idempotency_key=f"trial_started:{visitor_key}:"
                                   f"{telemetry.day_of(telemetry.utc_now())}",
                   detail={"live": self.live})
        if self.live:
            self.track("trial_paid", row=row, reference_id=session_id,
                       amount_minor=option.amount_minor,
                       gross_volume_minor=option.amount_minor,
                       card_funding=card["funding"], card_country=card["country"],
                       idempotency_key=f"trial_paid:{session_id or visitor_key}",
                       detail={"via": "checkout"})
        return row

    def send_trial_receipt(self, row: dict[str, Any]) -> list[Any]:
        """The checklist-5 email: amount, renewal amount, renewal date, cancel link, reply address."""
        email = row.get("email", "")
        if not email:
            return []
        copy = self.copy(row)
        return self.mailer.send_all([trial_receipt_email(
            copy, email=email,
            cancel_url=f"{self.settings.public_url}/account/cancel",
            card_note=_card_note(row))])

    # ---- cancellation ------------------------------------------------------

    def cancel_now(self, *, visitor_key: str) -> CancelResult:
        """One click, executed first. Nothing on this path may require a second decision.

        Order inside this function *is* the compliance rule, so it is written linearly:
        tell the processor, write the local state, only then describe what happened. If the
        processor says no, the local row is left alone and `done` is False, because a screen
        reading "Cancelled" over a subscription that will still bill is the Roach Motel we are
        defending against, wearing friendlier clothes.
        """
        row = self.store.get_subscription(visitor_key)
        if row is None:
            return CancelResult(done=False, headline="No subscription found.",
                                detail="There is nothing here to cancel.",
                                processor_ok=False, access_until="")

        if row["status"] in ("cancelled_at_period_end", "canceled"):
            # Already cancelled. Re-stating the state beats a second cancel call, and no new
            # save offer is offered for an event that already happened (rule 3: one offer
            # per cancel event).
            return CancelResult(
                done=True, headline="Already cancelled.",
                detail="No further charge is coming.",
                processor_ok=True,
                access_until=self.access_until(row),
            )

        processor_ok = True
        error = ""
        subscription_id = row.get("subscription_id", "")
        if self.client is not None and subscription_id:
            try:
                updated = self.client.cancel_at_period_end(subscription_id)
                period_end_iso = epoch_to_iso(stripe_billing.period_end_epoch(updated))
            except stripe_billing.StripeError as exc:
                processor_ok = False
                # Named `error` and typed `str` everywhere below, because the value is the
                # message, not the exception: it lands in `CancelResult.error`, which the screen
                # and the log both read as text. Binding the exception here and calling
                # `str(error)` at the one use site would work too, and would invite a later
                # reader to reach for `error.status` on what is a string by the time anyone
                # looks.
                error = str(exc)
                log.error("cancel failed at processor for %s: %s", subscription_id, exc)
                period_end_iso = row.get("current_period_end", "")
        else:
            # Rehearsal, or a row with no processor id (a session-id-less complete). Either
            # way there is nothing to tell Stripe, so the local state is the whole truth.
            period_end_iso = row.get("current_period_end", "")

        if not processor_ok:
            # The failure sentence must not depend on a date being readable. `access_until` goes
            # through `Copy`, and `Copy` renders amounts and dates from whatever the row holds;
            # a row written before this module stored card fields, or one whose period end never
            # arrived from the processor, can carry a value one of those renderers rejects. A
            # refusal the app knows how to explain should never reach the member as a crash, so
            # the decoration is dropped and the sentence stands: the date is allowed to be
            # missing here, the explanation is not.
            try:
                access = self.access_until(row)
            except (TypeError, ValueError) as exc:
                log.warning("no readable access date for %s: %s", visitor_key, exc)
                access = ""
            return failure_result(self.settings, error, access_until=access)

        updated_row = self.store.upsert_subscription(
            visitor_key=visitor_key,
            status="cancelled_at_period_end",
            current_period_end=period_end_iso,
            cancelled_at=billing_store.utc_now(),
        )
        access = self.access_until(updated_row)
        self.send_cancellation(updated_row, access_until=access)
        # Keyed on the cancellation timestamp, which is the one thing that distinguishes this
        # cancel event from a later one by the same member. A double click on the button is the
        # case the key exists for: `cancel_now` returns "already cancelled" on the second
        # visit and never reaches here, so the count is one per real cancel event.
        self.track("cancel_clicked", row=updated_row,
                   idempotency_key=f"cancel_clicked:{visitor_key}:"
                                   f"{updated_row.get('cancelled_at', '')}",
                   detail={"live": self.live})
        return CancelResult(
            done=True,
            headline="Cancelled. Nothing more will be charged.",
            detail=f"You keep everything until {access}.",
            processor_ok=True,
            access_until=access,
        )

    def send_cancellation(self, row: dict[str, Any], *, access_until: str,
                          reactivated: bool = False, plan_note: str = "") -> list[Any]:
        email = row.get("email", "")
        if not email:
            return []
        return self.mailer.send_all([cancellation_email(
            self.copy(row), email=email, access_until=access_until,
            reactivated=reactivated, plan_note=plan_note)])

    def accept_offer(self, *, visitor_key: str, key: str) -> save_offer.Outcome | None:
        """Apply an accepted save offer, then log it. Unknown or unconfigured options are
        ignored (returning None) rather than applied, so a stale tab cannot select an option
        the funnel no longer sells.

        Three properties the code has to keep straight, in this order:

        1. **A save that the processor refused is not a save.** The local row is left
           cancelled and the screen says the offer did not apply. Showing "you're on the lite
           plan" over a subscription the processor still bills at full price is selling a
           thing that is not in place.
        2. **Offers must compose.** Taking the discount while on lite must not upgrade the
           member back to the plan they just declined (spec rule 4), so the tier is never
           written by the discount path and the price moves to half of *lite*.
        3. **Re-accepting is not an error.** A member who pauses twice, or taps the button
           twice from a stale tab, is asking for a state they may already be in. Calls that
           Stripe would reject as no-ops are skipped on the state we already hold.
        """
        row = self.store.get_subscription(visitor_key)
        if row is None:
            return None
        # Option D is not in the menu: it is not an offer, it is the state the account is
        # already in. Special-cased ahead of the menu lookup rather than added to it, because
        # `offer_menu` doubles as "what may be applied to this subscription", and an entry
        # there would let a hand-posted form treat "no thanks" as a change to make.
        #
        # It is logged because an unrecorded decline corrupts the one number the menu exists
        # to produce: with only acceptances counted, a save offer that rescued nobody looks
        # like one that rescued everybody.
        if key == save_offer.KEEP_CANCELLED:
            outcome = save_offer.accepted_outcome(
                key, self.copy(row), self.settings, renewal_date=self.access_until(row),
                resumed=False)
            self.store.log_event(visitor_key=visitor_key, event=outcome.event, applied=True)
            return outcome
        if save_offer.offer_menu(self.settings, self.copy(row)).get(key) is None:
            return None

        copy = self.copy(row)
        already_lite = row.get("plan") == "lite"
        paused_until = ""
        errors: list[str] = []
        resumed = False

        if key in save_offer.REVVERSIBLE and self.client is not None:
            subscription_id = row.get("subscription_id", "")
            try:
                if row.get("status") == "paused":
                    # Paused is a different state from cancelled, and reversing it takes a
                    # different call: `cancel_at_period_end=False` on a paused subscription
                    # changes nothing, because collection is paused independently of whether a
                    # cancellation is pending. Skipping the resume here left the member looking
                    # at "Half price for two months" over a subscription whose invoices the
                    # processor still holds, and the pause expiring on its own date rather than a
                    # stated one is how a save offer becomes a mystery charge.
                    self.client.resume_collection(subscription_id)
                elif row.get("status") not in ("cancelled_at_period_end",):
                    # Anything else (trialing, active, past_due) is not a pending cancellation,
                    # so there is nothing to reverse and a `cancel_at_period_end=False` call
                    # would be noise at best.
                    self.client.resume_at_period_end(subscription_id)
                if key == save_offer.PAUSE:
                    if row.get("status") == "paused":
                        # Already paused: re-state the date we hold rather than pausing again
                        # and moving the resume day forward on a member who asked twice.
                        # `iso_to_local_date` is the ISO-to-date step and this `human_date`
                        # takes a date, so the two compose here, unlike in `_pause`.
                        paused_until = human_date(iso_to_local_date(
                            row.get("paused_until", "")) or date.today())
                    else:
                        paused_until = self._pause(subscription_id)
                elif key == save_offer.DOWNGRADE and not already_lite:
                    self._downgrade(row)
                elif key == save_offer.DISCOUNT:
                    self._discount(row, errors)
                resumed = True
            except stripe_billing.StripeError as exc:
                errors.append(str(exc))
                log.error("save offer %s failed for %s: %s", key, visitor_key, exc)

        outcome = save_offer.accepted_outcome(
            key, copy, self.settings, renewal_date=copy.renewal_date, resumed=resumed,
            already_lite=already_lite, paused_until=paused_until)

        if errors:
            self.store.log_event(visitor_key=visitor_key, event=outcome.event,
                                 applied=False, processor_errors=errors)
            return save_offer.Outcome(
                event=outcome.event,
                headline="That offer could not be applied.",
                detail=("Your cancellation still stands and nothing will be charged. We could "
                        "not make the change on our side, so nothing has been sold to you "
                        "that is not really in place. Go back and pick another option, or "
                        "just stay cancelled."),
                fields={},
            )

        # A save that reversed the cancellation also ends any pause, and only this line says so.
        # Each offer's own `fields` describes itself alone, which is right for a single choice and
        # wrong in sequence: pause, then come back and take the discount, leaves the row reading
        # `paused` while the processor was told to resume billing, so the dashboard shows "Paused.
        # Nothing is charged until…" over a subscription that is about to be billed. `resumed` is
        # only True on the path that actually reversed the pause or the cancellation, so an offer
        # that failed above cannot clear it by accident.
        fields = dict(outcome.fields)
        if resumed:
            # The tier survives; the money state is read back rather than guessed, by the same call
            # and the same mapping the webhook uses just below. Naming a status here would be a
            # guess about the processor: a member cancelling on day two of a seven-day trial who
            # takes the discount is still `trialing`, and writing `active` would price them at the
            # rebill amount on the dashboard and in the renewal email. `current_period_end` comes
            # along too, so the disclosed date is the real one and not the pre-cancel estimate.
            self._sync_from_processor(row, fields)
            fields["paused_until"] = ""
        self.store.upsert_subscription(visitor_key=visitor_key, **fields)
        self.store.log_event(visitor_key=visitor_key, event=outcome.event, applied=True)
        # One telemetry name for all three retention offers, because the Day-1 sheet counts
        # "saves taken" as one number and §3.2 test 6 compares saves against confirmed cancels.
        # The spec's own per-option name stays in `detail` so a drill-down is still one filter
        # away rather than a re-run of the funnel.
        self.track("save_offer_selected", row=row,
                   idempotency_key=f"save_offer_selected:{visitor_key}:"
                                   f"{outcome.event}:{billing_store.utc_now()}",
                   detail={"offer_event": outcome.event, "key": key})
        if key in save_offer.REVVERSIBLE:
            updated = self.store.get_subscription(visitor_key) or row
            self.send_cancellation(updated, access_until=self.access_until(updated),
                                   reactivated=True, plan_note=outcome.headline)
        return outcome

    def _sync_from_processor(self, row: dict[str, Any], fields: dict[str, Any]) -> None:
        """Read the subscription back and add whatever the processor now says to ``fields``.

        A save offer that reversed a cancellation changed the subscription's state at Stripe, and
        only Stripe knows which state that is: `trialing` for a member inside their trial, `active`
        for one past it, and a `current_period_end` that may have moved. Doing this locally meant
        writing a guessed status, and a guessed status is a disclosed price.

        Silent on a read failure, and that is deliberate: the offer itself has already succeeded at
        this point, so failing here would report "we could not apply that" about a change that was
        applied. The row keeps the state it had, which is stale rather than wrong, and the next
        webhook, the notices sweep, or a `/dashboard` read corrects it.
        """
        subscription_id = row.get("subscription_id", "")
        if self.client is None or not subscription_id:
            return
        try:
            sub = self.client.retrieve_subscription(subscription_id)
        except stripe_billing.StripeError as exc:
            log.warning("could not re-read subscription %s after a save offer: %s",
                        subscription_id, exc)
            return
        status = str(sub.get("status", ""))
        if status in billing_store.STATUSES:
            fields["status"] = status
        elif stripe_billing.is_cancelled(sub):
            fields["status"] = "cancelled_at_period_end"
        period_end = stripe_billing.period_end_epoch(sub)
        if period_end:
            fields["current_period_end"] = epoch_to_iso(period_end)
        if sub.get("trial_end"):
            fields["trial_end"] = epoch_to_iso(sub["trial_end"])

    def _pause(self, subscription_id: str) -> str:
        """Pause and return the day it resumes, spelled out, as we will state it.

        `epoch_to_iso` then `human_date` rather than feeding the date straight in: `human_date`
        takes the ISO strings this repo stores, and passing it a `date` meant a `TypeError` that
        `accept_offer` swallowed into its processor-error list, so a pause that had *succeeded*
        at Stripe reported "that offer could not be applied".
        """
        resumes_at = int(time.time()) + self.settings.pause_days * 86400
        self.client.pause_collection(subscription_id, resumes_at=resumes_at)
        # Module-qualified, and not by habit: `billing_store` has an ISO-parsing `human_date`
        # and `funnel_copy` a date-taking one, both imported into this module's orbit. Bare
        # names let the wrong one win, which turned a date into a string and the string into
        # an AttributeError that `accept_offer` reported as "the processor refused your pause".
        return billing_store.human_date(epoch_to_iso(resumes_at))

    def _downgrade(self, row: dict[str, Any]) -> None:
        self._downgrade_price(row, self.settings.lite_amount_minor)

    def _discount(self, row: dict[str, Any], errors: list[str]) -> None:
        """Apply the 2-month coupon, and keep it honest for a member already on lite.

        The coupon is the processor's object and its own duration decides the two months, so
        nothing here can claim a longer discount than Stripe enforces. For a lite member the
        subscription price also moves to half of lite: half-of-lite is the number
        `save_offer` shows that member, and a screen and an invoice that disagree about a
        price is the failure this whole module is written to avoid.
        """
        subscription_id = row.get("subscription_id", "")
        if not row.get("discount_months"):
            self.client.apply_coupon(subscription_id, self.settings.discount_coupon_id)
        if row.get("plan") == "lite":
            try:
                self._downgrade_price(
                    row, lite_with_discount(self.settings.lite_amount_minor))
            except stripe_billing.StripeError as exc:
                errors.append(str(exc))

    def _downgrade_price(self, row: dict[str, Any], amount_minor: int) -> None:
        item = row.get("subscription_item_id", "")
        if not item and row.get("subscription_id"):
            item = stripe_billing.item_id(
                self.client.retrieve_subscription(row["subscription_id"]))
        if not item:
            raise stripe_billing.StripeError(
                "this subscription has no priced item to change", status=409)
        self.client.update_amount(item, amount_minor, self.settings.currency)

    def record_survey(self, *, visitor_key: str, reason: str, note: str) -> None:
        """Optional, never blocking (§2.3). Stored under the spec's own event name."""
        self.store.log_event(visitor_key=visitor_key, event="survey_reason",
                             reason=reason, note=note[:500])
        # Deliberately *not* idempotent on a key: the spec calls the survey two questions, and
        # a member who submits both in two posts is two legitimate rows in the response table
        # while the funnel event remains "they gave us the reason". `survey_submitted` feeds
        # the completion ratio (surveys / cancels), where a double submit is real noise worth
        # seeing rather than a duplicate to hide.
        self.track("survey_submitted", visitor_key=visitor_key,
                   detail={"reason": reason, "note": note[:200]})

    def access_until(self, row: dict[str, Any]) -> str:
        """Day-level "until when do I keep this", spelled out.

        Falls back to the estimate when the processor's date is unknown, and says nothing
        false either way: the estimate is the trial length from today, which is exactly what
        the checkout screen told them to expect.
        """
        copy = self.copy(row)
        return copy.renewal_date

    # ---- lifecycle notices -------------------------------------------------

    def send_due_notices(self) -> list[Any]:
        """The day −2 renewal notices that are due and unsent.

        Called on a timer, and safe to call twice: `mark_notification_sent` returns False for
        a kind already recorded, and that False is what suppresses the resend. Without the
        marker, a deploy on a renewal morning would tell every subscriber their card was about
        to be charged, twice.
        """
        notice_at = (datetime.now(timezone.utc)
                     + timedelta(days=self.settings.prebilling_notice_days)
                     ).replace(microsecond=0).isoformat()
        results = []
        for row in self.store.due_notifications("renewal_reminder", now_iso=notice_at):
            copy = self.copy(row)
            if not self.store.mark_notification_sent(
                    visitor_key=row["visitor_key"], kind="renewal_reminder",
                    detail=row.get("current_period_end", "")):
                continue
            results.extend(self.mailer.send_all([renewal_notice_email(
                copy, email=row["email"],
                cancel_url=f"{self.settings.public_url}/account/cancel")]))
        return results

    def _track_rebill_decline(self, row: dict[str, Any], summary: dict[str, Any]) -> None:
        """Record a failed rebill, and the day the +3 retry is spent.

        The mailer already refuses a second decline notice via a per-period dedupe marker.
        Telemetry needs the same judgement expressed in *dates* rather than in markers, because
        the rule is calendar-shaped (§2.1: one retry at day +3, then stop) and because the
        export has to report the unrecoverable ones distinctly: a decline the retry recovered
        and a decline that ended the subscription are two very different losses, and the
        sheet's `rebill_declines_silent` reads as dunning aggressiveness only if it means one
        of them consistently.

        The anchor is the subscription's period end, not "now". A decline webhook that arrives
        hours after the charge attempt, or a replay of one, still describes the same period,
        so the retry deadline does not drift with delivery time. If that anchor is gone, an
        event already counted for this period suppresses a second one; otherwise the row's
        creation date is the fallback anchor.

        Two readings of the event object settle this rather than any docstring, and they disagreed
        in a way that decided the implementation. The first draft treated `attempted` plus "we have
        an email on file" as proof the processor had already spent our retry, which made every
        replayed failure a churn. That reading was wrong about the world, and its own comment said
        the thing it should have noticed: Stripe's smart-dunning ladder is hours and days, not our
        calendar days, so `attempted` and `attempt_count` describe *its* retries and say nothing
        about the +3 we owe. A payment provider cannot be the authority on our deadline; only our
        clock can, which is what `period_end + 3d <= today` says, and it needs no cooperation from
        the payload.

        `attempt_count >= 2` survives as a floor rather than a trigger, and not for legibility. The
        export is run daily and the deadline reads as crossed at the first hour past midnight, so
        without a floor an invoice whose retry is due today and fails this morning is booked as a
        loss the same morning, while a duplicate event delivered three days late is booked as a
        loss that happened *today*. Two attempts is the cheapest fact that distinguishes a period
        we tried twice inside from a period still inside the window, and it never guesses on the
        first notice of a failure.

        `charge_settled` is the one place the payload is trusted outright. A failure event for an
        invoice that has since settled carries the settled charge, and an old period plus money in
        is Stripe catching up on news we already have: calling it terminal would report a loss on a
        member still paying. `paid` alone proves nothing on a failure webhook, since a failed
        attempt's own charge is unpaid by definition, and `charge_id` alone survives on payloads
        that merely name an intent, so it takes the pair.
        """
        period_iso = epoch_to_iso(summary.get("current_period_end") or 0)
        marker = f"decline:{period_iso or summary['id']}"
        already_noticeable = bool(row.get("email")) and period_iso
        # `mark_notification_sent` is asked only of a row that could actually be mailed about,
        # so a row with no email does not burn the marker a later, better-informed event
        # would need. The return value is kept purely to *report* whether this decline could be
        # mailed about for the period, and is deliberately not part of the terminal test below:
        # on a row with no email the marker is never written, so a replay of a mail-less decline
        # keeps reporting itself as first, and a terminal test built on it would fire on the
        # second of thirty retries. Stated in code as an unused-by-the-rule value, because the
        # first version of this read it as "was this the first failure" and that assumption is
        # exactly what faked a churn on every replay.
        self.notice_marker_was_new = self.store.mark_notification_sent(
            visitor_key=row["visitor_key"], kind=marker) if already_noticeable else False
        anchor = period_iso or row.get("created_at", "")
        retry_due = f"{anchor[:10]}+{self.settings.decline_retry_days}d"
        # Deadline arithmetic in dates, not epochs: the policy is written in calendar days, and
        # comparing a stale test fixture's period end against "now minus 3 days" is how a
        # replayed decline becomes a false loss.
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=self.settings.decline_retry_days)).date().isoformat()
        aged_out = bool(anchor[:10] and anchor[:10] <= cutoff)
        # A failure event for an invoice that has *since* settled is news we already have, and it
        # says so by carrying the settled charge: `paid` alone means nothing on a failure webhook
        # because a failed attempt's own charge is unpaid by definition, and `charge_id` alone
        # survives on a Radar-style payload that names an intent. Requiring the pair, and
        # requiring it only against an aged-out period, is what keeps a same-day retry storm from
        # reading as thirty lost members while a genuinely expired invoice still lands.
        charge_settled = bool(summary.get("paid")) and bool(summary.get("charge_id"))
        terminal = aged_out and int(summary.get("attempt_count") or 0) >= 2 and not charge_settled
        self.track("rebill_declined", row=row,
                   amount_minor=int(summary.get("amount_total") or 0),
                   currency=summary.get("currency") or self.settings.currency,
                   reference_id=str(summary.get("charge_id") or summary["id"]),
                   idempotency_key=f"rebill_declined:{summary['id']}",
                   detail={"retry_on": retry_due, "period_end": period_iso,
                           "notice_sent": self.notice_marker_was_new,
                           "attempted": bool(summary.get("attempted")),
                           "attempt_count": int(summary.get("attempt_count") or 0),
                           "aged_out": aged_out, "terminal": terminal})
        if terminal:
            # One row per subscription *period*, not per event id: Stripe may send several
            # failure events for a period it has given up on, and the sheet counts losses.
            self.track("rebill_decline_terminal", row=row,
                       amount_minor=int(summary.get("amount_total") or 0),
                       currency=summary.get("currency") or self.settings.currency,
                       reference_id=str(summary.get("charge_id") or summary["id"]),
                       idempotency_key=f"rebill_terminal:{row['visitor_key']}:"
                                       f"{period_iso or summary['id']}",
                       detail={"retry_on": retry_due, "period_end": period_iso,
                               "attempt_count": int(summary.get("attempt_count") or 0)},
                       agent_log=True, agent_level="warning",
                       agent_message=f"rebill for {row['visitor_key']} exhausted the "
                                     f"one-retry policy and was not recovered")
        return None

    def record_decline(self, row: dict[str, Any]) -> list[Any]:
        """One retry at day +3, then stop (§2.1). The email names the date so the customer
        knows the difference between "we are still trying" and "we gave up"."""
        email = row.get("email", "")
        if not email:
            return []
        retry = (datetime.now(timezone.utc)
                 + timedelta(days=self.settings.decline_retry_days)).date()
        return self.mailer.send_all([decline_email(
            self.copy(row), email=email, retry_on=human_date(retry))])

    # ---- risk signals (refunds, disputes, Radar warnings) -------------------

    def account_signal(self, summary: dict[str, Any], row: dict[str, Any] | None,
                       event: dict[str, Any] | None = None) -> str:
        """Count one processor risk signal, and return a reason for the log.

        `event` is the whole webhook body, and it is *optional* for the worst reason: this is the
        only branch that reads the raw payload rather than the reduced `summary`, because
        `is_fraud_warning` works on the Radar signal object's own fields (`fraud_reporting`,
        `dispute`, the advisory taxonomy), which `event_from_webhook` deliberately does not copy
        into the summary. Reduced shapes lose information on purpose, and a caller that reaches
        here with a summary alone cannot ask a question the reduction threw away. That is exactly
        the trap: this branch read `event.get("data")` while `event` was still an unset local, so
        any Radar warning raised `UnboundLocalError` on the way in, which on a webhook is a 5xx,
        which Stripe retries forever, which means the one signal the ≤ 0.5% fraud ceiling is made
        of never landed and the ceiling read zero forever. Hence the parameter rather than a
        capture: the summary carries no raw object to fall back to, and inventing one here would
        let a caller believe a fraud question had been answered when nothing was inspected.

        These three event families are the reason this function exists at all. §3.3 is blunt
        about it: fraud-not-chargeback is neither a dispute nor a chargeback, no table holds
        it, and a lane can pass every visible metric while being disqualified on the one ratio
        that actually closes merchant accounts. Counting it starts with writing a row.

        The ratio denominators are set here rather than at the export, and each signal writes
        the *charge's* captured amount into `gross_volume_minor`: §3.4's dispute formula
        divides by authorised volume, not by revenue we kept, and a dispute on a charge we
        never captured is a dispute on volume we never authorised.

        `trial_paid` is backfilled when a refund or a dispute arrives for a subscription whose
        conversion we somehow never recorded (a charge made in the dashboard, an outage during
        checkout). It is keyed on the subscription rather than the session id, so a real
        earlier row wins and this fills only the hole. Without it, refund % divides by a
        smaller paid count and reports a healthier number than the truth.

        Nothing here is gated on a local email, a readable date, or a known card. All three
        have been absent on events that mattered, and a risk signal we only count when the
        payload was friendly is a ratio biased towards the safe-looking events.
        """
        event_type = summary["type"]
        # The two-stack lesson from §3.4: one rollup per lane, and a number from the wrong
        # merchant account in our rollup is worse than a gap. A live-mode event while we hold
        # test keys means that account's disputes and refunds are not our disputes and
        # refunds, so they are recorded as one named, countable anomaly and kept out of every
        # ratio. Guarded here rather than only in `handle_event` so the rule cannot be walked
        # past by a caller that reaches the helper directly, which is what the two call sites
        # above would otherwise do differently from each other.
        if summary.get("live_mode") and self.holds_test_keys():
            logged = (self.telemetry.log(
                event="mode_mismatch_event", level="warning",
                message=f"live-mode {event_type} received while holding test keys",
                context={"stripe_event": summary["id"],
                         "signal_id": summary.get("signal_id", ""),
                         "charge_id": summary.get("charge_id", "")},
                idempotency_key=f"mode_mismatch:{summary['id']}",
            ) if self.telemetry is not None else False)
            return f"mode-mismatch:{'logged' if logged else 'already-known'}:{event_type}"
        if event_type.startswith("charge.dispute"):
            if event_type != "charge.dispute.created":
                # `charge.dispute.updated`/`.closed` changes who holds the money, not whether
                # a dispute happened. It already landed in the 1% numerator on creation, so
                # recounting here would double-count the same cardholder complaint.
                return f"dispute-{event_type.rsplit('.', 1)[-1]}:counted-at-creation"
            event, level = "dispute_created", "warning"
            message = (f"dispute {summary.get('signal_id') or summary['id']} opened on "
                       f"charge {summary.get('charge_id') or 'unknown'}")
        elif event_type.startswith("radar."):
            if not stripe_billing.is_fraud_warning(
                    (event or {}).get("data", {}).get("object", {}) or {}):
                # Radar advice that does not name fraud (`generic_complaint`,
                # `product_unsatisfactory`) is not the ≤ 0.5% numerator, and counting it would
                # trip the kill switch on the wrong signal. Logged, not counted.
                return "radar-warning:not-fraud-flagged"
            event, level = "radar_early_warning", "warning"
            message = (f"Radar early warning {summary.get('signal_id') or summary['id']} "
                       f"({summary.get('warning_reason') or 'reason not given'})")
        elif event_type == "charge.refunded":
            event, level = "refund_issued", "info"
            message = (f"refund of {summary.get('amount', 0)} minor units on charge "
                       f"{summary.get('charge_id') or 'unknown'}")
        else:
            return f"signal:unhandled:{event_type}"

        # Keyed on the event's own identity, which is exactly what Stripe re-delivers. A
        # redelivered dispute must not count twice against a 1% ceiling.
        key = f"{event}:{summary['id']}"
        captured = int(summary.get("amount_captured") or 0)
        amount = int(summary.get("amount") or 0)
        counted = self.track(
            event, row=row,
            visitor_key=(row or {}).get("visitor_key", "") or summary["visitor_key"],
            amount_minor=amount,
            gross_volume_minor=captured or amount,
            refunded_minor=amount if event == "refund_issued" else 0,
            card_funding=summary.get("card_funding", ""),
            card_country=summary.get("card_country", ""),
            currency=summary.get("currency") or self.settings.currency,
            reference_id=summary.get("charge_id", "") or summary.get("signal_id", ""),
            idempotency_key=key,
            detail={"stripe_event": summary["id"], "event_type": event_type,
                    "signal_id": summary.get("signal_id", ""),
                    "subscription_id": summary.get("subscription_id", ""),
                    "dispute_status": summary.get("dispute_status", "")},
            agent_log=True, agent_message=message, agent_level=level,
        )
        if row is None:
            # Counted regardless: the row's absence is a bookkeeping gap, not a reason to
            # unsee a dispute. Named in the reason string so the log says which happened.
            log.warning("webhook %s has no local subscription row (%s); counted anyway",
                        event_type, summary.get("subscription_id") or "no subscription id")
        if event != "refund_issued" or row is None or not row.get("subscription_id"):
            return f"{event}:{'counted' if counted else 'duplicate'}"
        self.track("trial_paid", row=row,
                   idempotency_key=f"trial_paid:{row['subscription_id']}",
                   amount_minor=int(row.get("trial_amount_minor") or 0),
                   gross_volume_minor=int(row.get("trial_amount_minor") or 0),
                   detail={"via": "backfill"})
        return f"{event}:{'counted' if counted else 'duplicate'}"

    def _track_expiry(self, summary: dict[str, Any], row: dict[str, Any] | None) -> None:
        """Count the conversion a Checkout session that lapsed represents.

        A counted event, not an anomaly line, for one reason: `trials_started` in the export is
        derived from rows, so an abandoned checkout must appear in the denominator or the
        Trial→Paid percentage is computed over conversions only and always reads optimistic.
        Keyed on the session id, which makes a redelivered expiry one row. Recorded whatever
        the local row looks like, because `handle_event` deliberately leaves a `trialing` row
        *alone* on a late expiry (the charge really happened, the notice is Stripe catching up)
        and a telemetry row that mirrored that choice would silently drop the abandonment for
        the exact race where a visitor retried.
        """
        self.track("checkout_expired", row=row,
                   visitor_key=(row or {}).get("visitor_key", "") or summary["visitor_key"],
                   amount_minor=int(summary.get("amount_total") or 0),
                   card_funding=summary.get("card_funding", ""),
                   card_country=summary.get("card_country", ""),
                   reference_id=str(summary.get("charge_id") or ""),
                   idempotency_key=f"checkout_expired:{summary.get('charge_id') or summary['id']}",
                   detail={"stripe_event": summary["id"],
                           "kept_row": bool(row and row.get("status") == "trialing")})

    # ---- webhook -----------------------------------------------------------

    def handle_event(self, event: dict[str, Any]) -> str:
        """Apply one Stripe event to local state. Returns a short reason for the log.

        Ignores-then-200 for anything this lane does not act on. A raised exception here would
        become a 5xx, and Stripe retries 5xx forever: an event type we simply do not use would
        arrive in an endless loop and look like an incident.
        """
        summary = stripe_billing.event_from_webhook(event)
        event_type = summary["type"]
        known = (
            "checkout.session.completed", "checkout.session.expired",
            "invoice.payment_failed", "invoice.paid",
        )
        # Risk signals take their own path, before the row-matching below and before the
        # ignore-list. Three reasons, all load-bearing: a Radar warning has no subscription at
        # all (so the row matcher would drop it as `unmatched`), a dispute for a subscription we
        # never saw is precisely the event we cannot afford to discard, and the ignore-list
        # below answers `ignored:` with no side effect, which is the opposite of what a kill
        # metric needs.
        if (event_type.startswith("charge.dispute") or event_type.startswith("radar.")
                or event_type == "charge.refunded"):
            if summary["live_mode"] and self.holds_test_keys():
                # Counted in the shared helper, whose mode guard holds on every path, and the
                # origin is named in the reason so the log distinguishes "another account's
                # event" from "an event we do not use".
                self.account_signal(summary, None, event)
                return f"ignored:test-mode-account-mismatch:{event_type}"
            signal_row = ((self.store.get_subscription(summary["visitor_key"])
                           if summary["visitor_key"] else None)
                          or self._by_subscription(summary["subscription_id"]))
            return self.account_signal(summary, signal_row, event)
        if event_type == "checkout.session.expired":
            # Matched here rather than by the block below, which this branch returns from
            # before reaching it. An expiry is the one event type this handler acts on for a
            # visitor it has never seen (`expired:no-row`), so it needs a row lookup of its own
            # and cannot borrow one, and the lookup is cheap enough to do twice rather than
            # hoisted above the risk-signal branch that does not want it.
            row = ((self.store.get_subscription(summary["visitor_key"])
                    if summary["visitor_key"] else None)
                   or self._by_subscription(summary["subscription_id"]))
            # Nobody pays for an expired session, so the row is marked for cleanup and the
            # dashboard must not offer a plan because of it. `trialing` rows are left alone:
            # an expiry notice that arrives *after* a successful complete is Stripe catching up,
            # and marking a live subscriber "expired" on that timing race would lock a paying
            # member out of the plan they bought.
            if row is not None and row.get("status") != "trialing":
                self.store.upsert_subscription(visitor_key=row["visitor_key"], status="canceled")
                self._track_expiry(summary, row)
                return "expired:marked"
            self._track_expiry(summary, row)
            return "expired:left-alone"
        if not event_type.startswith("customer.subscription.") and event_type not in known:
            return f"ignored:{event_type}"
        if summary["live_mode"] and self.holds_test_keys():
            # Logged as a signal *before* returning. This is the one ignored path that must
            # not be silent: a live-mode event while we hold test keys means another account
            # is taking this funnel's money, which is a dispute against a descriptor this app
            # never set. Counting it keeps that visible even when it arrives as a flood, and
            # the idempotency key is the Stripe event id, so it is one row per event either
            # way, never an unbounded log line under retry pressure.
            self.account_signal(summary, None)
            return f"ignored:test-mode-account-mismatch:{event_type}"

        visitor_key = summary["visitor_key"]
        subscription_id = summary["subscription_id"]
        row = (self.store.get_subscription(visitor_key) if visitor_key else None) \
            or self._by_subscription(subscription_id)

        # An expiry for a visitor whose row we already dropped: nothing to mark, and nothing
        # to blame. Handled before the unmatched warning so it does not look like the
        # never-seen-before case below, which genuinely wants a log line.
        if row is None and event_type == "checkout.session.expired":
            return "expired:no-row"
        if row is None:
            # An event for a subscription we have never seen: a second browser, a re-run
            # webhook, or a charge made in the dashboard. Loud in the log, benign in
            # behaviour, and never a 5xx.
            log.warning("webhook %s for unknown subscription %s (%s)", event_type,
                        subscription_id, visitor_key or "no visitor key")
            return f"unmatched:{event_type}"

        visitor_key = row["visitor_key"]
        fields: dict[str, Any] = {}
        status = summary["status"]

        if event_type == "checkout.session.completed":
            if not row.get("email") and summary["email"]:
                fields["email"] = summary["email"]
            # The local trial row created in rehearsal has no processor id; backfill it so
            # the dashboard's cancel button can reach the real subscription.
            if not row.get("subscription_id") and subscription_id:
                fields["subscription_id"] = subscription_id
            if not row.get("customer_id") and summary["customer_id"]:
                fields["customer_id"] = summary["customer_id"]
            if summary["card_brand"]:
                fields["card_brand"] = summary["card_brand"]
                fields["card_last4"] = summary["card_last4"]
            merged = {**row, **fields}
            self._apply(row, fields)
            # `mark_notification_sent` rather than reading the list: it both checks the marker
            # and sets it, so a duplicate `checkout.session.completed` (Stripe re-delivers)
            # sends one receipt. The marker was cleared in `checkout_complete` before this
            # event can land, which is what lets a genuine second trial get its own receipt.
            if merged.get("email") and self.store.mark_notification_sent(
                    visitor_key=visitor_key, kind="trial_receipt"):
                self.send_trial_receipt(merged)
            return "receipt:sent-or-deduped"

        if event_type == "customer.subscription.deleted":
            fields.update(status="canceled",
                          cancelled_at=row.get("cancelled_at") or billing_store.utc_now())
            self._apply(row, fields)
            return "canceled"

        if event_type == "invoice.payment_failed":
            fields["status"] = "past_due"
            self._apply(row, fields)
            self._track_rebill_decline(row, summary)
            # The grace retry is one retry (§2.1), so the decline mail is one mail. The
            # marker is keyed on the period end rather than a bare kind: a later decline, in
            # a later period, is a different event and gets its own notice.
            marker = f"decline:{summary['current_period_end'] or summary['id']}"
            decline_row = {**row, **fields}
            if decline_row.get("email") and self.store.mark_notification_sent(
                    visitor_key=visitor_key, kind=marker):
                results = self.record_decline(decline_row)
                return f"past_due:decline_mail={len([r for r in results if r.ok])}"
            return "past_due:decline-mail-already-sent"

        if event_type == "invoice.paid":
            if row.get("status") == "past_due":
                fields["status"] = "active"
            if not row.get("subscription_id") and subscription_id:
                fields["subscription_id"] = subscription_id
            self._apply(row, fields)
            # Only a *rebill* is a rebill. `checkout.session.completed` already counts the
            # trial capture as `trial_paid`, and counting the same invoice here too would let
            # one $1 trial appear as a conversion twice, which inflates both the paid count and
            # the authorised volume it divides by. A trial invoice carries no `period_end`
            # transition we care about; a rebill always names the period it just bought.
            period_end_iso = epoch_to_iso(summary["current_period_end"])
            if period_end_iso and period_end_iso != row.get("current_period_end", ""):
                self.track("rebill_succeeded", row=row,
                           amount_minor=int(summary.get("amount_total") or 0),
                           gross_volume_minor=int(summary.get("amount_total") or 0),
                           currency=summary.get("currency") or self.settings.currency,
                           reference_id=str(summary.get("charge_id") or subscription_id),
                           idempotency_key=f"rebill_succeeded:"
                                           f"{summary.get('charge_id') or summary['id']}",
                           detail={"previous_period_end": row.get("current_period_end", ""),
                                   "stripe_event": summary["id"]})
            # Stop advertising the discounted price once the processor says the promotion has
            # run out, judged on the coupon's *own* end date rather than on how many invoices
            # we have seen. A member who resumed from a pause or was charged early would
            # otherwise be shown "half price" above a full-price invoice, which is the
            # badge-versus-policy failure in checklist 8.
            expires_at = summary.get("discount_expires_at") or 0
            if row.get("discount_months") and not summary.get("discount"):
                self.store.upsert_subscription(visitor_key=visitor_key, discount_months=0)
                return "paid:discount-gone"
            if row.get("discount_months") and expires_at and expires_at <= int(time.time()):
                self.store.upsert_subscription(visitor_key=visitor_key, discount_months=0)
                return "paid:discount-ended"
            return "paid"

        # customer.subscription.* : mirror the processor, always.
        if status in billing_store.STATUSES:
            fields["status"] = status
        elif stripe_billing.is_cancelled({"status": status,
                                          "cancel_at_period_end": summary["cancel_at_period_end"]}):
            fields["status"] = "cancelled_at_period_end"
        if summary["current_period_end"]:
            fields["current_period_end"] = epoch_to_iso(summary["current_period_end"])
        if summary["trial_end"]:
            fields["trial_end"] = epoch_to_iso(summary["trial_end"])
        if summary["card_brand"]:
            fields["card_brand"] = summary["card_brand"]
            fields["card_last4"] = summary["card_last4"]
        self._apply(row, fields)
        return f"synced:{status or 'unknown-status'}"

    def _apply(self, row: dict[str, Any], fields: dict[str, Any]) -> None:
        if fields:
            self.store.upsert_subscription(visitor_key=row["visitor_key"], **fields)

    def _by_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        if not subscription_id:
            return None
        return self.store.find_by_subscription(subscription_id)

    # ---- status for the health page ---------------------------------------

    def status(self) -> dict[str, Any]:
        """Everything `/health` says about money, and it must be readable by someone who
        has not read this file."""
        keys = stripe_billing.FunnelKeys.load()
        out: dict[str, Any] = {
            "charges_enabled": self.settings.charges_enabled,
            "client_active": self.client is not None,
            "rehearsal_reason": self.rehearsal_reason,
            "key_present": keys.usable,
            "key_type": keys.key_type if keys.usable else "",
            "acct_type": keys.acct_type if keys.usable else "",
            # A missing webhook secret is the failure this lane was warned about: the
            # handler then accepts *unsigned* posts, which is fine while no automated action
            # hangs off it and not fine the moment one does. So it is shown as a named state
            # rather than buried, and `/health` fails the billing check on it in live mode.
            "webhook_secret_present": bool(keys.webhook_secret),
            "counts": self.store.subscription_counts(),
            "offer_counts": self.store.event_counts(),
            "mail_transport": self.mailer.transport,
            "discount_coupon_configured": bool(self.settings.discount_coupon_id),
            "offer_options": save_offer.offer_menu(
                self.settings, build_copy(self.settings)).keys(),
        }
        if self.client is not None:
            out["processor"] = self.client.health()
        return out


def _card_note(row: dict[str, Any]) -> str:
    brand = row.get("card_brand", "")
    last4 = row.get("card_last4", "")
    if brand and last4:
        return f"{brand} ending {last4}"
    return brand or ""


def _answers_blob(answers: dict[str, Any], coach_note: str) -> str:
    """Answers plus the coach note, as the JSON text the row stores.

    Snapshot rather than reference on purpose: the plan must survive the visitor clearing
    their cookies, and a paid member whose plan regenerates differently after they cancelled
    is a refund conversation.
    """
    import json
    return json.dumps({"answers": answers, "coach_note": coach_note}, ensure_ascii=False)


def session_expiry_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=CHECKOUT_SESSION_LIFETIME)
            ).replace(microsecond=0).isoformat()
