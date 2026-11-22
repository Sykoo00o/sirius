"""The paywall's limited-time discount, and the charts on the study screens.

The discount is only honest if it behaves like a real deadline: the clock starts once, on the
server, a reload never restarts it, the discounted price is what gets charged while it runs, and
the full price is what the page shows and charges after it ends. Every test here guards one of
those, plus the rule that the amount charged is always the amount last displayed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

from conftest import post, walk                                  # noqa: E402

import claims                                                    # noqa: E402
import plans                                                     # noqa: E402
from config import FunnelSettings                                # noqa: E402
from funnel_copy import money                                    # noqa: E402
from quiz import CHARTS, QUESTIONS                               # noqa: E402


def _clock(client, monkeypatch, start: float):
    """Freeze the app's clock at `start`; returns a setter to move it."""
    now = {"t": start}
    monkeypatch.setattr(client.main, "_now", lambda: now["t"])
    return lambda seconds: now.__setitem__("t", start + seconds)


def _paywall(client, monkeypatch):
    move = _clock(client, monkeypatch, 1_800_000_000.0)
    client.get("/")
    walk(client)
    return move, client.get("/preview")


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------

def test_the_discount_is_35_5_percent_off_each_first_charge_and_never_the_rebill():
    settings = FunnelSettings(trial_amount_minor=299, rebill_amount_minor=2499,
                              plan_4w_amount_minor=999, plan_12w_amount_minor=1999,
                              intro_discount_bps=3550)
    cut = {o.key: (o.amount_minor, o.full_minor) for o in plans.options(settings, discounted=True)}
    assert cut == {"1w": (193, 299), "4w": (644, 999), "12w": (1289, 1999)}
    assert plans.discount_percent(settings) == "35.5"
    assert plans.audit(settings) == []
    # A subscription sold at a discounted amount still maps back to its plan.
    assert plans.for_amount(settings, 644).key == "4w"
    discounted = plans.find(settings, "4w", discounted=True)
    assert plans.settings_for(settings, discounted).rebill_amount_minor == 2499


def test_zero_turns_the_discount_off():
    settings = FunnelSettings(intro_discount_bps=0)
    assert not plans.discount_enabled(settings)
    assert plans.options(settings, discounted=True) == plans.options(settings)
    assert not plans.offer_active(settings, 0.0, 1.0)


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------

def test_the_paywall_shows_a_ten_minute_countdown_and_struck_through_prices(discount_client,
                                                                          monkeypatch):
    _, page = _paywall(discount_client, monkeypatch)
    assert page.status_code == 200
    html = page.text
    assert 'class="offer-bar"' in html and "35.5% off" in html
    assert 'data-remaining="600"' in html and "10:00" in html
    settings = discount_client.main.settings
    for option in plans.options(settings, discounted=True):
        assert f'<s class="po-was">{money(option.full_minor, settings.currency)}</s>' in html
        assert money(option.amount_minor, settings.currency) in html


def test_a_reload_carries_on_from_the_same_deadline(discount_client, monkeypatch):
    move, _ = _paywall(discount_client, monkeypatch)
    move(4 * 60 + 30)
    again = discount_client.get("/preview").text
    assert 'data-remaining="330"' in again and "05:30" in again


def test_after_ten_minutes_the_page_shows_and_checkout_uses_the_full_price(discount_client,
                                                                          monkeypatch):
    move, _ = _paywall(discount_client, monkeypatch)
    move(10 * 60 + 1)
    page = discount_client.get("/preview").text
    assert 'class="offer-bar"' not in page and "po-was" not in page
    settings = discount_client.main.settings
    full_4w = plans.find(settings, "4w")
    checkout = discount_client.get("/checkout?plan=4w").text
    assert f'name="amount" value="{full_4w.amount_minor}"' in checkout
    assert 'class="offer-bar"' not in checkout


def test_checkout_while_the_clock_runs_prices_the_discount(discount_client, monkeypatch):
    move, _ = _paywall(discount_client, monkeypatch)
    move(60)
    settings = discount_client.main.settings
    cut = plans.find(settings, "12w", discounted=True)
    checkout = discount_client.get("/checkout?plan=12w").text
    assert f'name="amount" value="{cut.amount_minor}"' in checkout
    assert money(cut.amount_minor, settings.currency) in checkout
    assert "<s>" + money(cut.full_minor, settings.currency) + "</s>" in checkout
    done = post(discount_client, "/checkout", [("plan", "12w"), ("amount", str(cut.amount_minor))])
    assert done.status_code == 200 and money(cut.amount_minor, settings.currency) in done.text


def test_a_form_opened_in_time_is_honoured_for_a_short_grace(discount_client, monkeypatch):
    move, _ = _paywall(discount_client, monkeypatch)
    cut = plans.find(discount_client.main.settings, "4w", discounted=True)
    move(10 * 60 + 60)  # one minute past the deadline, inside the two-minute grace
    done = post(discount_client, "/checkout", [("plan", "4w"), ("amount", str(cut.amount_minor))])
    assert done.status_code == 200, done.headers.get("location")


def test_a_stale_discounted_form_is_never_charged_at_a_price_it_did_not_show(discount_client,
                                                                            monkeypatch):
    move, _ = _paywall(discount_client, monkeypatch)
    cut = plans.find(discount_client.main.settings, "4w", discounted=True)
    move(10 * 60 + 300)
    late = post(discount_client, "/checkout", [("plan", "4w"), ("amount", str(cut.amount_minor))])
    assert late.status_code == 303 and late.headers["location"] == "/checkout?plan=4w&changed=1"
    page = discount_client.get(late.headers["location"]).text
    assert "discount ended before you paid" in page and "Nothing has been charged" in page


def test_a_rehearsal_submit_does_not_restart_the_clock(discount_client, monkeypatch):
    move, _ = _paywall(discount_client, monkeypatch)
    move(200)
    cut = plans.find(discount_client.main.settings, "1w", discounted=True)
    post(discount_client, "/checkout", [("plan", "1w"), ("amount", str(cut.amount_minor))])
    move(300)
    page = discount_client.get("/preview")
    assert page.status_code == 200, "the email was lost with the rehearsal row"
    assert 'data-remaining="300"' in page.text


# ---------------------------------------------------------------------------
# Study charts
# ---------------------------------------------------------------------------

def test_every_study_screen_has_a_chart_and_a_source():
    charted = [q for q in QUESTIONS if q.chart]
    assert {q.chart for q in charted} == set(CHARTS)
    for question in charted:
        assert question.source, question.key


def test_chart_text_passes_the_claims_scan():
    for key, chart in CHARTS.items():
        text = " ".join([chart.title, chart.note, *(b.label for b in chart.bars),
                         *(b.group for b in chart.bars)])
        assert not claims.violations(text), (key, claims.violations(text))


def test_charts_carry_the_published_figures():
    assert [b.value for b in CHARTS["dorey"].bars] == [40.0, 34.5, 25.5]
    assert [b.value for b in CHARTS["pastore"].bars] == [31.7, 146.2]
    assert [b.value for b in CHARTS["cochrane"].bars] == [56, 6, 74, 11]
    for chart in CHARTS.values():
        assert all(0 <= b.value <= chart.scale_max for b in chart.bars)


def test_a_study_screen_renders_its_chart(client):
    from conftest import ANSWERS
    client.get("/")
    for index, pairs in enumerate(ANSWERS):
        page = client.get(f"/onboarding/{index}")
        if 'class="chart"' in page.text:
            assert "chart-row" in page.text and "chart-note" in page.text
            return
        post(client, f"/onboarding/{index}", pairs)
    raise AssertionError("no study screen rendered a chart on the walk")
