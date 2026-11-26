"""Answering without a page load (`static/quiz.js`).

The script sends each answer with an `X-KD-Swap: 1` header and swaps the reply into the page.
These tests pin the server side of that contract: a swap request gets the next screen in the
same reply (or a 204 naming a non-quiz destination), an ordinary form post still gets its
redirect, and in both cases the answer is checked and saved exactly the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

from conftest import _URLENCODED, ANSWERS, _merge                 # noqa: E402

SWAP = {"X-KD-Swap": "1"}


def _post(client, index: int, pairs, headers=None):
    # The same urlencoded body a browser (and quiz.js, via URLSearchParams) sends.
    return client.request("POST", f"/onboarding/{index}", data=_merge(list(pairs)),
                          headers={"content-type": _URLENCODED, **(headers or {})},
                          follow_redirects=False)


def test_a_swap_answer_returns_the_next_screen_in_the_same_reply(client):
    client.get("/")
    reply = _post(client, 0, ANSWERS[0], SWAP)
    assert reply.status_code == 200
    assert reply.headers["X-KD-URL"] == "/onboarding/1"
    assert reply.headers["Cache-Control"] == "no-store"
    assert "How old are you?" in reply.text and 'class="quiz-top"' in reply.text
    # The answer was saved: question 1 now shows it chosen, and question 2 is reachable.
    assert 'value="men"' in client.get("/onboarding/0").text
    assert client.get("/onboarding/1", follow_redirects=False).status_code == 200


def test_an_ordinary_form_post_still_redirects(client):
    client.get("/")
    reply = _post(client, 0, ANSWERS[0])
    assert reply.status_code == 302 and reply.headers["location"] == "/onboarding/1"
    assert "X-KD-URL" not in reply.headers


def test_an_invalid_swap_answer_re_renders_the_same_screen_with_its_message(client):
    client.get("/")
    reply = _post(client, 0, [("answer", "robot")], SWAP)
    assert reply.status_code == 422
    assert "What is your gender?" in reply.text and 'role="alert"' in reply.text
    assert "X-KD-URL" not in reply.headers


def test_the_last_answer_names_the_analysis_screen_instead_of_swapping(client):
    client.get("/")
    for index, pairs in enumerate(ANSWERS[:-1]):
        assert _post(client, index, pairs, SWAP).status_code == 200, index
    last = _post(client, len(ANSWERS) - 1, ANSWERS[-1], SWAP)
    assert last.status_code == 204
    assert last.headers["X-KD-Location"] == "/analysing"
    assert client.get("/analysing").status_code == 200


def test_a_forward_jump_swap_lands_on_the_first_unanswered_screen(client):
    client.get("/")
    _post(client, 0, ANSWERS[0], SWAP)
    reply = _post(client, 5, [("answer", "3")], SWAP)
    assert reply.status_code == 200 and reply.headers["X-KD-URL"] == "/onboarding/1"


def test_quiz_pages_load_the_swap_script_and_keep_a_plain_form(client):
    client.get("/")
    page = client.get("/onboarding/0").text
    assert '<script src="/static/quiz.js?v=' in page
    # Without JavaScript the same form posts to the same route.
    assert 'action="/onboarding/0"' in page and 'method="post"' in page
    assert client.get("/static/quiz.js").status_code == 200
