"""After the last answer: analysing, welcome, compatibility, email, ready, then the paywall.

The order is the product decision; what these tests guard is that every screen is reachable only
in order, that the email gate holds, that the paywall sells exactly three paid plans with their
own terms on screen, and that the chosen plan is the plan the checkout prices.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

from conftest import ANSWERS, post, walk                        # noqa: E402

import plans                                                     # noqa: E402
from config import FunnelSettings                                # noqa: E402
from funnel_copy import build_copy, money                        # noqa: E402


def _quiz_only(client):
    """The quiz without the email step, which `walk` adds at the end."""
    client.get("/")
    for index, pairs in enumerate(ANSWERS):
        last = post(client, f"/onboarding/{index}", pairs)
    return last


def test_the_last_answer_lands_on_the_analysis_screen(client):
    last = _quiz_only(client)
    assert last.status_code == 302 and last.headers["location"] == "/analysing"
    page = client.get("/analysing")
    assert page.status_code == 200
    assert "Analysing your answers" in page.text
    assert page.text.count('role="progressbar"') >= 4
    assert 'href="/welcome"' in page.text, "the no-JS path needs a way on"


@pytest.mark.parametrize("path", ["/analysing", "/welcome", "/compatibility", "/email", "/ready"])
def test_every_post_quiz_screen_needs_a_finished_quiz(client, path):
    client.get("/")
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 302 and "/onboarding" in response.headers["location"]


def test_welcome_names_the_track_the_visitor_chose(client):
    _quiz_only(client)
    text = client.get("/welcome").text
    assert "millions of men prioritizing sexual wellness" in text
    assert "women" not in text.split("<main", 1)[1].split("</main>", 1)[0]


def test_compatibility_is_high_for_a_plain_profile(client):
    _quiz_only(client)
    text = client.get("/compatibility").text
    assert "<b>Excellent</b>" in text and 'href="/email"' in text


def test_compatibility_drops_when_an_answer_needs_a_clinician():
    import main
    from plan_builder import Profile, build_plan
    assert main.compatibility(build_plan(Profile(safety=frozenset({"pain"}))))["level"] == "Great"
    assert main.compatibility(build_plan(Profile()))["level"] == "Excellent"


def test_the_plan_and_paywall_wait_for_an_email(client):
    _quiz_only(client)
    for path in ("/ready", "/preview"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302 and response.headers["location"] == "/email", path


def test_a_bad_email_is_refused_on_the_same_screen(client):
    _quiz_only(client)
    response = post(client, "/email", [("email", "not-an-email")])
    assert response.status_code == 422 and "working email" in response.text


def test_email_leads_to_ready_with_a_get_my_plan_button(client):
    _quiz_only(client)
    saved = post(client, "/email", [("email", "walker@example.com")])
    assert saved.status_code == 303 and saved.headers["location"] == "/ready"
    ready = client.get("/ready").text
    assert "Your personal kegel plan is ready" in ready
    assert "walker@example.com" in ready
    assert 'href="/preview">Get my plan' in ready


def test_paywall_offers_exactly_three_paid_plans_and_no_free_one(client):
    client.get("/")
    walk(client)
    text = client.get("/preview").text
    settings = FunnelSettings.from_env()
    values = [part.split('"', 1)[0] for part in text.split('name="plan" value="')[1:]]
    assert values == ["1w", "4w", "12w"]
    assert 'value="4w" checked' in text, "the 4-week plan is the preselected one"
    for option in plans.options(settings):
        terms = build_copy(plans.settings_for(settings, option))
        assert terms.disclosure("price_line") in text, option.key
        assert money(option.amount_minor, settings.currency) in text
    assert "free" not in text.lower().replace("free text", ""), "no free plan, no free wording"


def test_the_chosen_plan_is_the_plan_the_checkout_prices(client):
    client.get("/")
    walk(client)
    settings = FunnelSettings.from_env()
    twelve = plans.find(settings, "12w")
    checkout = client.get("/checkout?plan=12w")
    assert checkout.status_code == 200
    form = checkout.text[checkout.text.find("<form"):]
    assert money(twelve.amount_minor, settings.currency) in form
    assert 'name="plan" value="12w"' in form
    assert "84 days" in form
    done = post(client, "/checkout", [("plan", "12w")])
    assert money(twelve.amount_minor, settings.currency) in done.text


def test_an_unknown_plan_goes_back_to_the_paywall(client):
    client.get("/")
    walk(client)
    response = client.get("/checkout?plan=free", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/preview"


def test_plan_table_is_sound():
    settings = FunnelSettings()
    assert plans.audit(settings) == []
    for option in plans.options(settings):
        assert plans.for_amount(settings, option.amount_minor) == option
    assert plans.audit(FunnelSettings(plan_4w_amount_minor=100)) != []
