"""Shared fixture for the funnel tests: a fresh app on a throwaway database.

Lives in `conftest.py` rather than inside one test module because Phase 2 adds a second suite
(`test_billing.py`) that must walk the funnel the same way. Two copies of a 12-answer walk is
how one suite starts passing on answers the other rejects, and the walk's encodings,
cookie-jar handling and redirect policy all have hard-won reasons behind them (see
`test_funnel_flow.py`'s module docstring for the two bugs each rule exists to prevent).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

JAR_KEY = "_jar"
#: Where `attach` remembers the module an app was built from, so the identity check below can ask
#: *this* client rather than guessing from `sys.modules`. See the `client` fixture and `live`.
APP_MODULE_KEY = "_app_module"
#: Every module that has to be rebuilt for a fixture to honour a new environment, and every module
#: the fixtures tear down between apps. Lives here rather than in one fixture because two fixtures
#: and a rebinding loop all need the identical list, and a module added to the app's import tree
#: but named in only one of the three skips its teardown: the next app then inherits the previous
#: one's module-level state and passes a test the configuration never earned.
# Every module whose state a rebuilt app can inherit. A module missing from this list keeps its
# previous import, so a fixture can end up holding a store that belongs to a finished test's
# `tmp_path`: the writes land, the assertions pass, and nothing is being tested. `telemetry` was
# added with Phase 3 for exactly that reason, and `quiz` because `storage` reads variant
# identity through it, so a stale copy reports a stale `test_variable` on every event row.
PHASE2_MODULES = ("main", "config", "storage", "session", "billing", "billing_store",
                  "mailer", "funnel_copy", "stripe_billing", "save_offer", "telemetry", "quiz")
RAISED_KEY = "_raised"
_URLENCODED = "application/x-www-form-urlencoded"


def crash_problem(response) -> str:
    """Something to assert a page has no: an unhandled exception, or an empty body.

    An `expect_status` response is a real 500 carrying starlette's debug traceback, so this
    sniffs the traceback's own fixed opening line rather than guessing at which frame failed.
    Returns "" for a page that rendered, and a one-line description otherwise, which keeps the
    failure message readable instead of dumping forty frames into it.
    """
    if response.status_code < 500:
        return ""
    body = response.text
    if "Traceback (most recent call last)" in body:
        return f"unhandled exception ({response.status_code}): " \
               f"{body.strip().splitlines()[-1][:200]}"
    return f"{response.status_code} with no page: {body[:200]!r}"


def bare_client(app) -> TestClient:
    """A `TestClient` that hands back a real 500, and the exception that caused it.

    Three flags have to move together, and each one is a trap that looks like it worked:

      * `raise_server_exceptions=False` is the documented knob, and on its own it changes only
        the body, to the string `Internal Server Error`. `ServerErrorMiddleware` re-raises after
        building its response either way, so the call still throws and there is nothing to assert
        on. Per-client rather than per-request, so any suite wanting failure responses builds
        through here.
      * `app.debug = True` stops that re-raise, which sounds like the fix and is not: the debug
        response calls `json.dumps` on every local in every frame, and the top frame of most
        failures here holds an `sqlite3.Row`. `app.state.traceback_encoder` is FastAPI's
        documented escape hatch, read through `request.app.state`, which is this same object.
      * `app.middleware_stack = None`. The stack is built once on the first request from
        `app.debug`, and this app has served hundreds by the time a test asks for a failure, so
        without dropping it the flag above is read by nothing.

    All three are undone in `attach`'s request wrapper, which closes the client after the call
    and rebuilds the plain stack. So prefer `crash_of(response)` over reading the page: the page
    is starlette's format, capped at 400 frames, and subject to their reformatting; the exception
    is the app's own type and message, which is what a test should be judging.
    """
    app.debug = True
    app.middleware_stack = None
    client = TestClient(app, raise_server_exceptions=False)
    app.state.traceback_encoder = lambda value: repr(value)
    return client


def restore_plain_stack(app) -> None:
    """Undo `bare_client`'s three switches. See `attach`'s `expect_status` branch."""
    app.debug = False
    app.state.traceback_encoder = None
    app.middleware_stack = None


#: One complete walk of the men's flow, screen by screen (38 screens: "meds" = "no" keeps the
#: medication fact out of the flow). Information screens post `answer=seen`, like any answer.
_MEN_WALK: list[tuple[str, str]] = [
    ("track", "men"), ("age", "30"), ("goal_men", "both"), ("erect_type", "keeping"),
    ("erect_freq", "sometimes"), ("erect_since", "1to5"), ("morning", "sometimes"),
    ("fact_men_erect", "seen"), ("firm_sex", "3"), ("sat", "3"), ("worry", "3"), ("relation", "2"),
    ("confidence_m", "bit"), ("sex_length", "2to7"), ("early", "sometimes"),
    ("control_want", "yes"), ("fact_men_ejac", "seen"), ("dribble", "sometimes"),
    ("urge_m", "rarely"), ("prostate", "no"), ("libido_m", "3"), ("meds", "no"),
    ("milestone", "seen"), ("tried", "unsure"), ("fact_find", "seen"), ("feel", "some"),
    ("strength", "3"), ("hold", "5"), ("fact_relax", "seen"), ("safety", "none"),
    ("alcohol", "weekly"), ("smoke", "no"), ("diet", "3"), ("activity", "3"),
    ("sitting", "4to8"), ("stress", "3"), ("minutes", "5"), ("when", "morning"),
]

ANSWERS: list[list[tuple[str, str]]] = [
    [("safety", value)] if key == "safety" else
    [("answer", value), ("note", "Long commute, two kids")] if key == "when" else
    [("answer", value)]
    for key, value in _MEN_WALK
]


def jar_of(client: TestClient) -> dict[str, str]:
    """Cookies this test conversation is holding, kept by hand.

    `client.cookies` is *not* the transport's jar in this build: it reads empty while the
    transport keeps its own, separate jar, so a hand-set cookie and a real one can both be in
    flight at once. Both directions of that mismatch cost debugging time on the app's behalf,
    so the jar lives in this dict and every request states what it sends.
    """
    return getattr(client, JAR_KEY, {})


def attach(client: TestClient, app_module=None, stripe_module=None) -> TestClient:
    """Wrap a `TestClient` with the jar and the per-request import-identity check.

    `app_module` is the module the app under test was imported from and `stripe_module` is the
    `stripe_billing` the caller is raising from, if the caller has one. Both have to be handed in
    rather than looked up, because the whole point of the check is which copy of `stripe_billing`
    *this* app bound, and there is no reliable global answer: every fixture rebuilds the app, so
    `sys.modules["main"]` names whoever was built last, and one fixture deliberately builds
    against a copy nothing else can reach. Omitted, the check is a no-op.
    """
    setattr(client, JAR_KEY, {})
    if app_module is not None:
        setattr(client.app, APP_MODULE_KEY, app_module)
    #: The module *this* app bound, which is the one that must own `sys.modules` while this app
    #: serves a request. None for apps built without one, and for the Phase 1 apps.
    bound_module = getattr(app_module, "stripe_billing", None) if app_module is not None else None
    plain = client.request

    def request(method, url, **kwargs):
        """Forward by keyword only, and optionally swallow server exceptions for one call.

        `httpx.Client.request(method, url, *, content, data, files, headers, ...)` takes
        nothing useful positionally, so a `*args` passthrough silently binds `data=` to
        `files=`, which switches the encoder to multipart, drops the content type, and turns
        every answer into a phantom.

        `expect_status=N` means "this request is meant to fail inside the app, hand me the
        failure rather than throwing it at the test body". The failure arrives as an
        `AppCrash`, whose `__cause__` is the app's own exception, and `crash_of(response)` reads
        it back. Ordinary requests carry `crash_of() is None`, so the check is the same call on
        both paths. A fresh client per opted-in request is the only way to make a per-client
        flag per-request; the cookie jar rides in the `Cookie` header above rather than in the
        client, so the second client costs nothing the harness relies on.
        """
        # Asked of `client.app`, on which the module is hung in `attach`, rather than of `client`:
        # `expect_status` builds a second, unwrapped client over the *same* app for the failure
        # path, and that request needs the same guarantee as the ordinary one. Per-client would
        # have left the one code path where an uncached exception is the whole point unchecked.
        headers = dict(kwargs.pop("headers", None) or {})
        for name, value in jar_of(client).items():
            headers.setdefault("cookie", f"{name}={value}")
        # POSTs must not follow their own 302: a client that follows it issues a GET to the
        # next step and throws the body away, so every answer looks accepted while none saved.
        # GETs are pinned to the same answer deliberately. `TestClient.get` follows redirects by
        # default and `httpx.Client.get` does not, so leaving the flag unset here meant the two
        # spellings of `client.get("/onboarding")` in the same suite reached different numbers of
        # pages and recorded different funnel steps. Stated once, in one direction, for every verb.
        kwargs["follow_redirects"] = kwargs.get("follow_redirects", False)

        # The module swap that makes the registration true, and it has to be here rather than
        # left to the fixture. Two apps are alive in a run that borrows both billing fixtures,
        # and `sys.modules` can only name one of them, so "the module that raises the app's own
        # errors" is a property that holds for the length of a request and nowhere else. The
        # `live` fixture hands its private copy back to the registration for exactly this much
        # time; whoever's module was there before gets it back the moment the response is in
        # hand, which is why a teardown that builds an app, or a fixture that never bothered
        # with a private copy at all, is served under the module it expects.
        #
        # Checked after the swap rather than before for the same reason: the failure mode this
        # polices is an arrangement undone partway through, by the code under test's own
        # imports, so a guarantee verified before the request starts is already stale by the
        # time it matters. A bare `setattr`/restore rather than `monkeypatch`, because
        # `monkeypatch` finalises after a yield fixture's teardown and would restore over
        # whatever the next fixture installed.
        incumbent = sys.modules.get("stripe_billing")
        swapped = bound_module is not None and incumbent is not bound_module
        if swapped:
            sys.modules["stripe_billing"] = bound_module
        try:
            assert_app_and_suite_agree_on_stripe_classes(client.app, stripe_module)
            if kwargs.pop("expect_status", None):
                quiet = bare_client(client.app)
                try:
                    response = quiet.request(method, url, headers=headers, **kwargs)
                finally:
                    quiet.close()
                    restore_plain_stack(client.app)
            else:
                response = plain(method, url, headers=headers, **kwargs)
        finally:
            if swapped:
                sys.modules["stripe_billing"] = incumbent
        setattr(response, RAISED_KEY, None)
        for raw in response.headers.get_list("set-cookie"):
            pair = raw.split(";", 1)[0]
            if "=" in pair:
                name, _sep, value = pair.partition("=")
                jar_of(client)[name.strip()] = value.strip()
        return response

    client.request = request  # type: ignore[method-assign]
    return client


def get(client: TestClient, path: str, **kwargs):
    """GET with follow-redirects off, stated at the call rather than inherited from httpx.

    GET is where `attach` leaves the flag alone (its POST note below explains why POSTs must not
    follow), and the default is not the same in the two clients this suite drives: the ASGI
    transport behind `TestClient.get` follows redirects, the plain `httpx` request through
    `client.request` does not. Same call, same app, different number of funnel steps reached, and
    telemetry was the thing that noticed, counting one hop as a whole quiz start. Every test that
    wants *this* response uses this helper; a test that means to ride the chain to the end says
    so with `get(client, path, follow=True)`.
    """
    follow = kwargs.pop("follow", False)
    return client.request("GET", path, follow_redirects=follow, **kwargs)


def post(client: TestClient, path: str, pairs: list[tuple[str, str]] | None = None,
         multipart: bool = False, **kwargs):
    """POST a form, in either of the two encodings a browser legally sends.

    Browsers send urlencoded for a plain form and multipart for one containing a file, and a
    funnel that understands only the first is one stray `<input type=file>` away from losing
    every answer. Multipart is built with `files={name: (None, value)}`, httpx's spelling of
    "this part is a field, not a file", which keeps repeated names so checkbox groups survive.
    """
    pairs = pairs or []
    if multipart:
        fields = [(name, (None, value)) for name, value in pairs]
        return client.request("POST", path, files=fields, **kwargs)
    return client.request("POST", path, data=_merge(pairs),
                          headers={"content-type": _URLENCODED}, **kwargs)


def _merge(pairs: list[tuple[str, str]]) -> dict:
    """Repeated names into one dict of lists, which is how a browser submits checkboxes."""
    merged: dict[str, list[str]] = {}
    for key, value in pairs:
        merged.setdefault(key, []).append(value)
    return {key: (values[0] if len(values) == 1 else values) for key, values in merged.items()}


def walk(client: TestClient, upto: int | None = None) -> None:
    for index, pairs in enumerate(ANSWERS if upto is None else ANSWERS[:upto]):
        # Alternate encodings so both paths stay exercised without a second suite.
        response = post(client, f"/onboarding/{index}", pairs, multipart=bool(index % 2))
        assert response.status_code in (200, 302), (index, response.status_code)
    if upto is None:
        # The paywall sits behind the email screen, so a finished walk includes it.
        response = post(client, "/email", [("email", "walker@example.com")])
        assert response.status_code in (200, 302, 303), response.status_code


def assert_app_and_suite_agree_on_stripe_classes(app, our_module=None) -> None:
    """Assert that one app, the code under test, and the classes this test raises are all a single
    copy of `stripe_billing`. Asked of *this app*, because no global answer exists.

    Import identity is not tidiness here; it decides whether an `except` fires. Importing one file
    twice makes two module objects with two sets of classes, and an exception raised from one is
    not an instance of the other, so a provider refusal the fake raises simply does not match the
    handler written to catch it. Nothing fails at import, nothing is logged, and every provider
    failure turns into a 500 that looks like an app bug rather than a harness one. Worse, the
    suite can stay *green* on that: an escaping exception is also a 500, so a test written to
    expect trouble gets trouble for the wrong reason. That is how the outage tests in
    `test_billing.py` passed for a while, on a `TypeError` escaping from inside the app's own
    rescue clause.

    Three bindings must agree, and each fails differently enough to be worth separating:

      * `app_module.stripe_billing`, what the app's `except` clauses name, read off the app object
        the request is about to be sent to (`attach` hangs the module there);
      * `sys.modules["stripe_billing"]`, which is what `stripe_billing.py` itself resolves when it
        constructs its own errors, so a mismatch means the code under test raises one copy and
        catches another;
      * `our_module`, the copy the caller is raising from, which each test declares explicitly
        rather than inheriting from some module-level `import` that has since gone stale.

    Asking this of the *app* rather than of `sys.modules` is the part that matters. Two apps are
    alive at once in a run that borrows both billing fixtures, and one of them is built against a
    copy no one else can see (`private_stripe_billing`), so "which module does the app use" has no
    global answer, only a per-app one. A check that asked globally reported two healthy apps as
    mismatched while hiding a real mismatch. Asked per *client* it was nearly as bad, because the
    `expect_status` path builds an unwrapped second client over the same app and that is exactly
    the request where an uncached exception class decides what the test sees.

    The second check, registration, is necessarily global, so it only holds while a request is in
    flight: the `live` fixture puts its private copy in `sys.modules` before a request and takes it
    out immediately after, so the registration names the module that raised the app's own errors
    for exactly as long as it has to. A fixture that leaves the registration pointing at a copy
    some *other* app bound trips it, and `restore_plain_stack`, which every teardown calls, is what
    catches that: it makes an app answer a plain request under the module its teardown installed.

    A no-op for apps built without a module, so the Phase 1 suites and plain `TestClient` uses are
    untouched. The message names the symptom, because the symptom is what makes this hunt long.
    """
    app = getattr(app, APP_MODULE_KEY, None) if app is not None else None
    if app is None:
        return
    theirs = getattr(app, "stripe_billing", None)
    if theirs is None:
        return
    if our_module is not None and our_module is not theirs:
        raise AssertionError(
            "this test raises stripe_billing classes the app under test cannot catch, so every "
            f"processor failure escapes every rescue. app sees id {id(theirs)}, the test sees id "
            f"{id(our_module)}. Read the module off `client.stripe` (the `live` fixture) or off "
            "the shared module, and never `import stripe_billing` in a test body, which silently "
            "rebinds to whichever copy happens to be loaded.")
    registered = sys.modules.get("stripe_billing")
    if registered is not None and registered is not theirs:
        raise AssertionError(
            "a request is being served while `sys.modules['stripe_billing']` names a module other "
            "than the one the app under test bound, so stripe_billing.py raises its own exception "
            f"classes from one copy while the app catches another: registered id "
            f"{id(registered)}, app's copy id {id(theirs)}. The `live` fixture's arrangement is: "
            "private copy installed in `sys.modules`, `import main` so the tree binds it, "
            "`restore()` before yielding, and the copy reinstalled only around the request itself "
            "(see `private_stripe_billing` and `restore_plain_stack`).")


def shared_stripe_module(app_module=None):
    """Make the whole loaded app tree share one `stripe_billing`, and hand back that module.

    Every fixture that rebuilds the app calls this, and so does any suite that imports a second
    copy of `stripe_billing` for itself. Two apps in one process cannot both be correct about
    which copy of that module is *the* copy, because the copy is chosen by which import ran when,
    and every consumer caches its own binding at import time: `main` does `import stripe_billing`,
    `billing.py` refers to `stripe_billing.StripeError` through its own module global, and a class
    body like `class FakeStripe(stripe_billing.StripeClient)` freezes whatever it resolved at the
    instant its module was imported. Four bindings, all set once, all invisible afterwards.

    What has to be true is narrow, and is what the two asserts below police: for any one app, the
    module *it* bound, the module `sys.modules` names (which is what `stripe_billing.py` resolves
    when it constructs its own errors), and the classes a test raises must all be one object.
    Different apps in the run may legitimately hold different copies, provided each is
    self-consistent while its own client is answering requests.

    With `app_module`, rebind that freshly imported app to the module already registered, so the
    tree agrees with `sys.modules` regardless of who imported what in which order. Without it,
    settle the *other* direction after a private copy was made: point every loaded app module at
    the copy registered right now, and hand it back so the caller can assert it got the one it
    wanted. That is the call `live` makes, because it patches `StripeClient` and must leave the
    rehearsal suites holding an unpatched factory.

    Safe to call at any time: `stripe_billing` reads no configuration at import, and nothing else
    captures anything from it at import besides names.
    """
    registered = sys.modules["stripe_billing"]
    if app_module is None:
        targets = (module for name, module in list(sys.modules.items()) if name in PHASE2_MODULES)
    else:
        targets = [app_module]
    for module in targets:
        if getattr(module, "stripe_billing", None) is not registered:
            module.stripe_billing = registered
    return registered


def private_stripe_billing():
    """Import a `stripe_billing` that belongs to the caller alone, and undo that afterwards.

    Returns `(module, restore)`. Build the app while the module is private and `restore()` before
    the test body runs, and you get the one property a patched module cannot give you: nobody else
    in the process can possibly see the patch.

    The alternative, which this suite did first, was to reload the registered copy, patch it, and
    rely on `monkeypatch` to put the class back. It survives one fixture and breaks on the next,
    because reverting an attribute is not the same as never having patched it: while the patch is
    up, some other fixture can build an app whose `billing` service captured the patched factory,
    and undoing it then leaves that app holding a recorder belonging to a finished test. Nothing
    raises; the wrong client just answers a request several modules away from the cause. A copy
    nobody else can reach cannot be captured by accident.

    Registers the copy under its real name while it is loaded because the module refers to its own
    exception classes through that global name, so a private copy that is not registered raises one
    copy of its errors and catches another. `restore()` puts back whatever `sys.modules` held,
    which is the unpatched module every other app in the run was built against.

    Call it before importing `main`, not after: the point is that the app tree binds this copy.
    """
    # Imported before the swap, and `setdefault` rather than indexing: a fixture that pops the name
    # first leaves nothing to read, and an import registers it if it is missing and returns the
    # incumbent if it is not. That incumbent is what goes back on `restore`, which is what the
    # *next* fixture expects to find.
    importlib.import_module("stripe_billing")
    registered = sys.modules["stripe_billing"]
    sys.modules["stripe_billing"] = importlib.reload(importlib.import_module("stripe_billing"))
    fresh = sys.modules["stripe_billing"]

    # Restored eagerly, by the caller right after `import main`, rather than at test teardown, and
    # that timing is the design: the private copy is needed only while the app tree is being built,
    # and the shorter the window in which the shared module is swapped out, the less that another
    # fixture can be built against it by accident. `pytest` also finalises a yield fixture's
    # teardown after the module-level `monkeypatch`s, so a late restore would race the very thing
    # it is meant to undo.
    def restore() -> None:
        sys.modules["stripe_billing"] = registered

    return fresh, restore


def make_client(env: dict[str, str]) -> TestClient:
    """Build an app under a given environment, with the module cache cleared.

    `main` reads its config at import, so an env dict is only honoured if the previously
    imported modules are dropped first. `monkeypatch` cannot help here because the fixture that
    needs this runs before the test body.
    """
    import os
    from unittest import mock

    for name in PHASE2_MODULES:
        sys.modules.pop(name, None)
    with mock.patch.dict(os.environ, env, clear=False):
        import main
        return attach(TestClient(main.app), main)


@pytest.fixture(autouse=True)
def _no_intro_discount(monkeypatch):
    """The suite prices plans at their full amounts unless a test opts in.

    The paywall's limited-time discount starts on first sight of `/preview`, which every full
    walk reaches, so leaving it on would make every pricing assertion depend on a clock. The
    discount's own tests use `discount_client`, which turns it back on.
    """
    monkeypatch.setenv("FUNNEL_INTRO_DISCOUNT_BPS", "0")


@pytest.fixture()
def discount_client(request, monkeypatch):
    """`client`, with the 35.5% ten-minute discount on."""
    monkeypatch.setenv("FUNNEL_INTRO_DISCOUNT_BPS", "3550")
    monkeypatch.setenv("FUNNEL_INTRO_DISCOUNT_MINUTES", "10")
    return request.getfixturevalue("client")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The funnel as it boots today: charges off, no funnel key, throwaway databases.

    `client.main` is the freshly-imported module. Tests generally drive the app through its
    HTTP surface, and reaching in is for the two things a request cannot express: reading the
    store the app is actually using, and asserting on a machine-readable readiness list rather
    than on the page that renders it. Importing `main` separately for either would build a
    *second* app on a second database, which is how a green test can prove nothing.
    """
    monkeypatch.setenv("FUNNEL_DB_PATH", str(tmp_path / "funnel.sqlite3"))
    monkeypatch.setenv("FUNNEL_BILLING_DB_PATH", str(tmp_path / "billing.sqlite3"))
    monkeypatch.setenv("FUNNEL_SESSION_SECRET", "test-secret-value")
    monkeypatch.setenv("FUNNEL_CHARGES_ENABLED", "false")
    monkeypatch.setenv("FUNNEL_PREVIEW_MODE", "local")
    for name in PHASE2_MODULES:
        if name != "stripe_billing":
            sys.modules.pop(name, None)
    import main
    shared = shared_stripe_module(main)

    # `main` and the module it bound both go to `attach`: the identity check in the request wrapper
    # has to compare against the app *this* client serves, and by the time a request runs,
    # `sys.modules` may belong to a different fixture's app. The module is hung on the client as
    # `client.stripe` for the same reason, so a test names the copy it means instead of trusting a
    # module-level `import` to still be current.
    wrapped = attach(TestClient(main.app), main, shared)
    wrapped.main = main
    wrapped.stripe = shared
    return wrapped

