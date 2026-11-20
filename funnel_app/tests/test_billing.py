"""Phase 2: checkout, the member dashboard, the one-click cancel and the save-offer sequence.

The rules under test here are mostly *compliance* rules with an order to them, so most of this
suite asserts on sequence rather than on status codes. A 200 on a cancel page says nothing about
whether the subscription was actually cancelled before the retention copy appeared, and "the
cancel flow felt fine" is not evidence: `LANE_C_PHASE0_STEP1-2.md` §2.3 lists six rules that are
each enforceable as an assertion, so they are written as assertions rather than left as intent.

Two fixtures, one per mode:

  * `client` (from `conftest.py`) runs with charges off and no key, which is how the funnel
    boots today. The rehearsal path still exercises the dashboard and the readiness checks,
    because those are reachable without a card.
  * `live` arms charges and installs a scripted `FakeStripe`, so the live branch (Checkout
    session creation, period-end cancellation, save-offer price changes, webhooks) runs against
    a stand-in that records what it was asked to do.

The fake exists instead of a mocked SDK because what needs checking is the *request*: whether
the entry price was priced today, whether cancel meant `cancel_at_period_end` rather than
`DELETE`, whether the coupon id was sent rather than a locally-computed half price. Mocking a
client library checks that we called the function we named, which is the part least likely to
be wrong.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

# `conftest.py` already put the app root on `sys.path` when pytest collected this directory,
# but importing it below must not depend on that: a plain `pytest tests/test_billing.py` from a
# different working directory, or an editor running one test, reaches this module first.
FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

from conftest import (  # noqa: E402
    FUNNEL_ROOT, PHASE2_MODULES as TEARDOWN_MODULES,
    assert_app_and_suite_agree_on_stripe_classes, attach, crash_problem, jar_of, post,
    private_stripe_billing, walk)

#: The `stripe_billing` classes this file raises, as a *live* handle rather than a snapshot.
#:
#: The requirement is exact: every exception this suite raises must be an instance of a class the
#: app's `except` clauses name. That is only true if both sides hold the *same module object*, and
#: a plain module-level `import stripe_billing` cannot promise it, because of how these suites
#: build apps. Each fixture pops the app's modules from `sys.modules` and re-imports them, and
#: importing one file twice makes a second module object with a second set of classes, so the copy
#: this name binds drifts out of date the moment any fixture (this suite's or a neighbour's)
#: rebuilds the app. Worse, a test body that says `import stripe_billing` re-binds the same global
#: to whichever copy is loaded at that instant, silently, with no error anywhere.
#:
#: Getting this wrong is invisible in the worst way: the fake raises a `StripeError` no handler in
#: the code under test recognises, every processor failure escapes every rescue, and the suite can
#: stay green on the strength of an unrelated crash. That is precisely how the outage tests here
#: passed for a while, on a `TypeError` escaping from inside the app's own rescue path.
#:
#: So this is a module proxy, not a snapshot: every attribute read resolves at the moment of use,
#: which makes staleness structurally impossible rather than something to remember. Two fixtures
#: decide what "current" means, and they differ on purpose:
#:
#:   * `conftest.client` keeps the module registered under the name and rebinds the freshly
#:     imported app tree to it, so a rehearsal app is never built against a second copy;
#:   * `live` builds its app against a *private* copy, because it patches `StripeClient`, and a
#:     patch on the shared module outlives its undo the moment another app captures the patched
#:     factory. See `conftest.private_stripe_billing`.
#:
#: Because those two apps hold different copies *at the same time*, "the module right now" cannot
#: be a global the way it once was. The active copy is set at fixture time and read here, and
#: `conftest.attach` verifies on every request that it really is the copy the app answering that
#: request bound.
class _LiveStripeBilling(types.ModuleType):
    """Attribute access on this delegates to the app's current `stripe_billing`.

    Subclasses `ModuleType` rather than quacking like a module so `isinstance` checks, `repr`,
    and anything else that inspects a module keep working, and so `__dict__` writes on the proxy
    (there are none, on purpose) could not shadow the delegate.
    """

    def __getattr__(self, name):
        # Only called when the name is missing from this proxy's own `__dict__`, which is how the
        # proxy keeps its own methods while every app name comes from the delegate.
        return getattr(current_stripe_billing(), name)

    def __dir__(self):
        return dir(current_stripe_billing())


#: Set by the `live` fixture, cleared on teardown. Deliberately not discovered from `sys.modules`
#: or from `main`: a test that borrows both fixtures has two apps alive at once, and the only
#: version of "current" that is not a guess is the one the fixture that made the app declares.
_ACTIVE: list = []


def current_stripe_billing():
    """The `stripe_billing` the test in flight should be using.

    The fixture's private copy if one is running, otherwise the module registered under the name,
    which is what `stripe_billing.py` resolves when it constructs its own errors. Falls back to
    importing it for the one window where nothing has: collection time, and the unit tests that
    touch module internals without an app at all. Importing *registers* the name, so the fallback
    leaves the process in the state the fixtures expect rather than a half-loaded one.
    """
    if _ACTIVE:
        return _ACTIVE[-1]
    registered = sys.modules.get("stripe_billing")
    if registered is not None:
        return registered
    import stripe_billing as fresh
    return fresh


stripe_billing = _LiveStripeBilling("stripe_billing")


# ---------------------------------------------------------------------------
# A recording Stripe
# ---------------------------------------------------------------------------
class PinAttribute:
    """A class attribute that accepts one value, and the disarmed value, and refuses every other.

    Used to pin `BillingService.client` to the recorder one test armed. Plain `monkeypatch.setattr`
    would freeze the value but not the assignment, so the app's own `billing.client =
    billing_client()` would sail through and the test's double would silently stop being the object
    under test. Raising at the assignment names the line that broke the arrangement, instead of
    leaving a later assertion to report an empty call list with no cause.

    `disarm()` is the escape hatch, not `None` accepted unconditionally. `None` means "this funnel
    cannot reach the processor" to every gate in `billing` and `main`, so a test of that posture, a
    live-mode event arriving with no usable client, or the rehearsal banner, has to be able to stand
    in it. But the app re-arms on several routes, and an unconditional pass-through is not strong
    enough to tell "this test asked for the disarmed state" from "the app's own re-arm quietly put
    the recorder back and this test is now asserting against a client it never configured", which is
    the exact confusion the pin exists to catch. A test that wants the disarmed state calls
    `disarm()`; everybody else still gets the raise.

    Disarming inverts what is pinned rather than adding a tolerated value on the side: once
    disarmed, the recorder itself is refused, and any attempt to put it back raises. That direction
    matters more than it looks. A descriptor that tolerates `None` *and* returns the recorder keeps
    a disarmed app serving requests through the double, so the "we hold no client" scenario is
    tested against a client that is present, and the test passes for the wrong reason while proving
    the opposite of what it says.
    """

    def __init__(self, value) -> None:
        self.value = value
        self.disarmed = False

    def disarm(self) -> None:
        """Pin the disarmed posture: `None` in, and the recorder refused from here on."""
        self.disarmed = True

    def rearm(self) -> None:
        """Back to the recorder-only arrangement this fixture installed."""
        self.disarmed = False

    def __get__(self, _obj, _owner=None):
        # The stored value, always, and *not* a read back through the instance: `obj.__dict__` is
        # where this descriptor's `__set__` puts nothing, so a "read the instance first" version
        # here looked more correct, returned the recorder to a test that had just disarmed it, and
        # turned the raise into an assertion 20 lines later.
        return None if self.disarmed else self.value

    def __set__(self, _obj, value):
        wanted = None if self.disarmed else self.value
        if value is not wanted:
            raise AssertionError(
                "something replaced the client this test pinned, so the object answering requests "
                f"is not the one under test: got {type(value).__name__}, pinned "
                f"{'None (disarmed)' if self.disarmed else type(self.value).__name__}. "
                "`main` re-arms through `billing_client()`; a test that wants the disarmed posture "
                "says so with `client.pin.disarm()` rather than assigning over the double, and a "
                "route that builds a client some other way needs its own test.")




class FakeStripe(stripe_billing.StripeClient):
    """Enough of api.stripe.com to run the funnel's live paths, with every call recorded.

    Its base is reassigned to the live app's `StripeClient` by `build_fake_stripe` before any test
    builds one, so the inheritance written here is the one that reads well at the top of the file
    rather than the one in force during a test.

    Subclasses the real client and replaces only `_request`, the one transport seam, so the
    methods under test (`create_checkout_session`, `cancel_at_period_end`, `pause_collection`,
    `update_amount`, `apply_coupon`) are the production ones. They are where the request bodies
    get built, and a fake that reimplemented them would test the fake instead of the code that
    decides whether a customer is charged $1 or $39.

    Responses are shaped like real objects (nested `items.data`, epoch timestamps, an expanded
    `default_payment_method`), because half of `stripe_billing` is reading those shapes, and a
    fake that returned a flattened convenience object would test the parser against something
    Stripe never sends.
    """

    def __init__(self, keys, **_ignored) -> None:
        # `_NoSession` rather than the real `httpx.Client`: `_request` is overridden below and
        # never calls it, so this only exists to make an accidental real call impossible. A
        # test that opened a socket to api.stripe.com would be a surprise 401 at best and a
        # real charge at worst.
        #
        # `_warmup` is set before the base constructor runs, because that constructor makes a
        # `GET /account` to confirm the merchant behind the key, and the call comes back into this
        # class's own `_request` while `__init__` is still in progress. Serving that one read from
        # the object itself, rather than skipping the call or overriding the constructor, keeps the
        # real `__init__` the thing under test: it still validates the keys, still sets the same
        # attributes, and still records the call in `calls` once there is a `calls` to record it
        # in, which is what the merchant-isolation tests read.
        self._warmup = True
        self.account: dict = {"country": "US", "business_name": "Routine Lab",
                              "charges_enabled": True,
                              "settings": {"dashboards_display":
                                           {"statement_descriptor": "ROUTINE LAB"}}}
        super().__init__(keys, session=_NoSession(self))
        self._warmup = False
        self.reset()

    def reset(self) -> None:
        """Forget every call, subscription and failure mark, and keep the object.

        Exists because one recorder serves one test. The app rebuilds its client through
        `main.billing_client()` on several routes, so a fixture cannot guarantee which object the
        app holds at any moment, and a substitution made here can be overwritten by the next
        request. Reconfiguring the one instance the factory always returns is the only arrangement
        where the app and the assertions cannot drift apart.

        The state has to go, not just be replaced: a test that leaves `/subscriptions/` marked as
        failing hands the next test a *setup* charge that gets refused, which surfaces as a 500
        from `/checkout/start` in a test that never mentioned an outage. The id counter resets so
        session ids stay `cs_fake1`, `sub_fake2`, predictable enough to assert on.
        """
        self.calls: list[tuple[str, str, dict]] = []
        self.subscriptions: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        #: Set for the duration of one request when a path is meant to fail at the socket.
        self.socket_error = ""
        self.session_request: dict = {}
        self.failures: list[tuple[str, str, int]] = []
        #: Paths whose POST should not touch the recorded-subscription state, which is how a
        #: webhook-only test can point an event at a subscription id this fake does not know.
        self.ignore_prefixes: tuple[str, ...] = ()
        self._counter = 0

    # ---- state helpers -----------------------------------------------------

    def _id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_fake{self._counter}"

    def seed_subscription(self, subscription_id: str, *, status: str = "trialing",
                          item_id: str = "", period_end_days: int = 7,
                          coupon: str = "") -> dict:
        """Put a subscription in the state a webhook or a cancel click will be judged against."""
        period_end = int(time.time()) + period_end_days * 86400
        subscription = {
            "id": subscription_id,
            "object": "subscription",
            "status": status,
            "customer": "cus_fake1",
            "cancel_at_period_end": False,
            "current_period_end": period_end,
            "trial_end": period_end,
            "collection_method": "charge_automatically",
            "default_payment_method": {
                "id": "pm_fake", "card": {"brand": "visa", "last4": "4242"}},
            "items": {"data": [{
                "id": item_id or "si_fake1",
                "price": {"id": "price_fake", "unit_amount": 3900,
                          "recurring": {"interval": "month"}}}]},
        }
        if coupon:
            subscription["discount"] = {"coupon": {"id": coupon},
                                        "expires_at": int(time.time()) + 62 * 86400}
        self.subscriptions[subscription_id] = subscription
        return subscription

    def calls_to(self, fragment: str) -> list[tuple[str, str, dict]]:
        return [call for call in self.calls if fragment in call[1]]

    def fail_mutations_of(self, prefix: str, status: int, message: str) -> None:
        """Make every POST to paths under `prefix` fail with that HTTP status.

        Status is part of the signature rather than an optional extra because it *is* the
        behaviour: a 4xx is a rejection the app reports and carries on, and a dead socket
        (status 0) is an ambiguity that reaches the route as an unhandled error. A test that
        could pick either by accident would pass for the wrong reason.
        """
        self.failures.append((prefix, status, message))

    # ---- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, *, data: dict | None = None,
                 idempotency_key: str = "", retries: int = 0) -> dict:
        """The seam *below* the request builders, which is what makes this fake useful.

        `get`, `post` and every high-level method still run for real, so the bodies asserted on
        are built by production code. Nothing here reimplements the real method: a socket that
        died is delegated to `super()._request` (see below), and a path this fake simply has no
        answer for raises in `_NoSession` rather than being quietly waved through, which is what
        keeps `GET /account`-style calls from reading as successes.
        """
        data = data or {}
        if self._warmup and path == "/account":
            # Inside `__init__`, before `reset()` has run, so nothing is recorded and nothing
            # else would answer. See the note in `__init__`.
            return dict(self.account)
        #: A socket that died on this path goes through the *real* `_request`, whose own
        #: `except httpx.HTTPError` arm is what turns a transport failure into the statusless
        #: `StripeError` the app sees. `_NoSession` raises it on the way in, so the one
        #: production line this fake would otherwise have to paraphrase stays the thing under
        #: test. Recording happens on the way in, so a failed write is still a call.
        dies = next((message for prefix, status, message in self.failures
                     if status == 0 and path.startswith(prefix)), None)
        self.calls.append((method, path, data))
        if dies is not None:
            self.socket_error = dies
            try:
                return super()._request(method, path, data=data,
                                        idempotency_key=idempotency_key, retries=retries)
            finally:
                self.socket_error = ""
        if method == "POST":
            # A create is deliberately unsigned by an idempotency key (see `StripeClient`: the
            # caller has nowhere to put a key on a request whose identity is unknown until it
            # returns), so only keys that *are* present get their namespace checked. Those are
            # the writes a double-tapped button can repeat, and a foreign or missing key there
            # is what turns one cancel click into two charges.
            if idempotency_key and not idempotency_key.startswith("funnel-"):
                raise AssertionError(f"foreign idempotency namespace: {idempotency_key!r}")
            for prefix, status, message in self.failures:
                if path.startswith(prefix):
                    # Faithful about *how* each failure arrives, because the app treats the two
                    # differently and a fake that blurred them would hide that. An HTTP 4xx/5xx
                    # becomes a `StripeError` carrying a status; status 0, a socket that died,
                    # never reaches this loop because it is routed through the real `_request`
                    # above.
                    raise stripe_billing.StripeError(message, status=status, code="")
            if path.startswith(self.ignore_prefixes):
                # Acknowledged and left alone: the response still parses, so the app takes its
                # local write path, which is exactly what a webhook test needs to observe.
                return {"id": path.split("/")[2]}
            return self._mutate(path, data)
        return self._read(path)

    @staticmethod
    def _not_found(object_id: str, kind: str) -> stripe_billing.StripeError:
        """Stripe's own phrasing for a missing object, at 404.

        Reproduced rather than invented because the app prints the processor's message on the
        page that says whether money moved, and a test that asserts that page exists should be
        reading a message a real 404 could have produced.
        """
        return stripe_billing.StripeError(f"No such {kind}: {object_id}", status=404)

    def _read(self, path: str) -> dict:
        if path.startswith("/checkout/sessions/"):
            session_id = path.rsplit("/", 1)[-1]
            if session_id in self.sessions:
                return self.sessions[session_id]
            if path.startswith(self.ignore_prefixes):
                # Not a lookup, this test: it came to watch the state a webhook writes, and
                # `ignore_prefixes` is the only knob it has. The read side honours it too, or
                # every such test trips the loud no-answer rule instead of reaching the code it
                # aimed at, which is the failure mode a double exists to spare it.
                return {"id": session_id, "mode": "subscription", "subscription": ""}
            raise self._not_found(session_id, "checkout session")
        if path.startswith("/subscriptions/"):
            subscription_id = path.split("/")[2]
            if subscription_id in self.subscriptions:
                return self.subscriptions[subscription_id]
            if path.startswith(self.ignore_prefixes):
                return self.seed_subscription(subscription_id)
            raise self._not_found(subscription_id, "subscription")
        if path == "/account":
            return self.account
        return {}

    def _mutate(self, path: str, data: dict) -> dict:
        if path == "/checkout/sessions":
            return self._create_session(data)
        if path.startswith("/subscription_items/"):
            item_id = path.split("/")[2]
            for subscription in self.subscriptions.values():
                for item in subscription["items"]["data"]:
                    if item["id"] == item_id:
                        item["price"]["unit_amount"] = data["price_data"]["unit_amount"]
            return {"id": item_id}
        if path.startswith("/subscriptions/"):
            subscription_id = path.split("/")[2]
            subscription = self.subscriptions.setdefault(
                subscription_id, self.seed_subscription(subscription_id))
            if "cancel_at_period_end" in data:
                subscription["cancel_at_period_end"] = bool(data["cancel_at_period_end"])
            if data.get("pause_collection"):
                subscription["status"] = "paused"
                subscription["pause_collection"] = data["pause_collection"]
            elif "pause_collection" in data:
                # Cleared, so billing resumes where it stopped rather than restarting a cycle:
                # `current_period_end` survives, which is what Stripe does and what the funnel
                # then reads back. Reset here and a resumed member gets a renewal date that never
                # appeared anywhere, and every date assertion downstream drifts by a period.
                subscription["status"] = ("trialing" if subscription.get("trial_end")
                                          and int(time.time()) < int(subscription["trial_end"])
                                          else "active")
                subscription.pop("pause_collection", None)
            if data.get("discounts"):
                subscription["discount"] = {
                    "coupon": {"id": data["discounts"][0]["coupon"]},
                    "expires_at": int(time.time()) + 62 * 86400}
            return subscription
        return {}

    def _create_session(self, data: dict) -> dict:
        self.session_request = data
        session_id = self._id("cs")
        subscription_id = self._id("sub")
        # The subscription's clock starts at the trial end the caller asked for, so a dashboard
        # reading of "next billing" after completion reflects that request.
        self.seed_subscription(subscription_id, item_id=self._id("si"), period_end_days=0)
        period_end = int(data["subscription_data"]["trial_end"])
        self.subscriptions[subscription_id]["current_period_end"] = period_end
        self.subscriptions[subscription_id]["trial_end"] = period_end
        self.sessions[session_id] = {
            "id": session_id,
            "object": "checkout.session",
            "mode": data.get("mode", "subscription"),
            "url": f"https://checkout.stripe.test/{session_id}",
            "client_reference_id": data.get("client_reference_id", ""),
            "customer": "cus_fake1",
            "subscription": subscription_id,
            "customer_details": {"email": data.get("customer_email") or "payer@example.com"},
        }
        return self.sessions[session_id]

class _SocketDied(httpx.TransportError):
    """The transport failing rather than answering, in the real hierarchy.

    A `TransportError`, which is what `httpx.ConnectError` is, because the real `_request`
    catches `httpx.HTTPError` and the whole point of that catch is that a failure with no HTTP
    answer arrives here. Raising something merely `OSError`-shaped would slip past it and report
    a socket problem as an unhandled crash, which is the opposite of what the code does.
    """


class _NoSession:
    """The stand-in socket: silent by default, and it can be told to die once.

    "Silent" is the important half. An `AssertionError` here read as a live-network leak, so
    `fail_mutations_of(prefix, 0, msg)` had to bypass this object entirely to simulate an outage,
    and the outage test ended up asserting a wiring detail instead of asserting that
    `StripeClient._request` classifies a transport failure the way the app needs it classified.
    A no-answer request now surfaces as a real, loud 500 naming `NoAnswerFromTestDouble`, while a
    deliberately-dead one goes through the production catch arm.
    """

    def __init__(self, owner: "FakeStripe") -> None:
        self.owner = owner

    def request(self, method, url, **kwargs):
        if self.owner.socket_error:
            raise _SocketDied(self.owner.socket_error)
        raise AssertionError(
            f"unanswered request in the test double: {method} {url}. Give the path a "
            "fixture response, or fail it on purpose with fail_mutations_of(..., 0, ...).")

    def close(self) -> None:
        pass


#: The real `StripeClient`, captured before anything could replace it.
#:
#: `live` swaps this class out for a factory function, which is how the app ends up talking to the
#: recorder, so after that patch the module attribute is a *function* and cannot be a base class:
#: assigning it to `__bases__` raises `TypeError: FakeStripe.__bases__ must be tuple of classes,
#: not 'function'`. Which is the patch working as intended, reported as a broken class. The double
#: must therefore derive from the pristine class, and `isinstance(client, StripeClient)` inside
#: `client_for` is satisfied the other way round, by handing the app a recorder it built itself.
_PRISTINE_STRIPE_CLIENT = current_stripe_billing().StripeClient


def build_fake_stripe() -> type:
    """Bind `FakeStripe` to the pristine `StripeClient`, cheaply, every time.

    A class body resolves its bases at the instant the enclosing module is imported, so a plain
    `class FakeStripe(stripe_billing.StripeClient)` inherits whichever copy of the module existed
    at collection time and holds it for the whole run. Same hazard as the module handle above, one
    level deeper, and the two fail differently enough to be worth naming: a mismatched exception
    class is invisible, while inheriting from a copy the app does not use means the double is not
    a `StripeClient` as the app counts one, so the complaint arrives in `client_for`, far from
    anything wrong here. Hence this re-points `__bases__` on every call rather than trusting the
    inheritance written above, which it does keep because it reads well at the top of the file.

    Reassigning `__bases__` rather than rebuilding the class keeps every method and every docstring
    where it is, and keeps `isinstance` results stable across tests.

    Two rules the base has to satisfy, which is why it reads off `_PRISTINE_STRIPE_CLIENT` rather
    than off a module attribute: it must be a *class*, since the armed fixture replaces the module's
    `StripeClient` with a factory function, and it must come from whichever copy supplies the
    exception classes the app catches, because the double raises those from inside the production
    methods it inherits.
    """
    base = _PRISTINE_STRIPE_CLIENT
    if FakeStripe.__mro__[1] is not base:
        FakeStripe.__bases__ = (base,)
    return FakeStripe


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def live(tmp_path, monkeypatch):
    """Charges armed, funnel test key present, Stripe replaced by the recorder.

    Patches `StripeClient` on the module `stripe_billing`, not `client_for`, so the real gating
    logic in `client_for` (charges off -> None, key absent -> None, both present -> client)
    still runs. Bypassing it would let a test pass while the switch that decides whether money
    moves stayed untested.

    One recorder serves the whole test, and `subscribe()` clears it rather than swapping it,
    because `main` rebuilds its client on several routes (`billing_client()` reads the key fresh so
    `/health` can reflect a key added after boot). See `FakeStripe.reset`. That rebuild is worth
    keeping on camera rather than stubbing: it is the reason a fake the fixture handed over is not
    necessarily the one answering the next request, which is a property of the app, not of the test.

    It builds its `stripe_billing` as a *private* copy (`private_stripe_billing`) rather than by
    patching the registered module, which is the only arrangement that can be undone. Reverting a
    module attribute is not the same as never having patched it: another app can be built against
    the patched value while the patch is up, and it keeps the recorder after `monkeypatch` puts the
    class back. See the helper for the full argument.

    `yield` rather than `return`, with the bookkeeping popped in a `finally`: this fixture installs
    a module the rest of the process cannot see, and a private copy left installed after its test
    ended is worse than no private copy at all. Anything that raises between here and the yield,
    including the asserts below, would leak it.
    """

    env = {
        "FUNNEL_DB_PATH": str(tmp_path / "funnel.sqlite3"),
        "FUNNEL_BILLING_DB_PATH": str(tmp_path / "billing.sqlite3"),
        "FUNNEL_SESSION_SECRET": "test-secret-value",
        "FUNNEL_PREVIEW_MODE": "local",
        "FUNNEL_CHARGES_ENABLED": "true",
        "FUNNEL_EMAIL_OUTBOX": str(tmp_path / "outbox"),
        "FUNNEL_SUPPORT_EMAIL": "help@routine-lab.test",
        "FUNNEL_DISCOUNT_COUPON_ID": "half_for_two",
        "STRIPE_SECRET_KEY_FUNNEL": "sk_test_funnel_abc123",
        "STRIPE_PUBLISHABLE_KEY_FUNNEL": "pk_test_funnel_abc123",
        "STRIPE_WEBHOOK_SECRET_FUNNEL": "whsec_test_abc123",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # `stripe_billing` is deliberately absent from this list, and that omission is the point:
    # `private_stripe_billing` below is what replaces it, and it needs to read the incumbent in
    # order to promise putting the same module back. Popping the name here would make the helper
    # register a fresh copy and then "restore" that same fresh copy, leaving the pristine module
    # nobody owns and every other app in the process bound to a recorder from a finished test.
    for name in TEARDOWN_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)

    # Order matters: the copy must be private *before* `main` is imported, so that everything the
    # app tree binds comes from the copy that is about to be patched, and the module left in
    # `sys.modules` when this test ends is a pristine one whoever imports it next.
    #
    # Pushed on the stack here and popped by the teardown at the end of this fixture, rather than
    # by an assert in the middle: if `import main` raises, an assert never runs and the copy stays
    # pushed for every test after this one, which is the silent-staleness bug this whole
    # arrangement exists to kill. Better to report a wrong module loudly than to hide one.
    fresh, restore = private_stripe_billing()
    _ACTIVE.append(fresh)
    try:
        # `client_for` ends by calling `StripeClient(keys)`, so replacing the name routes the real
        # gating logic (charges off -> None, key absent -> None, both present -> a client) into the
        # recorder rather than around it.
        #
        # Everything the test will need is closed over *here*, while `fresh` is still the registered
        # module, and nothing below reaches for a module global afterwards. The `finally` puts the
        # pristine module back before the test body runs, so by then this file's own `stripe_billing`
        # name resolves elsewhere; a lambda that read it lazily would build recorders out of the
        # wrong copy's classes, or find a real class where it wanted the factory.
        made: list = []

        def _factory(keys, **_kwargs):
            # One recorder per test, always the same object, so the app and the assertions cannot
            # be holding different doubles. `subscribe()` clears this one rather than replacing it,
            # for the same reason: several routes rebuild the client, and any substitution the
            # fixture made would be quietly overwritten. See `FakeStripe.reset`.
            if not made:
                made.append(build_fake_stripe()(keys))
            return made[-1]

        fresh.StripeClient = _factory
        import main  # noqa: F811
        recorder = _factory(fresh.FunnelKeys.load())
    finally:
        # Back to the pristine registered module whether or not the import worked. `restore` is
        # unconditional and the copy stays reachable through `_ACTIVE` for the body below, so a
        # failed import leaves the process exactly as it found it.
        restore()
    assert main.stripe_billing is fresh, "the app bound a different copy than the patched one"
    assert sys.modules["stripe_billing"] is fresh, \
        "`main` bound a copy that is not the registered one, which the assert above already " \
        "rules out; if this fires, something re-registered the module during the import"

    client = attach(TestClient(main.app), main, fresh)

    # `main` re-arms itself on several routes: `billing.client = billing_client()`, so that a key
    # added after boot takes effect without a restart. Harmless in rehearsal, and a silent trap
    # here. That line ran on the next request, the patched factory answered with a double of its
    # own, and the recorder this test had just configured stopped being the one answering requests.
    # Nothing raised: the test read an empty `calls`, or set an outage switch on an object nobody
    # consulted, and every page still came back 200.
    #
    # So two things. `billing_client` is replaced with a function that arms *this* recorder, which
    # makes the app's own re-arm a no-op instead of a swap. And the attribute itself is pinned, so
    # that anything else which assigns over it raises here rather than quietly invalidating an
    # assertion three calls later.
    #
    # The gating that used to be exercised by letting the real `client_for` run (charges off ->
    # None, no key -> None) is not lost: `test_charges_on_but_no_key_still_boots_and_keeps_the_two_nos_apart`
    # and the config tests above cover both branches directly, which is where they are cheap to
    # test, rather than by leaving the app free to replace its own double mid-test.
    def _arm(disarm: bool | None = None, *_ignored, **_kwargs):
        """What the app's re-arming routes call. Reads the *posture*, and normally hands back the
        recorder.

        `disarm=None` means "nobody told me", which is every real route, and the answer then comes
        from the same place production's answer would come from: `client_for`'s own gating on the
        live environment. A test that removes the funnel key is therefore answered `None` by the
        next request, which is what a key withdrawn without a restart does, and a test that puts one
        back is armed again. `disarm=True`/`False` overrides it for a test that wants the posture
        asserted rather than derived.

        Reading the environment rather than returning a constant is the part the first version got
        wrong. It answered `recorder` no matter what, so every "we hold no key" path in the app was
        measured with a client present, and a live-mode event arriving at a rehearsal app looked
        handled because a fixture refused to let the app be in the state the test claimed.
        """
        if disarm is None:
            # The gating the real factory applies, against the real environment, so this stays a
            # mirror of production rather than a stub that happens to be convenient. Both of the
            # app's two no's have to be here, and reading only the key misses the one this suite
            # leans on hardest: `client_for` also returns None for a configured lane with
            # `FUNNEL_CHARGES_ENABLED=false`, and a fixture that ignored that armed a recorder for
            # an app production says must not reach Stripe at all. Calling `client_for` rather than
            # reimplementing its conditions is the version that cannot drift from it.
            disarm = fresh.client_for(main.settings) is None
        (pin.disarm() if disarm else pin.rearm())
        return None if disarm else recorder

    monkeypatch.setattr(main, "billing_client", _arm)
    # Set on the class directly rather than through `monkeypatch`, because `client` is an instance
    # attribute and monkeypatch would refuse to set a class attribute it cannot find. The teardown
    # below deletes it again, which puts every instance back to reading its own `__dict__`.
    #:
    #: Held in a local and reached from `_arm` through the closure, *not* re-read as
    #: `type(main.billing).client` inside `_arm`. A data descriptor lives on the class and
    #: `type(instance).attr` goes through `__get__` like any other read, so that expression hands
    #: back the pinned *client* and not the descriptor, and a helper written to flip the pin instead
    #: asked a `FakeStripe` for `.disarm()`. Two names for one object, and this one is the truth.
    pin = PinAttribute(recorder)
    type(main.billing).client = pin

    #: The recorder, for a test that needs to name the double rather than reach it through
    #: `client.fake()`; and `client.disarm()`, which flips this app into the no-processor posture
    #: the pin would otherwise refuse. See `PinAttribute.disarm`.
    client.recorder = recorder

    def _arm_client():
        """Refresh this app's client the way the re-arming routes do, and hand it back.

        Derived from the environment, so a test that removed the funnel key gets `None`, one that
        turned charges off gets `None`, and one that restored either is armed again. Calling this is
        what makes a posture a *state the app is in* rather than a value a test wrote once: the
        webhook route re-arms on the way in, so anything the test assigned by hand is gone by the
        first request, and an assertion afterwards measures an app that no longer matches the test's
        premise.

        A test may still ask for a posture outright with `client.disarm()` / `client.arm()`, which
        pass the flag explicitly. Prefer the derived call: it fails when the gating changes instead
        of papering over it.
        """
        main.billing.client = main.billing_client()
        return main.billing.client

    client.refresh_client = _arm_client

    def _set_posture(disarm: bool):
        main.billing_client(disarm=disarm)
        main.billing.client = None if disarm else recorder
        return main.billing.client

    client.disarm = lambda: _set_posture(True)
    client.arm = lambda: _set_posture(False)

    client.fake = lambda: recorder
    client.main = main
    client.billing = main.billing
    client.tmp = tmp_path
    client.stripe = fresh
    try:
        yield client
    finally:
        del type(main.billing).client
        popped = _ACTIVE.pop()
        assert popped is fresh, f"fixture nesting surprised: popped {popped!r}, expected {fresh!r}"


def subscribe(client) -> str:
    """Walk the funnel, pay, come back. Returns the Checkout session id.

    Clears the recorder first, which is what wipes the outage switches: a test that ends with
    `/subscriptions/` marked as failing leaves that mark on the one client the app builds, and the
    next test that borrows the fixture would find its *setup* charge refused and report a 500 from
    a route it never touched. Clearing in place rather than substituting a new object is forced by
    the app rebuilding its client per request, which would drop a substitute on the next call.
    """
    client.fake().reset()
    client.get("/")
    walk(client)
    client.get("/preview")
    client.get("/checkout")
    started = post(client, "/checkout/start", [("email", "payer@example.com")])
    assert started.status_code == 303, started.text[:600]
    url = started.headers["location"]
    assert url.startswith("https://checkout.stripe.test/"), url
    session_id = url.rsplit("/", 1)[-1]
    done = client.get(f"/checkout/complete?session_id={session_id}")
    assert done.status_code == 200, done.text[:600]
    return session_id


def visitor_of(client) -> str:
    """The visitor key this conversation holds, unsigned, as the billing tables store it."""
    return jar_of(client)["rl_vid"].rsplit(".", 1)[0]


def outbox(client, needle: str) -> list:
    return sorted((client.tmp / "outbox").glob(f"*{needle}*"))


def point_at_unknown_subscription(client) -> None:
    """Make the app's billing client ignore subscription mutations.

    For the tests that drive *state* through a signed webhook rather than through a button:
    those need the local write path without pretending this fake holds a subscription that
    only exists in the event payload.
    """
    client.billing.client.ignore_prefixes = ("/subscriptions/",)


def set_period_end(client, *, when: datetime, status: str = "active") -> None:
    row = client.billing.store.get_subscription(visitor_of(client))
    client.billing.store.upsert_subscription(
        visitor_key=row["visitor_key"], status=status,
        current_period_end=when.replace(microsecond=0).isoformat())


# ---------------------------------------------------------------------------
# Baseline offer, in every slot
# ---------------------------------------------------------------------------

def test_baseline_offer_is_one_dollar_seven_days_then_thirty_nine(live):
    """The spec's numbers, asserted on the page rather than on config, because the failure this
    guards is a disclosure that stopped rendering while the config stayed correct."""
    live.get("/")
    walk(live)
    checkout = live.get("/checkout")
    assert checkout.status_code == 200
    assert "$1 today for 7 days, then $39/month until you cancel" in checkout.text


def test_env_names_match_the_spec_and_price_the_funnel(monkeypatch):
    """`TRIAL_AMOUNT_MINOR` and friends are the names the brief writes them with, so a `.env`
    copied from the spec must not silently fall back to the built-in defaults."""
    import os
    from unittest import mock
    import config
    with mock.patch.dict(os.environ, {
            "TRIAL_AMOUNT_MINOR": "1499", "TRIAL_DAYS": "14",
            "REBILL_AMOUNT_MINOR": "2499",
            "FUNNEL_TRIAL_AMOUNT_MINOR": "", "FUNNEL_TRIAL_DAYS": "",
            "FUNNEL_REBILL_AMOUNT_MINOR": ""}, clear=False):
        settings = config.FunnelSettings.from_env()
    assert (settings.trial_amount_minor, settings.trial_days,
            settings.rebill_amount_minor) == (1499, 14, 2499)


def test_prefixed_name_wins_when_both_are_set(monkeypatch):
    """Documented precedence: the `FUNNEL_`-prefixed variable is this app's own, so a bare
    `TRIAL_AMOUNT_MINOR` exported by something else on the same host must not price this lane.
    """
    import os
    from unittest import mock
    import config
    with mock.patch.dict(os.environ, {
            "TRIAL_AMOUNT_MINOR": "999", "FUNNEL_TRIAL_AMOUNT_MINOR": "100"}, clear=False):
        settings = config.FunnelSettings.from_env()
    assert settings.trial_amount_minor == 100


# ---------------------------------------------------------------------------
# Merchant isolation
# ---------------------------------------------------------------------------

def test_key_type_is_decoded_from_the_key_itself(monkeypatch):
    """Reading `key_type`/`acct_type` off the key is what makes a shared-merchant mistake
    visible on `/health` before it becomes a lane-B statement descriptor on a lane-C customer.
    """
    import os
    from unittest import mock
    with mock.patch.dict(os.environ, {
            "STRIPE_SECRET_KEY_FUNNEL": "sk_live_funnel_zzz",
            "STRIPE_PUBLISHABLE_KEY_FUNNEL": ""}, clear=False):
        keys = stripe_billing.FunnelKeys.load()
    assert keys.key_type == "live"
    assert keys.acct_type == "standard"
    assert keys.usable


def test_publishable_secret_mode_mismatch_is_refused(monkeypatch):
    import os
    from unittest import mock
    with mock.patch.dict(os.environ, {
            "STRIPE_SECRET_KEY_FUNNEL": "sk_live_funnel_x",
            "STRIPE_PUBLISHABLE_KEY_FUNNEL": "pk_test_funnel_x"}, clear=False):
        with pytest.raises(stripe_billing.StripeMisconfigured) as exc:
            stripe_billing.FunnelKeys.load()
    assert "test" in str(exc.value).lower(), \
        "the message must name both spellings, since that is what the operator must fix"


def test_charges_on_but_no_key_still_boots_and_keeps_the_two_nos_apart(tmp_path, monkeypatch):
    """Two different no's, kept distinguishable: a switch flipped off is a decision, a missing
    key is a gap someone must fix, and a checkout page that conflates them teaches the operator
    to wait for something that will not arrive."""
    import os
    from unittest import mock
    import config
    monkeypatch.delenv("STRIPE_SECRET_KEY_FUNNEL", raising=False)
    monkeypatch.setenv("FUNNEL_CHARGES_ENABLED", "true")
    settings = config.FunnelSettings.from_env()
    assert settings.charges_enabled is True
    assert stripe_billing.client_for(settings) is None


# ---------------------------------------------------------------------------
# Checkout: live
# ---------------------------------------------------------------------------

def test_checkout_creates_a_session_pricing_the_trial_today(live):
    """The entry price must be its own line item.

    Without it Stripe bills the subscription's own price today and `trial_end` only defers the
    *second* charge, so a customer would be charged $39 on a screen that promised $1. That is a
    disclosure failure created by the integration rather than by the copy, which is why it is
    asserted on the outgoing request and not on the page.
    """
    subscribe(live)
    request = live.fake().session_request
    amounts = [line["price_data"]["unit_amount"] for line in request["line_items"]]
    assert 100 in amounts, f"trial charge missing from the invoice: {amounts}"
    recurring = [line for line in request["line_items"] if "recurring" in line["price_data"]]
    assert len(recurring) == 1, "two recurring lines would produce two charges a month"
    assert recurring[0]["price_data"]["unit_amount"] == 3900
    assert recurring[0]["price_data"]["recurring"]["interval"] == "month"
    assert request["subscription_data"]["trial_end"] > time.time()


def test_checkout_session_carries_the_visitor_and_absolute_urls(live):
    subscribe(live)
    request = live.fake().session_request
    assert request["client_reference_id"], "no reference, so a webhook cannot find the visitor"
    for key in ("success_url", "cancel_url"):
        assert request[key].startswith("http"), request[key]


def test_trial_end_in_the_past_is_refused(live):
    """A zero-length trial would charge the rebill amount today, contradicting the screen."""
    fake = live.fake(); fake.reset()
    # `pytest.raises(Exception)`, deliberately loose. The guard is a guard because reaching
    # the processor with a dead trial would charge the rebill amount on a screen that promised
    # $1, so the behaviour worth pinning is "it refuses"; which exception type it refuses with
    # is a refactor detail, and pinning the class here would make a harmless retype of
    # `stripe_billing`'s hierarchy look like a compliance regression.
    with pytest.raises(Exception) as exc:
        fake.create_checkout_session(
            success_url="https://x.test/ok", cancel_url="https://x.test/no",
            customer_email="a@b.test", client_reference_id="v1",
            trial_amount_minor=100, trial_days=7, rebill_amount_minor=3900,
            currency="usd", trial_end_epoch=int(time.time()) - 60)
    assert "trial_end" in str(exc.value), \
        "the refusal must name the field, since that is what an operator acts on"
    assert not fake.calls, "a refused session still reached the processor"


def test_invalid_email_is_rejected_before_a_session_exists(live):
    fake = live.fake(); fake.reset()
    live.billing.client = fake
    live.get("/")
    walk(live)
    response = post(live, "/checkout/start", [("email", "not-an-email")])
    assert response.status_code == 422
    assert not fake.calls_to("/checkout/sessions"), "a session was created for a bad address"
    assert "email" in response.text.lower()


def test_completing_a_stranger_session_does_not_credit_my_plan(live):
    """The session id arrives in a URL, so it is attacker-shaped. The processor's own
    `client_reference_id` is the only thing that decides whose subscription this is."""
    subscribe(live)
    live.fake().seed_subscription("sub_stranger")
    live.fake().sessions["cs_stranger"] = {
        "id": "cs_stranger", "mode": "subscription",
        "client_reference_id": "someone-else", "subscription": "sub_stranger",
        "customer_details": {"email": "other@example.com"}}
    response = live.get("/checkout/complete?session_id=cs_stranger")
    assert response.status_code == 400
    assert "4-week plan" not in response.text


def test_unknown_session_is_handled_without_a_five_hundred(live):
    """Paying and coming back in another browser is normal, and so is a stale link. Either way
    the page must name whether money moved, because "something went wrong" is what makes a
    customer submit the $1 again and end up with two trials."""
    subscribe(live)
    point_at_unknown_subscription(live)
    # `expect_status`: the processor 404s on a session that does not exist, and the handler's
    # 400 page is what is under test. Left to the default, the transport re-raises that 404 into
    # this call as a traceback and the assertion never runs.
    response = live.request("GET", "/checkout/complete?session_id=cs_nope", expect_status=400)
    assert response.status_code == 400
    assert "statement" in response.text.lower(), "the page must say whether money moved"


def test_dashboard_shows_the_processors_period_end_as_a_fact(live):
    """Checklist 6, and the reason `renewal_is_real` exists: a date the processor gave is
    stated flatly, a date we guessed is qualified. Rendering a guess as a promise is the
    renewal-date surprise the spec names as the top refund reason; the reverse error, a real
    date still hedged, tells a member the page does not know their own account."""
    subscribe(live)
    dashboard = live.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Next billing date" in dashboard.text, "a real date must be stated as the date"
    assert "about" not in dashboard.text.split("Next billing date")[1][:160].lower()


def test_rehearsal_dashboard_qualifies_an_estimated_date(client):
    """The other side of the same rule, driven the way the funnel's own harness drives it.

    The `client` fixture is the `conftest.py` one (charges off), so the renewal date here is
    the estimate rather than a period end the processor returned. The harness writes the trial
    row directly rather than clicking a rehearsal button that does not yet exist: until the
    rehearsal screen gains an email field, that row has no way to acquire an address, and a
    receipt with no recipient is a different test's business.
    """
    client.get("/")
    walk(client)
    client.main.billing.store.upsert_subscription(
        visitor_key=jar_of(client)["rl_vid"].rsplit(".", 1)[0],
        email="member@example.com", status="trialing")
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "No plan on this browser yet" not in dashboard.text
    assert "Estimated next bill" in dashboard.text, "a guessed date presented as a fact"
    assert "Next billing date" not in dashboard.text
    assert "$39/month" in dashboard.text
    assert "Cancel subscription" in dashboard.text


def test_rehearsal_dashboard_without_a_subscription_is_not_an_error(client):
    """A visitor who never paid reaches a member page rather than a 500 or a blank, because
    this is also where an honest "nothing here yet" has to be said."""
    client.get("/")
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "No plan on this browser yet" in dashboard.text


def test_each_fixtures_app_raises_and_catches_the_same_stripe_classes(client, live):
    """The two billing apps coexist, each internally consistent, and that sentence is the whole
    contract this harness has.

    Borrowing both fixtures is the point. Each builds its app a different way, on purpose:
    `client` keeps the registered `stripe_billing` and rebinds its freshly imported tree to it,
    `live` builds against a *private* copy because it patches `StripeClient`. So two different
    copies of the module are alive at once and a third is registered under the name, which is the
    arrangement that made a process-wide check report a mismatch between two perfectly healthy
    apps while hiding a real one elsewhere. Asking each client about *its* module is the only
    question with a correct answer, and a test that borrows one fixture cannot show the
    difference.

    Named for the failure it prevents rather than for the mechanism: a test that raises a class
    the app cannot catch reports an app crash, forever, and reads as a bug in the code under test.
    """
    assert_app_and_suite_agree_on_stripe_classes(client, client.stripe)
    assert_app_and_suite_agree_on_stripe_classes(live, live.stripe)
    assert client.stripe is not live.stripe, \
        "the two fixtures now share a module, so this test has stopped proving anything"


def test_rehearsal_checkout_click_charges_nothing_and_creates_no_row(client):
    """Rehearsal records intent as an analytics event and stops there.

    Recorded as an assertion because "does rehearsal create a subscription row" is a decision,
    and the dashboard, the renewal sweep and the receipt all behave differently depending on
    the answer. If a later step gives the rehearsal screen an email field and a real row, this
    test is the thing that has to change deliberately.
    """
    client.get("/")
    walk(client)
    client.get("/preview")
    done = post(client, "/checkout", [])
    assert done.status_code == 200
    assert "Intent recorded, nothing charged" in done.text
    key = jar_of(client)["rl_vid"].rsplit(".", 1)[0]
    assert client.main.billing.store.get_subscription(key) is None
    assert client.main.billing_client() is None


def test_dashboard_states_amount_and_date_above_the_plan(live):
    subscribe(live)
    body = live.get("/dashboard").text
    assert "$39/month" in body
    # The billing block must precede the plan, or "always visible" means "scroll past the
    # product to find the price", which is checklist 6's failure mode.
    assert body.find("Next billing date") < body.find("Week 1"), \
        "the billing block renders below the plan"


def test_dashboard_shows_the_four_week_plan_unlocked(live):
    subscribe(live)
    body = live.get("/dashboard").text
    for week in ("Week 1", "Week 2", "Week 3", "Week 4"):
        assert week in body, week
    assert "/account/plan.txt" in body and "/account/plan.pdf" in body
    # Weeks 2-4 were the paywall claim; on the member page they must read as unlocked.
    assert "Locked" not in body


def test_plan_export_is_a_downloadable_attachment(live):
    subscribe(live)
    response = live.get("/account/plan.txt")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    body = response.text.lower()
    for needle in ("meal", "log:", "monday", "fallback"):
        assert needle in body, needle


def test_a_sale_records_a_row_even_when_the_processor_returns_no_card(live):
    """A thin processor response must not cost the funnel its record of a sale.

    A paid trial whose subscription has no expanded items, or no payment method attached, is a
    shape Stripe really returns. `checkout_complete` used to write those blanks as `NULL`, and a
    column declared `not null default ''` rejects an explicit `NULL` it is handed while accepting
    the default for a column the statement omits, so the insert raised `IntegrityError` and the
    member hit a 500 on the way to their thank-you page with a *charged card* and no row behind
    it: no plan to open, no renewal date, no cancel button, and nothing for the renewal sweep to
    find. Asserted through the real path rather than on the row, because the crash was in the
    write.
    """
    subscribe(live)
    session_id = live.fake().session_request and list(live.fake().sessions)[-1]
    key = visitor_of(live)
    live.fake().reset()
    # The shape: a session that exists, with a subscription that has neither items nor a card.
    live.fake().sessions[session_id] = {
        "id": session_id, "object": "checkout.session", "mode": "subscription",
        "client_reference_id": key, "subscription": "sub_thin",
        "url": f"https://checkout.stripe.test/{session_id}"}
    live.fake().subscriptions["sub_thin"] = {
        "id": "sub_thin", "object": "subscription", "status": "trialing",
        "customer": "cus_thin", "items": {"object": "list", "data": []},
        "trial_end": int(time.time()) + 7 * 86400,
        "current_period_end": int(time.time()) + 7 * 86400}

    again = live.get(f"/checkout/complete?session_id={session_id}")
    assert again.status_code == 200, crash_problem(again)
    row = live.billing.store.get_subscription(key)
    assert row is not None, "a charged customer without a row cannot cancel, see no plan, and " \
                            "never hear their renewal date"
    assert row["subscription_id"] == "sub_thin"
    assert row["subscription_item_id"] == "", "a blank must be stored as the empty string"
    assert live.get("/dashboard").status_code == 200


def test_plan_pdf_export_is_a_pdf_or_sends_you_to_the_text(live):
    """Two acceptable outcomes, and one that is not: a 200 whose body is plain text wearing a
    `.pdf` filename, which downloads as a file nobody can open. A printer that is missing or
    has failed redirects to the text export instead, which is the same content with an honest
    name. `textutil` is a macOS binary, so this suite must pass on a Linux box too.
    """
    subscribe(live)
    response = live.get("/account/plan.pdf", follow_redirects=False)
    if response.status_code == 200:
        assert response.content.startswith(b"%PDF"), response.headers.get("content-type")
        assert "application/pdf" in response.headers["content-type"]
    else:
        assert response.status_code == 302
        assert response.headers["location"] == "/account/plan.txt"
        text = live.get(response.headers["location"])
        assert "text/plain" in text.headers["content-type"]
        assert "attachment" in text.headers["content-disposition"]


def test_receipt_email_carries_every_checklist_item(live):
    """Slot 5: amount charged, renewal amount, renewal date, one-click cancel link, monitored
    reply address. Asserted on the rendered message, since that is the artefact a complainant
    would produce."""
    subscribe(live)
    written = outbox(live, "trial-receipt")
    assert written, "no receipt written to the outbox"
    body = written[-1].read_text(errors="replace")
    assert "$1" in body and "$39/month" in body
    assert datetime.now(timezone.utc).strftime("%Y") in body, \
        "a renewal date with a year, not a bare day name"
    assert "/account/cancel" in body
    assert "help@routine-lab.test" in body
    assert "Reply-To" in body
    assert "visa ending 4242" in body, \
        "a card hint is how a charge gets recognised rather than disputed"


def test_rehearsal_keeps_phase_one_card_screen_and_says_why(client):
    """Charges off must keep rendering Phase 1's page, disabled inputs and its own wording,
    rather than a live-looking screen with no payment widget behind it."""
    client.get("/")
    walk(client)
    checkout = client.get("/checkout")
    assert "disabled" in checkout.text
    assert "rehearsal" in checkout.text.lower()
    assert "STRIPE_SECRET_KEY_FUNNEL" in client.get("/health").text


# ---------------------------------------------------------------------------
# The cancel sequence
# ---------------------------------------------------------------------------

def test_cancel_is_executed_before_any_offer_appears(live):
    """§2.3 rule 1 as a page assertion: the confirmation sits above the offers.

    A template could satisfy a weaker test by mentioning "cancelled" anywhere. This checks
    order, which is the actual rule.
    """
    subscribe(live)
    response = post(live, "/account/cancel", [])
    assert response.status_code == 200
    body = response.text
    confirmed = body.find("Nothing more will be charged")
    first_offer = body.find("Pause")
    assert confirmed != -1, "cancellation confirmation headline missing"
    assert first_offer != -1, "save-offer menu missing"
    assert confirmed < first_offer, "an offer renders above the cancellation confirmation"
    assert "/account/cancel/offer" in body


def test_cancel_sets_period_end_rather_than_deleting(live):
    """Access must run to the end of the paid period; our own terms promise it, and revoking
    access on a cancel is what turns a clean cancellation into a refund request."""
    subscribe(live)
    post(live, "/account/cancel", [])
    calls = live.fake().calls_to("/subscriptions/")
    assert calls, "the processor was never told"
    _method, _path, data = calls[-1]
    assert data.get("cancel_at_period_end") is True
    assert not data.get("cancel_at"), "an immediate cancel_at cuts access off today"
    assert not [call for call in live.fake().calls if call[0] == "DELETE"], \
        "a DELETE ends access today and forfeits the days already paid for"


def test_cancel_writes_state_and_sends_confirmation(live):
    subscribe(live)
    assert "Cancel subscription" in live.get("/dashboard").text
    post(live, "/account/cancel", [])
    dashboard = live.get("/dashboard")
    assert "Cancelled. Nothing more will be charged." in dashboard.text
    assert "Access until" in dashboard.text, \
        "a cancelled member must be able to re-read the date"
    assert "Cancel subscription" not in dashboard.text, "cancel offered a second time"
    written = outbox(live, "cancellation")
    assert written, "no cancellation email"
    assert "Reactivate" in written[-1].read_text(errors="replace")


def test_second_cancel_click_is_not_a_second_event(live):
    subscribe(live)
    post(live, "/account/cancel", [])
    calls = len(live.fake().calls_to("/subscriptions/"))
    again = post(live, "/account/cancel", [])
    assert again.status_code == 200
    assert "Already cancelled" in again.text
    assert len(live.fake().calls_to("/subscriptions/")) == calls, \
        "a repeat click re-hit the processor"


def test_processor_refusal_is_reported_as_not_cancelled(live):
    """The rule a friendly UI wants to break: showing "Cancelled" over a subscription the
    processor still bills. A cancel that failed must say so and must not offer a save."""
    subscribe(live)
    # 409 rather than 502 on purpose: a rejected cancellation is a reported failure, and what
    # this test is about is the *report*, not the transport. The 5xx case has its own test.
    live.fake().fail_mutations_of("/subscriptions/", 409,
                                  "The subscription could not be cancelled right now.")
    response = post(live, "/account/cancel", [], expect_status=200)
    # `crash_problem` first, so a handler that raised reports the frame that raised rather than
    # a bare "500 != 200", and the status assert second, so a page that rendered but rendered the
    # wrong thing is not blamed on a crash.
    assert crash_problem(response) == "", crash_problem(response)
    assert response.status_code == 200
    assert "could not cancel" in response.text.lower()
    assert "Pause" not in response.text, "an offer appeared for a subscription still billing"
    assert "Cancelled. Nothing more will be charged." not in live.get("/dashboard").text
    row = live.billing.store.get_subscription(visitor_of(live))
    assert row["status"] == "trialing", f"local state moved to {row['status']} on a failed cancel"


def test_a_processor_outage_is_reported_as_a_failure(live):
    """A 502 is an HTTP answer, so it reaches the app as a `StripeError` and `cancel_now`
    reports it the same way it reports a rejection: nothing claimed, no offer offered."""
    subscribe(live)
    live.fake().fail_mutations_of("/subscriptions/", 502, "the payment provider is unavailable")
    response = post(live, "/account/cancel", [], expect_status=200)
    assert response.status_code == 200, response.status_code
    assert "could not cancel" in response.text.lower()
    assert "Pause" not in response.text, "an offer appeared after an unconfirmed cancel"
    row = live.billing.store.get_subscription(visitor_of(live))
    assert row["status"] == "trialing", \
        f"a provider outage wrote {row['status']}, so the local row now claims a cancel"
    assert "Cancel subscription" in live.get("/dashboard").text, \
        "the member was shown no way to ask again"


def test_a_dead_socket_fails_loudly_without_claiming_anything(live):
    """A socket that died is not a rejection, and the app does not pretend it is.

    It reaches the route as an unhandled error rather than as a message on the cancel screen,
    which is a rough edge worth naming rather than smoothing over in a fake: a 500 on the
    cancel button is bad, and a 500 that had already written "cancelled" locally would be
    worse. Asserted as the 500 it is, because "we do not know whether that cancelled" is a
    different statement from "it did", and pretending otherwise is the friendlier-looking lie.
    The property that must never break, and does not, is that nothing is written and nothing is
    offered, so the member's next visit shows the same live subscription and the same button.
    Turning the 500 into a retry-with-explanation is a UX improvement rather than a compliance
    one, and is left out of Phase 2 on purpose.
    """
    subscribe(live)
    live.fake().fail_mutations_of("/subscriptions/", 0,
                                  "transport error calling POST: ConnectError: boom")
    response = post(live, "/account/cancel", [], expect_status=500)
    assert response.status_code == 500, \
        "a dead socket is not a rejection, and the app does not dress it up as one"
    assert "Pause" not in response.text
    row = live.billing.store.get_subscription(visitor_of(live))
    assert row["status"] == "trialing", \
        f"a failed request wrote {row['status']}, so a customer would believe they cancelled"


def test_cancel_affords_no_confirmation_step(live):
    """Checklist 7: post-login, one click, zero navigation, zero retention gate *before* the
    action. Asserted structurally: the dashboard's cancel affordance is a form that submits and
    is done, with no field, select or textarea between the visitor and the cancellation."""
    subscribe(live)
    dashboard = live.get("/dashboard").text
    start = dashboard.find('action="/account/cancel"')
    form = dashboard[start:dashboard.find("</form>", start)]
    for tag in ("<input", "<select", "<textarea"):
        assert tag not in form, f"{tag} in the cancel form is a gate: {form[:200]}"
    assert "<button" in form


# ---------------------------------------------------------------------------
# Save offers
# ---------------------------------------------------------------------------

def test_offer_menu_lists_three_real_options_plus_the_exit(live):
    subscribe(live)
    body = post(live, "/account/cancel", []).text
    for needle in ("Pause", "$19/month", "$19.5", "No thanks, keep me cancelled"):
        assert needle in body, needle


def test_discount_option_hidden_without_a_configured_coupon():
    """Spec rule 4 in its negative form: an offer the processor cannot honour is a lie with
    extra steps, so a missing coupon removes the option rather than faking it locally."""
    import os
    from unittest import mock
    import config
    import save_offer
    from funnel_copy import build_copy
    with mock.patch.dict(os.environ, {"FUNNEL_DISCOUNT_COUPON_ID": ""}, clear=False):
        settings = config.FunnelSettings.from_env()
        menu = save_offer.offer_menu(settings, build_copy(settings))
    assert "discount" not in menu.keys()
    assert set(menu.keys()) == {"pause", "downgrade"}
    with mock.patch.dict(os.environ, {"FUNNEL_DISCOUNT_COUPON_ID": "half_for_two"},
                         clear=False):
        settings = config.FunnelSettings.from_env()
        assert "discount" in save_offer.offer_menu(settings, build_copy(settings)).keys()


def test_accepting_pause_resumes_billing_and_pauses_collection(live):
    subscribe(live)
    post(live, "/account/cancel", [])
    response = post(live, "/account/cancel/offer", [("offer", "pause")])
    assert response.status_code == 200
    assert "Paused" in response.text
    pauses = [call for call in live.fake().calls_to("/subscriptions/")
              if call[2].get("pause_collection")]
    assert pauses, "the pause was only written locally, so the card still charges"
    assert pauses[-1][2]["pause_collection"]["resumes_at"] > time.time()
    assert "Paused." in live.get("/dashboard").text


def test_accepting_downgrade_moves_the_price_to_nineteen(live):
    subscribe(live)
    post(live, "/account/cancel", [])
    response = post(live, "/account/cancel/offer", [("offer", "downgrade")])
    assert response.status_code == 200
    assert "$19/month" in response.text
    updates = live.fake().calls_to("/subscription_items/")
    assert updates, "the tier was renamed locally without moving the price"
    assert updates[-1][2]["price_data"]["unit_amount"] == 1900
    assert "$19/month" in live.get("/dashboard").text


def test_accepting_discount_sends_the_coupon_not_a_local_guess(live):
    subscribe(live)
    post(live, "/account/cancel", [])
    response = post(live, "/account/cancel/offer", [("offer", "discount")])
    assert response.status_code == 200
    coupons = [call for call in live.fake().calls_to("/subscriptions/")
               if call[2].get("discounts")]
    assert coupons, "the discount was never applied at the processor"
    assert coupons[-1][2]["discounts"][0]["coupon"] == "half_for_two"


def test_a_downgraded_member_gets_half_of_lite_not_half_of_full(live):
    """Spec rule 4's composition case: taking the discount while on lite must not lift the
    member back to the plan they just declined, and must not price them off the wrong base."""
    subscribe(live)
    post(live, "/account/cancel", [])
    post(live, "/account/cancel/offer", [("offer", "downgrade")], expect_status=200)
    # The expected price is computed here from the settings rather than copied from the code
    # under test. `half_lite_price` rounds *up* to a cent (`math.ceil`), and the invoice the
    # processor raises matches that rounding, so a hardcoded "$9.5" would assert this suite's
    # own arithmetic error rather than the app's: a cent short of a quarter is half of $9.51,
    # not half of the lite price.
    assert live.main.build_copy(live.main.settings).half_lite_price == \
        f"${live.main.settings.lite_amount_minor / 200:.2f}".rstrip("0").rstrip("."), \
        "the arithmetic this test names is half of the lite amount, not half of the full plan"
    post(live, "/account/cancel/offer", [("offer", "discount")])
    row = live.billing.store.get_subscription(visitor_of(live))
    assert row["plan"] == "lite", f"the discount changed the tier to {row['plan']}"
    assert row["status"] != "cancelled_at_period_end", "the save did not stop the cancellation"
    assert row["discount_months"] == 2, row["discount_months"]
    updates = live.fake().calls_to("/subscription_items/")
    assert updates[-1][2]["price_data"]["unit_amount"] == 950
    # The screen a member reads must price them the way their invoice will, so the figure on
    # the account page is half of *lite* and the next-charge line names the lite base. Compared
    # by value, not by a hand-written string: `money()` strips trailing zeros, so a literal here
    # drifts the moment the lite price is a round number, and drift in a price assertion is how
    # a wrong price passes.
    account = live.get("/dashboard").text
    half_lite = live.main.build_copy(live.main.settings).half_lite_price
    full_half = live.main.build_copy(live.main.settings).half_price
    assert half_lite in account, \
        f"the price shown is not half of what this member pays, expected {half_lite}"
    assert full_half not in account, \
        f"the full plan's half price ({full_half}) quoted to a lite member"
    next_charge = account.split("Next charge:")[1][:160]
    assert "$39/month" not in next_charge, \
        "the next-charge line names the full plan's price after a downgrade"


def test_failed_offer_does_not_claim_the_save(live):
    """The inversion of the cancel rule, and easier to get wrong because the screen wants to be
    encouraging here. A save the processor refused must leave the customer cancelled."""
    subscribe(live)
    post(live, "/account/cancel", [])
    live.fake().fail_mutations_of("/subscription_items/", 400, "No such price")
    response = post(live, "/account/cancel/offer", [("offer", "downgrade")], expect_status=200)
    assert response.status_code == 200
    assert "could not be applied" in response.text.lower()
    row = live.billing.store.get_subscription(visitor_of(live))
    assert row["status"] == "cancelled_at_period_end", \
        f"a refused save left the row '{row['status']}'"
    assert row["plan"] != "lite"


def test_every_outcome_is_logged_by_name(live):
    """§2.3 rule 6: outcomes are recorded under stable names so save-offer economics are
    computable later. Asserted through the app, so a route that forgets to log fails."""
    subscribe(live)
    post(live, "/account/cancel", [])
    post(live, "/account/cancel/offer", [("offer", "pause")])
    counts = live.billing.store.event_counts()
    assert counts.get("pause_selected") == 1, counts


def test_declining_everything_is_itself_recorded(live):
    """Without option D logged, the denominator for "does the save offer work" is wrong in the
    flattering direction: acceptances divided by acceptances is one hundred percent."""
    subscribe(live)
    post(live, "/account/cancel", [])
    response = post(live, "/account/cancel/offer", [("offer", "keep_cancelled")])
    assert response.status_code == 200
    assert live.billing.store.event_counts().get("cancel_confirmed") == 1
    row = live.billing.store.get_subscription(visitor_of(live))
    assert row["status"] == "cancelled_at_period_end", row["status"]
    assert live.fake().calls_to("/subscription_items/") == []


def test_the_exit_button_reaches_the_route_it_claims_to(live):
    """The exit is a button that posts, not a link that hides, so that "doing nothing" is not
    the only way to say no.

    Asserted on the markup rather than assumed: when that button was a plain link to the
    account page, every screen still looked right and no decline was ever recorded, which is
    the quietest possible way for the funnel's main retention metric to be wrong.
    """
    subscribe(live)
    body = post(live, "/account/cancel", []).text
    label = body.find("No thanks, keep me cancelled")
    assert label != -1, "the exit option is missing from the cancel screen"
    block = body[label:]
    start = body.rfind("<form", 0, label)
    form = body[start:body.find("</form>", start)]
    assert 'action="/account/cancel/offer"' in form, form[:200]
    assert 'value="keep_cancelled"' in form, form[:200]
    assert "same thing as doing nothing" in block[:400], \
        "the exit must say it is equivalent to leaving, or it reads as one more prompt"


def test_survey_is_after_cancellation_and_optional(live):
    """Reachable on its own, never required, and it states that the account is already
    cancelled. An empty submission is a valid submission."""
    subscribe(live)
    post(live, "/account/cancel", [])
    form = live.get("/account/survey")
    assert form.status_code == 200
    assert "cancelled" in form.text.lower()
    assert "Skip" in form.text
    assert post(live, "/account/survey", []).status_code == 200
    assert live.billing.store.event_counts().get("survey_reason") == 1


def test_survey_with_answers_records_the_reason(live):
    subscribe(live)
    post(live, "/account/cancel", [])
    response = post(live, "/account/survey",
                    [("reason", "cost"), ("note", "Too steep at $39")])
    assert response.status_code == 200
    with __import__("sqlite3").connect(live.billing.store.path) as conn:
        stored = conn.execute(
            "select detail_json from funnel_save_offer_events "
            "where event = 'survey_reason' order by id desc limit 1").fetchone()
    assert json.loads(stored[0])["reason"] == "cost"


def test_a_stale_tab_cannot_select_an_unoffered_option(live):
    """A menu item removed from config must not be appliable from a page opened earlier. The
    response is the cancelled page rather than an error, because that is the state requested."""
    subscribe(live)
    post(live, "/account/cancel", [])
    response = post(live, "/account/cancel/offer", [("offer", "upgrade_to_annual")])
    assert response.status_code == 200
    assert live.billing.store.get_subscription(visitor_of(live))["plan"] != "lite"
    assert not live.fake().calls_to("/subscription_items/")


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def _signed(body: bytes, secret: str, *, timestamp: int | None = None,
            signature: str | None = None) -> str:
    import hashlib
    import hmac
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    digest = signature or hmac.new(secret.encode(), f"{stamp}.".encode() + body,
                                   hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def hook(client, event: dict):
    """POST one event, signed with the fixture's webhook secret."""
    body = json.dumps(event).encode()
    return client.request("POST", "/webhooks/stripe", content=body, headers={
        "content-type": "application/json",
        "stripe-signature": _signed(body, "whsec_test_abc123")})


def _event(kind: str, obj: dict, *, event_id: str = "",
           type_name: str = "customer.subscription") -> dict:
    return {"id": event_id or f"evt_{kind}", "type": f"{type_name}.{kind}",
            "live_mode": False, "data": {"object": obj}}


def test_valid_signed_event_updates_state(live):
    session_id = subscribe(live)
    subscription_id = live.fake().sessions[session_id]["subscription"]
    key = visitor_of(live)
    response = hook(live, _event("deleted", {"id": subscription_id, "status": "canceled",
                                             "metadata": {"visitor_key": key}}))
    assert response.status_code == 200, response.text[:300]
    assert live.billing.store.get_subscription(key)["status"] == "canceled"


def test_event_is_matched_by_subscription_id_without_metadata(live):
    """Stripe's own subscription objects do not always carry our metadata, and an event that
    only says `sub_...` must still find the row, or a real cancellation would go unrecorded."""
    session_id = subscribe(live)
    subscription_id = live.fake().sessions[session_id]["subscription"]
    response = hook(live, _event("deleted", {"id": subscription_id, "status": "canceled"}))
    assert response.status_code == 200
    assert live.billing.store.get_subscription(visitor_of(live))["status"] == "canceled"


def test_bad_signature_is_rejected(live):
    subscribe(live)
    body = json.dumps(_event("deleted", {"id": "sub_x", "status": "canceled"})).encode()
    response = live.request(
        "POST", "/webhooks/stripe", content=body,
        headers={"content-type": "application/json",
                 "stripe-signature": _signed(body, "whsec_test_abc123",
                                             signature="0" * 64)})
    assert response.status_code == 400


def test_stale_timestamp_is_rejected(live):
    """Without the tolerance window a captured header is valid forever, and replaying an old
    `subscription.deleted` would cancel a subscription that has since been reactivated."""
    subscribe(live)
    body = json.dumps(_event("deleted", {"id": "sub_x", "status": "canceled"})).encode()
    old = int(time.time()) - 4000
    response = live.request(
        "POST", "/webhooks/stripe", content=body,
        headers={"content-type": "application/json",
                 "stripe-signature": _signed(body, "whsec_test_abc123", timestamp=old)})
    assert response.status_code == 400


def test_unsigned_event_rejected_when_a_secret_is_configured(live):
    subscribe(live)
    body = json.dumps(_event("deleted", {"id": "sub_x", "status": "canceled"})).encode()
    response = live.request("POST", "/webhooks/stripe", content=body,
                            headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_unknown_event_type_returns_200(live):
    """Stripe retries a 5xx indefinitely. Answering an event type this lane does not use with
    an error turns a useless event into a permanent loop that reads like an outage."""
    subscribe(live)
    body = json.dumps({"id": "evt_1", "type": "charge.dispute.created",
                       "live_mode": False, "data": {"object": {"id": "dp_1"}}}).encode()
    response = live.request(
        "POST", "/webhooks/stripe", content=body,
        headers={"content-type": "application/json",
                 "stripe-signature": _signed(body, "whsec_test_abc123")})
    assert response.status_code == 200


def test_event_for_a_subscription_we_never_seen_does_not_500(live):
    """A second browser, a re-run webhook, or a charge made in the dashboard. Loud in the log,
    benign in behaviour, and never a retry loop."""
    subscribe(live)
    response = hook(live, _event("deleted", {"id": "sub_unheard", "status": "canceled"}))
    assert response.status_code == 200


def test_period_end_from_the_processor_replaces_the_estimate(live):
    """A renewal date is a disclosure, so the processor's date must win over the local one and
    survive into the dashboard."""
    session_id = subscribe(live)
    subscription_id = live.fake().sessions[session_id]["subscription"]
    future = datetime.fromtimestamp(
        int((datetime.now(timezone.utc) + timedelta(days=21)).timestamp()), timezone.utc)
    hook(live, _event("updated", {"id": subscription_id, "status": "trialing",
                                  "current_period_end": int(future.timestamp()),
                                  "metadata": {"visitor_key": visitor_of(live)}}))
    row = live.billing.store.get_subscription(visitor_of(live))
    stored = datetime.fromisoformat(row["current_period_end"])
    assert (stored.year, stored.month, stored.day) == \
        (future.year, future.month, future.day), row["current_period_end"]
    assert "Next billing date" in live.get("/dashboard").text


def test_payment_failure_marks_past_due_and_notices_once(live):
    """§2.1: one retry at day +3, then stop. The "once" is what the notification marker is for,
    since Stripe re-sends `invoice.payment_failed` on every attempt."""
    session_id = subscribe(live)
    subscription_id = live.fake().sessions[session_id]["subscription"]
    for attempt in range(3):
        response = hook(live, _event(
            "payment_failed",
            {"id": "in_1", "subscription": subscription_id, "status": "past_due",
             "customer_details": {"email": "payer@example.com"}},
            event_id=f"evt_fail{attempt}", type_name="invoice"))
        assert response.status_code == 200, response.text[:200]
    assert live.billing.store.get_subscription(visitor_of(live))["status"] == "past_due"
    declines = outbox(live, "decline")
    assert len(declines) == 1, f"decline mail sent {len(declines)} times"
    assert "202" in declines[-1].read_text(errors="replace"), "the retry must name a real date"


def test_live_event_on_a_test_key_is_ignored(live):
    """A live-mode webhook aimed at a test key is a misconfiguration, and applying it would let
    one lane's traffic rewrite the other lane's billing state."""
    session_id = subscribe(live)
    subscription_id = live.fake().sessions[session_id]["subscription"]
    event = _event("deleted", {"id": subscription_id, "status": "canceled"})
    event["live_mode"] = True
    hook(live, event)
    assert live.billing.store.get_subscription(visitor_of(live))["status"] == "trialing"


# ---------------------------------------------------------------------------
# Renewal notices
# ---------------------------------------------------------------------------

def test_renewal_notice_is_due_two_days_out_and_only_once(live):
    """§2.1's day −2 notice, and the property that makes a timer safe to re-run after a
    restart: the send is recorded before it is attempted."""
    subscribe(live)
    set_period_end(live, when=datetime.now(timezone.utc) + timedelta(hours=40))
    first = live.request("POST", "/ops/send-notices")
    assert first.status_code == 200, first.text[:200]
    assert first.json()["sent"] == 1, first.json()
    notices = outbox(live, "renewal")
    assert notices, "no renewal notice written"
    body = notices[-1].read_text(errors="replace")
    assert "$39/month" in body and "202" in body and "/account/cancel" in body
    second = live.request("POST", "/ops/send-notices")
    assert second.json()["sent"] == 0, "a customer told twice about one charge"


def test_a_far_off_renewal_gets_no_notice(live):
    subscribe(live)
    set_period_end(live, when=datetime.now(timezone.utc) + timedelta(days=25))
    assert live.request("POST", "/ops/send-notices").json()["sent"] == 0


def test_a_reactorivatod_subscription_gets_its_notice(client):
    """State driven straight from the store, because a resumed-from-pause row is what the
    sweep must not skip. Kept off the `live` fixture's traffic so one failure names one cause.
    """
    client.get("/")
    key = jar_of(client)["rl_vid"].rsplit(".", 1)[0]
    client.main.billing.store.upsert_subscription(
        visitor_key=key, email="member@example.com", status="active",
        current_period_end=(datetime.now(timezone.utc)
                            + timedelta(hours=30)).replace(microsecond=0).isoformat())
    assert client.request("POST", "/ops/send-notices").json()["sent"] == 1


def test_a_cancelled_subscription_gets_no_renewal_notice(client):
    """Telling someone their card is about to be charged, after they cancelled, is its own
    alarm, and alarms of that kind are what produce fraud flags.

    Driven through the store rather than through the `live` fixture's cancel button, because
    that button's real job (reversing the period-end cancel) makes it a poor way to *enter*
    the cancelled state, and a failure here should name the sweep, not the button.
    """
    client.get("/")
    key = jar_of(client)["rl_vid"].rsplit(".", 1)[0]
    client.main.billing.store.upsert_subscription(
        visitor_key=key, email="member@example.com", status="cancelled_at_period_end",
        current_period_end=(datetime.now(timezone.utc)
                            + timedelta(hours=30)).replace(microsecond=0).isoformat())
    assert client.request("POST", "/ops/send-notices").json()["sent"] == 0


def test_a_subscription_with_no_known_date_gets_no_notice(client):
    """Blank means "we do not know", and a mail saying "you will be charged soon" with no date
    behind it is noise wearing a disclosure's clothes."""
    client.get("/")
    key = jar_of(client)["rl_vid"].rsplit(".", 1)[0]
    client.main.billing.store.upsert_subscription(
        visitor_key=key, email="member@example.com", status="active",
        current_period_end="")
    assert client.request("POST", "/ops/send-notices").json()["sent"] == 0


# ---------------------------------------------------------------------------
# Form encoding and signatures, unit level
# ---------------------------------------------------------------------------

def test_form_encoding_matches_stripes_shape():
    """Hand-rolled because dropping the SDK means hand-rolling it, and a wrongly nested key is
    a silent 400 rather than a typing error anyone spots."""
    encoded = stripe_billing.encode_form({
        "mode": "subscription",
        "subscription_data": {"trial_end": 1700000000,
                              "metadata": {"visitor_key": "v1"}},
        "line_items": [{"quantity": 1, "price_data": {"unit_amount": 100}},
                       {"quantity": 2, "price_data": {"unit_amount": 3900}}],
        "cancel_at_period_end": True,
        "resume_flag": False,
        "cleared": None,
    })
    assert "mode=subscription" in encoded
    assert "subscription_data[trial_end]=1700000000" in encoded
    assert "subscription_data[metadata][visitor_key]=v1" in encoded
    assert "line_items[0][price_data][unit_amount]=100" in encoded
    assert "line_items[1][quantity]=2" in encoded
    assert "cancel_at_period_end=true" in encoded, "Python's True is not Stripe's true"
    assert "resume_flag=false" in encoded, "False must be the literal false, not 'False'"
    assert "cleared" not in encoded, "None must be absent, not empty"


def test_signature_verifies_the_timestamp_prefix():
    """The classic rewrite bug: signing the payload without the `timestamp.` prefix passes
    locally and fails against the real thing every time."""
    import hashlib
    import hmac
    body = b'{"id":"evt_1"}'
    stamp = str(int(time.time()))
    wrong = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    with pytest.raises(stripe_billing.StripeError):
        stripe_billing.verify_signature(body, f"t={stamp},v1={wrong}", "whsec")
    right = hmac.new(b"whsec", f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    assert stripe_billing.verify_signature(body, f"t={stamp},v1={right}", "whsec") == "verified"


# ---------------------------------------------------------------------------
# Health page
# ---------------------------------------------------------------------------

def _readiness(client) -> tuple[dict, list[str]]:
    module = client.main
    state = module.billing.status()
    return state, module.billing_readiness(state, module.build_copy(module.settings))


def test_no_readiness_findings_when_correctly_configured(live):
    """A correctly configured, in-use funnel reports nothing.

    `subscribe()` first, which is not decoration: `/health` warns while no subscription has been
    read from the provider in this process, so a fixture that only armed the app would hit a true
    warning and the test would certify an untouched process. The warning has its own test below.
    """
    subscribe(live)
    live.get("/dashboard")
    _state, issues = _readiness(live)
    assert issues == [], issues


def test_a_live_funnel_that_has_never_read_a_subscription_says_so(live):
    """The other half of the pair above, and the reason that one subscribes.

    This assertion is what keeps `test_no_readiness_findings_when_correctly_configured` honest:
    without it, the readiness check could be dropped entirely and both tests would stay green.
    Driven by flipping the process flag the check reads rather than by staging an app that has
    never served a member, because a fresh `live` fixture has already run `subscribe()`-free
    requests and the flag is exactly the state a restart also produces.
    """
    module = live.main
    subscribe(live)
    live.get("/dashboard")
    assert module.SUBSCRIPTION_READ, "a dashboard with a real period end must set the flag"
    monkey = __import__("unittest").mock.patch.object(module, "SUBSCRIPTION_READ", False)
    with monkey:
        _state, issues = _readiness(live)
    assert any("estimate" in issue.lower() for issue in issues), \
        "charges armed with nothing read yet must be named on `/health`"


def test_health_names_an_unset_webhook_secret(live):
    """§2.4 warns that an unset webhook secret means unsigned posts are accepted. Asserted on
    the reasons rather than on markup, so the check cannot drift away from the page."""
    module = live.main
    copy_obj = module.build_copy(module.settings)
    state, _issues = _readiness(live)
    issues = module.billing_readiness(dict(state, webhook_secret_present=False), copy_obj)
    assert issues, "an unset webhook secret in live mode must fail the billing check"
    assert any("unsigned" in issue.lower() or "secret" in issue.lower() for issue in issues)


def test_health_names_charges_armed_without_a_key(live):
    """The worst configuration available: a checkout page that asks for a card and has no key
    to charge it with."""
    module = live.main
    state, _issues = _readiness(live)
    issues = module.billing_readiness(
        dict(state, key_present=False, client_active=False),
        module.build_copy(module.settings))
    # By variable name, not by paraphrase. Someone reading this on `/health` at 11pm has to be
    # able to paste it into the deployment environment without hunting for which of the three
    # funnel keys was meant, and "no funnel secret key is set" does not survive that test.
    assert any("STRIPE_SECRET_KEY_FUNNEL" in issue for issue in issues), issues


def test_rehearsal_health_shows_the_billing_state_without_alarm(client):
    """The ops page has to name the variable an operator has to set.

    Two assertions that look pedantic and are not. The first is the load-bearing one: rehearsal
    prints no alarming banner, and the only funnel-specific string it must still print is the
    secret key's own name, because "will take a card: no" without a variable attached is a status
    nobody can act on. The second is that the readiness section exists at all, so a template edit
    that dropped it fails here rather than quietly turning the page into decoration.
    """
    health = client.get("/health")
    assert health.status_code == 200
    assert "STRIPE_SECRET_KEY_FUNNEL" in health.text
    assert "Billing readiness" in health.text





def test_a_second_offer_after_a_pause_ends_the_pause_everywhere(live):
    """Pause, come back next month, take the discount instead: the member is billing again,
    and the account page must not still say they are paused.

    Each offer's fields describe that offer alone, which is correct in isolation and wrong in
    sequence. The discount path writes a coupon and a tier and never a status, because it is not
    the path that decides whether billing runs, so it left whatever status was there before.
    Pausing wrote `paused`, the second offer told the processor to resume collection, and the row
    stayed `paused`, so the dashboard read "Paused. Nothing is charged until Friday" over a
    subscription that was about to be billed, and the renewal sweep skipped it as not a billing
    status. Two wrong screens off one stale column, and the member who stops watching their card
    stops expecting a charge that is coming.

    The status is therefore read back from the processor rather than named, which is also why this
    test asserts the *verb* went out: a row that only looks right because the fake happened to
    agree with it would not catch a resume that was never sent.
    """
    subscribe(live)
    post(live, "/account/cancel", [])
    post(live, "/account/cancel/offer", [("offer", "pause")])
    key = visitor_of(live)
    paused = live.billing.store.get_subscription(key)
    assert paused["status"] == "paused", "the pause itself did not land, so this proves nothing"
    assert "Paused." in live.get("/dashboard").text

    # Unpausing and un-cancelling are two different calls, and only the pause one is this offer's
    # job: clearing `cancel_at_period_end` on a paused subscription changes nothing at all, since
    # collection is paused independently of whether a cancellation is pending. A funnel that sent
    # that instead bills nobody while promising "Half price for two months", and passes a test
    # written for the whole thing.
    post(live, "/account/cancel/offer", [("offer", "discount")])
    resumption = [(method, path, data) for method, path, data
                  in live.fake().calls_to("/subscriptions/")
                  if "pause_collection" in data]
    assert resumption, "the second offer never touched collection, so the pause still holds"
    assert resumption[-1][2]["pause_collection"] == "", \
        f"collection was not cleared, it was re-configured: {resumption[-1][2]!r}"

    row = live.billing.store.get_subscription(key)
    assert row["status"] in ("trialing", "active"), \
        f"the row still reads {row['status']!r} over a subscription that bills again"
    assert not row["paused_until"], f"a resumed member still shown paused until {row['paused_until']}"
    dashboard = live.get("/dashboard").text
    assert "Paused." not in dashboard, "the dashboard still claims a pause that has ended"
    assert "Next billing date" in dashboard, \
        "the resumed member lost their renewal date rather than getting the real one"
