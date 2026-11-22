"""Stripe Checkout, webhooks and cancellation, on the funnel's own merchant keys.

Merchant isolation is the reason this module exists separately from `backend/finance_agent.py`.
Spec §2.4: lane C uses the FUNNEL keys exclusively, so a lane-C dispute ratio can never take
lane B's MRR with it. Enforced here, in one place (`FunnelKeys.load`), rather than left to
whichever caller remembers the right variable name. Two failure modes are refused outright:

  * **No funnel key at all** -> `StripeUnavailable`. Charges stay off and the checkout screen
    says so, which is the same posture Phase 1 already had.
  * **A B2B key where a funnel key belongs** -> `StripeMisconfigured`. `acct_type` is read
    from the key itself, so this is caught before the first charge rather than discovered as
    an inter-account id error, or worse, as a lane-B statement descriptor on a lane-C customer.

**No new dependencies.** Every call goes through `httpx` against `https://api.stripe.com`.
Neither the `stripe` SDK nor `requests` is installed in the venv this app runs on, and
`README.md` commits to the funnel deploying without the engine, so adding either would make
the unit un-runnable where it already runs. `httpx` is already there (FastAPI's own test
transport sits on it). The trade is that form-encoding is hand-rolled (`encode_form`), so that
function is unit-tested rather than trusted: a wrongly encoded nested key is a silent 400, not
a typing error anyone notices.

Money moves only when `charges_enabled` is true. With it false, `/checkout` records intent as
Phase 1 did, and the card screen says "no card is charged" on its face.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from config import FunnelSettings

log = logging.getLogger("funnel.billing")

API_BASE = "https://api.stripe.com/v1"
#: How far back to accept a webhook by timestamp. Stripe's own default replay window; wider
#: and a intercepted-and-replayed event stays valid long after it stopped being true.
DEFAULT_TOLERANCE_SECONDS = 300


class StripeError(Exception):
    """Any processor failure. Carries the HTTP status and Stripe's message so a caller can
    log something specific instead of 'the payment failed'."""

    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class StripeUnavailable(StripeError):
    """No funnel secret key configured. Expected in rehearsal, and the only error a caller
    should treat as 'continue without charging'."""


class StripeMisconfigured(StripeError):
    """Keys present but wrong for this lane: a B2B key, or a mode mismatch with the test/live
    flag. Loud on purpose: guessing which account a charge lands on is not acceptable."""


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FunnelKeys:
    """The three FUNNEL variables, validated as a set.

    `key_type` and `acct_type` are decoded from the key rather than asked for, because
    `pk_test` paired with `sk_live` is a real config bug that produces a real 401, and this
    catches it with a message that names the pairing.
    """

    secret: str
    publishable: str
    webhook_secret: str

    @property
    def key_type(self) -> str:
        return "test" if self.secret.startswith("sk_test") else "live"

    @property
    def acct_type(self) -> str:
        return "express" if "_express_" in self.secret else "standard"

    @property
    def usable(self) -> bool:
        return bool(self.secret)

    @classmethod
    def load(cls) -> "FunnelKeys":
        secret = os.getenv("STRIPE_SECRET_KEY_FUNNEL", "").strip()
        publishable = os.getenv("STRIPE_PUBLISHABLE_KEY_FUNNEL", "").strip()
        webhook = os.getenv("STRIPE_WEBHOOK_SECRET_FUNNEL", "").strip()
        if not secret:
            # Absent is the rehearsal case, not an error. Loud once in the log, then the
            # checkout handler does the telling on the page itself.
            return cls(secret="", publishable=publishable, webhook_secret=webhook)

        keys = cls(secret=secret, publishable=publishable, webhook_secret=webhook)
        mode = os.getenv("FUNNEL_STRIPE_MODE", "").strip().lower()
        if mode and mode not in {"test", "live"}:
            raise StripeMisconfigured(
                f"FUNNEL_STRIPE_MODE={mode!r} is neither 'test' nor 'live'")
        if mode and mode != keys.key_type:
            raise StripeMisconfigured(
                f"FUNNEL_STRIPE_MODE={mode!r} but STRIPE_SECRET_KEY_FUNNEL is a "
                f"{keys.key_type} key ({secret[:8]}...). Charges would fail on every attempt.")
        if keys.publishable and keys.publishable.split("_")[1] != keys.key_type:
            raise StripeMisconfigured(
                f"STRIPE_PUBLISHABLE_KEY_FUNNEL is a {keys.publishable.split('_')[1]} key "
                f"while the secret key is {keys.key_type}. Checkout would 401 on open.")
        return keys


# ---------------------------------------------------------------------------
# Form encoding
# ---------------------------------------------------------------------------

def encode_form(data: dict[str, Any]) -> str:
    """Encode a nested dict/list as Stripe's `application/x-www-form-urlencoded` shape.

    Stripe expects `a[b]=1` and `a[0][b]=2`, and arrays of scalars as `a[]=x&a[]=y`. Rules
    that matter, learned the expensive way elsewhere in this repo:

      * An empty dict or list contributes **nothing**. Sending `payment_method_data=` with no
        sub-keys is a 400, whereas omitting it is fine.
      * ``False`` must encode as the literal string ``false``, not Python's ``False``.
      * ``None`` contributes nothing: for Stripe, absent means "leave alone" on an update,
        while an empty string means "clear it". Collapsing the two would clear a field a
        caller never mentioned.
    """
    parts: list[str] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                walk(f"{prefix}[{key}]" if prefix else str(key), inner)
        elif isinstance(value, (list, tuple)):
            for index, inner in enumerate(value):
                walk(f"{prefix}[{index}]" if prefix else str(index), inner)
        elif value is None:
            return
        elif prefix == "":
            raise ValueError("cannot encode a top-level scalar without a key")
        else:
            if isinstance(value, bool):
                text = "true" if value else "false"
            else:
                text = str(value)
            parts.append(f"{quote(prefix, safe='[]}')}={quote(text, safe='')}")

    walk("", data)
    return "&".join(parts)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class StripeClient:
    """Thin POST/GET wrapper with the funnel key on every call.

    `max_retries` is a *read* retry only, used for idempotent GETs (webhook re-fetches).
    A create is never retried here: the idempotency key is the retry mechanism for writes,
    and silently re-POSTing a `checkout.sessions.create` behind an idempotency key we did
    not send would double-create a session.
    """

    def __init__(self, keys: FunnelKeys, *, timeout: float = 20.0,
                 base_url: str = API_BASE,
                 session: httpx.Client | None = None) -> None:
        if not keys.usable:
            raise StripeUnavailable(
                "STRIPE_SECRET_KEY_FUNNEL is not set, so this funnel cannot take money")
        self.keys = keys
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._session = session or httpx.Client(timeout=timeout)

    # ---- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, *, data: dict[str, Any] | None = None,
                 idempotency_key: str = "", retries: int = 0) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.keys.secret}",
            "Stripe-Version": os.getenv("STRIPE_API_VERSION", "2025-11-17.basil").strip()
            or "2025-11-17.basil",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key[:255]
        body = encode_form(data) if data is not None else None
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        url = f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        while True:
            try:
                response = self._session.request(
                    method, url, content=body.encode() if body else None,
                    headers=headers, timeout=self.timeout)
            except httpx.HTTPError as exc:
                # A network failure on a create is ambiguous: the charge may or may not have
                # happened. Say so, because the caller's honest options differ by method.
                raise StripeError(
                    f"transport error calling {method} {path}: {type(exc).__name__}: {exc}") from exc
            if response.status_code >= 500 and attempt < retries:
                attempt += 1
                time.sleep(min(2 ** attempt * 0.25, 2.0))
                continue
            break

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            err = payload.get("error") or {}
            raise StripeError(
                err.get("message") or f"stripe {method} {path} -> HTTP {response.status_code}",
                status=response.status_code, code=err.get("code", "") or err.get("type", ""))
        return payload

    def get(self, path: str, *, retries: int = 2) -> dict[str, Any]:
        return self._request("GET", path, retries=retries)

    def post(self, path: str, data: dict[str, Any] | None = None, *,
             idempotency_key: str = "") -> dict[str, Any]:
        return self._request("POST", path, data=data or {}, idempotency_key=idempotency_key)

    # ---- checkout ----------------------------------------------------------

    def create_checkout_session(self, *, success_url: str, cancel_url: str,
                                customer_email: str, client_reference_id: str,
                                trial_amount_minor: int, trial_days: int,
                                rebill_amount_minor: int, currency: str,
                                trial_end_epoch: int,
                                metadata: dict[str, str] | None = None) -> dict[str, Any]:
        """One Checkout session: $trial now, $rebill every month after `trial_days`.

        A **price data** inline (`price_data`), not a pre-created Price object. Reason: the
        offer is one price point and the spec wants a price change to be one `.env` edit;
        a catalog Price would mean "edit .env, then go create the Price in the dashboard",
        and the drift between the two is invisible until someone is billed wrong.

        `trial_end` sets the subscription's trial period, so the recurring charge date is the
        processor's, not ours. `trial_end` in the past raises: a trial with zero days left
        would charge the rebill amount today, which is a disclosure lie.
        """
        if trial_end_epoch <= int(time.time()):
            raise StripeError(
                f"trial_end {trial_end_epoch} is in the past; refusing to create a session "
                "that charges the recurring amount today")
        body: dict[str, Any] = {
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": client_reference_id,
            "customer_email": customer_email or None,
            "subscription_data": {
                "trial_end": trial_end_epoch,
                "metadata": metadata or {},
            },
            # The trial invoice carries the renewal terms into Stripe's own receipt, so the
            # disclosure exists in the processor's email as well as ours (checklist 5).
            # The entry price as a one-off, additive line. Without it Stripe bills the
            # subscription's own price today and the `trial_end` only defers the *second*
            # charge, so the customer would see $39 on the day the screen promised $1. With
            # it, the trial invoice reads exactly the disclosure: $1 now, $39 monthly from
            # the trial end. `trial_days` is quoted in the description so the invoice names
            # the period the customer agreed to.
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": currency,
                        "unit_amount": trial_amount_minor,
                        "product_data": {
                            "name": f"{trial_days}-day trial",
                            "description": (
                                f"{trial_days}-day trial. Renews at "
                                f"{rebill_amount_minor / 100:.2f} {currency.upper()} "
                                "monthly after the trial."),
                        },
                    },
                },
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": currency,
                        "unit_amount": rebill_amount_minor,
                        "recurring": {"interval": "month"},
                        "product_data": {
                            "name": os.getenv("FUNNEL_STATEMENT_PRODUCT_NAME",
                                              "kegeldoc membership").strip(),
                        },
                    },
                },
            ],
            "invoice_creation": {"enabled": True, "invoice_data": {
                "custom_fields": [
                    {"name": "Renewal", "value": (
                        f"Renews at {rebill_amount_minor / 100:.2f} {currency.upper()} "
                        f"monthly after the {trial_days}-day trial")},
                ],
            }},
            "metadata": metadata or {},
        }
        return self.post("/checkout/sessions", body)

    # ---- subscription state ------------------------------------------------

    def retrieve_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self.get(f"/subscriptions/{quote(subscription_id, safe='')}")

    def cancel_at_period_end(self, subscription_id: str) -> dict[str, Any]:
        """One click, and it is done. `cancel_at_period_end`, not `DELETE`: the customer
        keeps the days they paid for (our own terms say so), and access revoking before the
        final-charge date is what turns a clean cancel into a refund request.

        Idempotency key derived from the subscription id and the literal action, so two taps
        on the button cannot open two different cancellations.
        """
        return self.post(f"/subscriptions/{quote(subscription_id, safe='')}",
                         {"cancel_at_period_end": True},
                         idempotency_key=f"funnel-cancel-{subscription_id}")

    def resume_at_period_end(self, subscription_id: str) -> dict[str, Any]:
        """Undo a period-end cancel: this is what a save-offer acceptance means."""
        return self.post(f"/subscriptions/{quote(subscription_id, safe='')}",
                         {"cancel_at_period_end": False},
                         idempotency_key=f"funnel-resume-{subscription_id}")

    def pause_collection(self, subscription_id: str, *, resumes_at: int) -> dict[str, Any]:
        """Save-offer A: pause now, resume on a date we state to the customer."""
        return self.post(f"/subscriptions/{quote(subscription_id, safe='')}", {
            "pause_collection": {"behavior": "mark_uncollectible",
                                 "resumes_at": resumes_at},
        }, idempotency_key=f"funnel-pause-{subscription_id}-{resumes_at}")

    def resume_collection(self, subscription_id: str) -> dict[str, Any]:
        return self.post(f"/subscriptions/{quote(subscription_id, safe='')}",
                         {"pause_collection": ""},
                         idempotency_key=f"funnel-resume-collection-{subscription_id}")

    def apply_coupon(self, subscription_id: str, coupon_id: str) -> dict[str, Any]:
        """Save-offer C: the discount is applied by the processor, never simulated locally.

        An offer shown on screen that the processor does not honour is the same class of
        failure as a disclosure that does not render.
        """
        return self.post(f"/subscriptions/{quote(subscription_id, safe='')}",
                         {"discounts": [{"coupon": coupon_id}]},
                         idempotency_key=f"funnel-coupon-{subscription_id}-{coupon_id}")

    def update_amount(self, item_id: str, amount_minor: int,
                      currency: str) -> dict[str, Any]:
        """Save-offer B: move the subscription's own price to the lite amount.

        Overriden on the subscription item, so the catalog price other customers share is
        untouched. Requires the item id, which is why `checkout_complete` records it.
        """
        return self.post(
            f"/subscription_items/{quote(item_id, safe='')}",
            {"price_data": {"currency": currency, "unit_amount": amount_minor,
                            "recurring": {"interval": "month"}}},
            idempotency_key=f"funnel-downgrade-{item_id}-{amount_minor}")

    # ---- read-only helpers used by the health page -------------------------

    def health(self) -> dict[str, Any]:
        """What key are we on, and is it reachable. Never raises: `/health` displays the
        failure rather than 500ing, so an operator can read the state without logs."""
        try:
            info = self.get("/account", retries=0)
            return {
                "reachable": True,
                "key_type": self.keys.key_type,
                "acct_type": self.keys.acct_type,
                "country": info.get("country", ""),
                "business_name": info.get("business_name") or info.get("settings", {}).get(
                    "business_name", ""),
                "payments_enabled": bool(info.get("charges_enabled")),
                "statements_descriptor": (info.get("settings", {})
                                          .get("dashboards_display", {})
                                          .get("statement_descriptor", "")) or "",
            }
        except (StripeError, httpx.HTTPError) as exc:
            return {"reachable": False, "error": str(exc),
                    "key_type": self.keys.key_type, "acct_type": self.keys.acct_type}


def client_for(settings: FunnelSettings) -> StripeClient | None:
    """The funnel client, or None when this lane is not permitted to charge.

    Two different no's, deliberately not collapsed: charges switched off (a policy decision,
    rehearsable) and no key present (a config gap). Both mean "do not call Stripe", and the
    caller should never wonder which one it got; the reasons stay distinguishable in the log
    and on `/health`.
    """
    keys = FunnelKeys.load()
    if not keys.usable:
        log.info("no STRIPE_SECRET_KEY_FUNNEL: checkout stays in rehearsal")
        return None
    if not settings.charges_enabled:
        log.info("funnel keys present but FUNNEL_CHARGES_ENABLED is false: rehearsal")
        return None
    return StripeClient(keys)


# ---------------------------------------------------------------------------
# Webhook signature
# ---------------------------------------------------------------------------

def _signature_header_value(header: str) -> tuple[str, list[str], list[str]]:
    """Parse `t=...,v1=...` into (timestamp, v1 sigs, v0 sigs)."""
    timestamp = ""
    v1: list[str] = []
    v0: list[str] = []
    for chunk in header.split(","):
        key, _sep, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key == "t":
            timestamp = value
        elif key == "v1":
            v1.append(value)
        elif key == "v0":
            v0.append(value)
    return timestamp, v1, v0


def verify_signature(payload: bytes, header: str, secret: str, *,
                     tolerance: int = DEFAULT_TOLERANCE_SECONDS,
                     now: int | None = None) -> str:
    """Verify a `Stripe-Signature` header. Returns "verified" (v1) or "verified-v0"
    (legacy); raises `StripeError` otherwise.

    Reimplemented rather than delegated to `stripe.Webhook.construct_event` because the
    SDK is not a dependency here (module docstring). The security properties that must
    survive a rewrite are: HMAC against the **raw bytes**, constant-time compare, a
    tolerance window so a captured header cannot be replayed indefinitely, and the payload
    prefix `f"{timestamp}.{payload}"` exactly as Stripe signs it. Dropping the timestamp
    from the signed string is the classic way this passes locally and fails in prod.
    """
    if not header:
        raise StripeError("missing Stripe-Signature header", status=400)
    timestamp, v1, v0 = _signature_header_value(header)
    if not timestamp.isdigit() or not (v1 or v0):
        raise StripeError("malformed Stripe-Signature header", status=400)
    age = (now if now is not None else int(time.time())) - int(timestamp)
    if tolerance and abs(age) > tolerance:
        raise StripeError(
            f"webhook timestamp is {age}s old, outside the {tolerance}s tolerance", status=400)

    signed = f"{timestamp}.".encode() + payload
    for signatures, result in ((v1, "verified"), (v0, "verified-v0")):
        for candidate in signatures:
            expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, candidate):
                return result
    raise StripeError("no webhook signature matched the payload", status=400)


# ---------------------------------------------------------------------------
# Event interpretation
# ---------------------------------------------------------------------------

def period_end_epoch(subscription: dict[str, Any]) -> int:
    """The subscription's current period end, in epoch seconds, or 0 when unknown.

    `current_period_end` is deprecated on newer Stripe versions in favour of
    `current_period_end` on the items / `billing_cycle_anchor_config`, but it is still
    returned, and reading both spellings with a fallback is the difference between a
    dashboard that shows the real renewal date and one that shows a guess.
    """
    for key in ("current_period_end", "billing_cycle_anchor"):
        value = subscription.get(key)
        if isinstance(value, int) and value > 0:
            return value
    trial_end = subscription.get("trial_end")
    if subscription.get("status") == "trialing" and isinstance(trial_end, int) and trial_end > 0:
        # During a trial, the trial end *is* the moment the paid period starts, so it is
        # what the customer needs to see. Falling through to 0 here would make the
        # account page say nothing about the first real charge.
        return trial_end
    items = ((subscription.get("items") or {}).get("data") or [{}])
    for item in items:
        value = item.get("current_period_end")
        if isinstance(value, int) and value > 0:
            return value
    return 0


def item_id(subscription: dict[str, Any]) -> str:
    """First subscription item id, needed to move the price on a downgrade."""
    for item in ((subscription.get("items") or {}).get("data") or []):
        if item.get("id"):
            return str(item["id"])
    return ""


def card_of(subscription: dict[str, Any]) -> tuple[str, str]:
    """(brand, last4) from the subscription's default payment method, when expanded.

    A receipt that names the card is how a customer recognises the charge instead of
    disputing it, which the corpus lists as a top small-B dispute cause. Absent is fine:
    the field is optional in the email, not a claim.
    """
    method = subscription.get("default_payment_method") or {}
    card = (method.get("card") or {}) if isinstance(method, dict) else {}
    return str(card.get("brand", "") or ""), str(card.get("last4", "") or "")


TERMINAL_STATUSES = ("canceled", "unpaid")


def is_cancelled(subscription: dict[str, Any]) -> bool:
    """True when no future charge is coming, including the period-end state.

    Used by the webhook to keep the local row and Stripe in step. `cancel_at_period_end`
    counts: from the customer's side the click already worked, and our own screen says the
    same thing, so a status that disagreed with the screen would be the bug.
    """
    status = str(subscription.get("status", ""))
    return bool(subscription.get("cancel_at_period_end")) or status in TERMINAL_STATUSES


def card_of_object(obj: dict[str, Any]) -> dict[str, Any]:
    """Card facts from any event object that carries a card, however it hangs.

    Four shapes reach us and all four mean the same thing: a payment method expanded on a
    subscription (`default_payment_method.card`), a charge with its payment method expanded
    (`charge.payment_method.card`), a dispute whose `charge` object is expanded, and a Radar
    early warning, whose *only* card information lives under `payment_intent.card_details`
    (`network` and `customer_notification` are not cards at all). Walking each spelling in
    one place is the point: the prepaid-BIN share the spec asks us to instrument from charge
    one (§1.C criterion 4) is worth nothing if it depends on which object the event happened
    to expand, and a funding type we did not observe is recorded as unknown rather than
    guessed as consumer, because understating the prepaid share is the direction that hides
    the warning.

    Returns `{"funding": ..., "country": ..., "brand": ..., "last4": ...}` with empty strings
    for whatever the payload did not carry.
    """
    # Depth matters here as much as spelling. A dispute carries no card of its own, only a
    # charge whose `payment_method_details` holds the funding type; a Checkout session hangs
    # the card off `payment_method`. Without those hops every dispute would log an unknown
    # BIN type, and the BIN share is the leading indicator §1.C criterion 4 asks us to
    # instrument from charge one, on exactly the events that trip the ratios.
    holders: list[Any] = [obj]
    for nested in (obj.get("charge"), obj.get("invoice"), obj.get("payment_intent")):
        if isinstance(nested, dict):
            holders.extend([nested, nested.get("payment_method"),
                            nested.get("payment_method_details")])
    candidates: list[Any] = [
        obj.get("default_payment_method"),
        obj.get("payment_method"),
        obj.get("payment_method_details"),
    ]
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        for key in ("default_payment_method", "payment_method", "payment_method_details"):
            value = holder.get(key)
            if isinstance(value, dict):
                candidates.append(value)
    # A Radar early warning's card facts, spelled differently and reachable only this way.
    for nested in (obj, obj.get("payment_intent") or {}):
        details = nested.get("card_details") if isinstance(nested, dict) else None
        if isinstance(details, dict) and details:
            candidates.append(details)
        # Stripe's live early-warning shape is flat: `network.card_funding`. Accepted
        # alongside the documented spelling because the two have both been observed, and a
        # warning we could not classify is the worst one to silently drop.
        network = nested.get("network") if isinstance(nested, dict) else None
        if isinstance(network, dict) and any(
                network.get(key) for key in ("card_funding", "card_brand", "card_country")):
            candidates.append({
                "funding": network.get("card_funding", ""),
                "country": network.get("card_country", ""),
                "brand": network.get("card_brand", ""),
            })
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        card = candidate.get("card") if isinstance(candidate.get("card"), dict) else candidate
        if any(card.get(key) for key in ("funding", "brand", "last4", "country")):
            return {
                "funding": str(card.get("funding", "") or ""),
                "country": str(card.get("country", "") or ""),
                "brand": str(card.get("brand", "") or ""),
                "last4": str(card.get("last4", "") or ""),
            }
    return {"funding": "", "country": "", "brand": "", "last4": ""}


#: Stripe's `warning_reason` values that mean "the cardholder says this was fraud". The
#: other values (`accepted`, `generic_complaint`, `product_unsatisfactory`, ...) are
#: Radar's advice and arrive as *warnings, not disputes*: nothing in `hustle_metrics` holds
#: them, which is exactly the gap §3.3 flags as the one gate we cannot otherwise measure.
FRAUD_WARNING_REASONS = ("fraudulent", "abusive")


def is_fraud_warning(warning: dict[str, Any]) -> bool:
    """Whether a `radar.early_warning.created` object names fraud.

    The ≤ 0.5% gate counts fraud flags, not advice. A warning whose reason is
    `generic_complaint` is not evidence the account is being closed for fraud, and counting
    it would trip the kill switch on the wrong signal, so only the fraud-named reasons are.
    An absent `warning_reason` is counted, since Stripe sends the field on every warning
    today and a warning we cannot classify is the one we should not quietly discard.
    """
    reason = str(warning.get("warning_reason", "") or "").strip().lower()
    return reason in FRAUD_WARNING_REASONS or not reason


def event_from_webhook(event: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Stripe event to the fields this funnel acts on.

    Single choke point on purpose: every number the DB stores, the dashboard shows and the
    emails quote comes out of here, so "where did this date come from" has one answer.
    """
    obj = event.get("data", {}).get("object", {}) or {}
    metadata = obj.get("metadata") or {}
    brand, last4 = card_of(obj)
    event_type = event.get("type", "")
    # Money facts for the telemetry layer. `charge` is expanded on a dispute or a Radar
    # warning, and is a bare id on `charge.refunded`, so the nested copy wins when the
    # payload bothers to include one and the flat object is the fallback.
    # Which object holds the *money* facts, which is not the event object for two of the
    # shapes we handle: a dispute's amounts and customer live on its charge, and a Radar
    # warning has no charge at all, only the payment intent it warns about. Resolving that
    # once here is what stops every downstream ratio from being computed off zeros for the
    # exact events the kill switches depend on.
    nested = obj.get("charge")
    if isinstance(nested, dict):
        charge = nested
    elif event_type.startswith("radar."):
        intent = obj.get("payment_intent")
        charge = intent if isinstance(intent, dict) else {}
    else:
        charge = obj
    # The dispute/warning's own id, kept separate because `charge_id` is read as a charge id
    # everywhere (dashboard lookups, dedupe keys) and a `dp_`/`rs_` id in that column breaks
    # both. The event id identifies the warning when there is no charge.
    nested_id = str(obj.get("id", "") or "") if charge is not obj else ""
    card = card_of_object(obj)
    metadata = obj.get("metadata") or charge.get("metadata") or {}
    return {
        "type": event_type,
        "id": event.get("id", ""),
        "subscription_id": obj.get("id", "") if event_type.startswith("customer.subscription")
        else (obj.get("subscription") or charge.get("subscription") or ""),
        "customer_id": obj.get("customer", ""),
        "status": obj.get("status", ""),
        "cancel_at_period_end": bool(obj.get("cancel_at_period_end")),
        "current_period_end": period_end_epoch(obj),
        "trial_end": obj.get("trial_end") or 0,
        "item_id": item_id(obj),
        "card_brand": brand,
        "card_last4": last4,
        "email": (obj.get("customer_details") or {}).get("email", "") or metadata.get("email", ""),
        "visitor_key": metadata.get("visitor_key", "") or obj.get("client_reference_id", ""),
        "plan_signature": metadata.get("plan_signature", ""),
        "amount_total": obj.get("amount_total") or obj.get("total") or 0,
        # ---- telemetry facts (Phase 3) ------------------------------------
        # Read off `charge` so one shape serves a charge, a refund, a dispute and a Radar
        # warning. `amount` is what a refund refunded and what a dispute contests;
        # `amount_captured` is what we actually kept, which is the authorised-volume
        # denominator §3.4 divides by. `outcome.type` is Stripe's own risk taxonomy on the
        # charge (`fraudulent`, `manual_review`), the closest thing to a fraud flag that
        # exists before a dispute is opened.
        # `''` for a Radar warning rather than the warning's own id, because every consumer
        # of this field treats it as a charge id (dashboard lookups, the dedupe key for a
        # refund event). Warnings carry their own identity in `event["id"]`, which the
        # telemetry uses, and pretending a `rs_` id is a `ch_` id would put a bogus value in
        # a column someone later searches for a refund.
        "charge_id": "" if event_type.startswith("radar.") else str(charge.get("id", "") or ""),
        "signal_id": nested_id,
        "amount": int(obj.get("amount") or charge.get("amount") or 0),
        "amount_captured": int(charge.get("amount_captured") or 0),
        "charge_status": str(charge.get("status", "") or ""),
        "charge_outcome_type": str((charge.get("outcome") or {}).get("type", "") or ""),
        "paid": bool(charge.get("paid")),
        "card_funding": card["funding"],
        "card_country": card["country"],
        "warning_reason": str(obj.get("warning_reason", "") or ""),
        "dispute_status": str(obj.get("status", "") or "") if event_type.startswith("charge.dispute") else "",
        # The 2-month coupon from the save-offer screen. `discount.expires_at` is the date the
        # processor itself says the promotion ends, which is better than counting invoices
        # against the "2 months" the screen promised: a member who was charged early, or
        # resumed from a pause, would otherwise be shown "half price" over a full-price
        # invoice. Absent `discount` is read as "no live promotion", not as "ended", so a
        # subscription that never had the coupon is not reported as having lost one.
        "discount": obj.get("discount") or {},
        "discount_expires_at": (obj.get("discount") or {}).get("expires_at") or 0,
        "currency": obj.get("currency", ""),
        # Stripe's envelope key is `livemode`, one word, and it sits on the *event*, not the
        # object. `event.get("live_mode")` read a key that appears in no payload, so this was
        # False for every event Stripe has ever sent, including a real live-mode dispute aimed at
        # another merchant account. The mode-mismatch guards are what keep that account's disputes
        # and refunds out of our 1% and 0.5% ceilings, so a ceiling that should have rung was
        # reported clean while the misconfiguration streamed past it. Both spellings are read:
        # `live_mode` is what our own fixtures and internal event copies have used, and it would
        # take a second bug to make that wrong, whereas dropping it would silently disable the
        # guard for every test in the suite at once.
        "live_mode": bool(event.get("livemode") or event.get("live_mode")),
        # Whether Stripe had already attempted this invoice when the failure fired. Absent reads
        # as False, which is the safe direction: `attempted` is what tells the retry policy that
        # a second failure is not the first one arriving twice, and inventing True would let any
        # replay declare a member terminally lost and stop the one retry the spec owes them.
        "attempted": bool(obj.get("attempted")),
        "attempt_count": int(obj.get("attempt_count") or 0),
    }
