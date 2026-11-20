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
import stripe_billing
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
                 mailer: Mailer, client: stripe_billing.StripeClient | None) -> None:
        self.settings = settings
        self.store = store
        self.mailer = mailer
        self.client = client

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
            self.settings,
            period_end_iso=(subscription or {}).get("current_period_end", ""))

    # ---- checkout ----------------------------------------------------------

    def checkout_trial_end_epoch(self) -> int:
        """End of the trial, as the processor will store it.

        Anchored on *now*, which is when the charge succeeds. A customer who opens checkout at
        23:50 and pays at 00:05 gets seven days from the charge, not from the page load, and
        `trial_days` from the day the money moved is what our disclosure promises.
        """
        return int(time.time()) + self.settings.trial_days * 86400

    def create_session(self, *, visitor_key: str, email: str, plan_signature: str,
                       origin: str = "") -> dict[str, Any]:
        """Create the Stripe Checkout session, or raise.

        `visitor_key` rides along as `client_reference_id` *and* in metadata: the first is
        what Stripe shows in the dashboard when a customer emails in about a charge, the
        second is what comes back on every event object, and needing both is exactly why
        neither is left out.
        """
        if self.client is None:
            raise stripe_billing.StripeUnavailable(self.rehearsal_reason)
        base = origin or self.settings.public_url
        metadata = {
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
            trial_amount_minor=self.settings.trial_amount_minor,
            trial_days=self.settings.trial_days,
            rebill_amount_minor=self.settings.rebill_amount_minor,
            currency=self.settings.currency,
            trial_end_epoch=self.checkout_trial_end_epoch(),
            metadata=metadata,
        )

    def checkout_complete(self, *, visitor_key: str, session_id: str, email: str,
                          plan_signature: str, answers: dict[str, Any],
                          coach_note: str = "") -> dict[str, Any]:
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

        trial_end_iso = epoch_to_iso(subscription.get("trial_end")
                                     or self.checkout_trial_end_epoch())
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
            trial_amount_minor=self.settings.trial_amount_minor,
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
        if event_type == "checkout.session.expired":
            # Nobody pays for an expired session, so the row is marked for cleanup and the
            # dashboard must not offer a plan because of it. `trialing` rows are left alone:
            # an expiry notice that arrives *after* a successful complete is Stripe catching up,
            # and marking a live subscriber "expired" on that timing race would lock a paying
            # member out of the plan they bought.
            if row is not None and row.get("status") != "trialing":
                self.store.upsert_subscription(visitor_key=row["visitor_key"], status="canceled")
                return "expired:marked"
            return "expired:left-alone"
        if not event_type.startswith("customer.subscription.") and event_type not in known:
            return f"ignored:{event_type}"
        if summary["live_mode"] and self.client is not None and \
                self.client.keys.key_type == "test":
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
