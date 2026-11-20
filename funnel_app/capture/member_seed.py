#!/usr/bin/env python3
"""Seed a paid subscriber into a running funnel, so a browser can visit the member screens.

Writes two files a machine can read and git never sees: `/tmp/member/visitor` (the visitor key)
and `/tmp/member/secret` (the session secret it was issued under). The companion `member_walk.js`
signs the cookie itself, which keeps the secret out of a JS file that gets committed.

Reads the same environment the app was started with, so run it in the same shell:

    FUNNEL_DB_PATH=/tmp/member/f.sqlite3 FUNNEL_BILLING_DB_PATH=/tmp/member/b.sqlite3 \\
    FUNNEL_SESSION_SECRET=... ../backend/.venv/bin/python capture/member_seed.py

It walks the real quiz and the real checkout path rather than inserting a row by hand, so the
answers blob is what a member's export would read and the plan the dashboard unlocks is the one
those twelve answers produce.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

import main  # noqa: E402

ANSWERS = [
    [("answer", "energy")], [("answer", "comeback")], [("answer", "3")],
    [("answer", "30")], [("answer", "evening")], [("answer", "social")],
    [("answer", "plant")], [("restrictions", "dairy"), ("restrictions", "nuts")],
    [("equipment", "dumbbells"), ("equipment", "bodyweight")],
    [("answer", "minimal")], [("answer", "under6")],
    [("answer", "time"), ("note", "Two kids and a 40-hour week")],
]

STATE = Path("/tmp/member")


def main_run() -> int:
    client = TestClient(main.app)
    client.get("/")
    for index, pairs in enumerate(ANSWERS):
        response = client.post(f"/onboarding/{index}", data=dict(pairs))
        if response.status_code != 200:
            print(f"quiz step {index} returned {response.status_code}", file=sys.stderr)
            return 1
    token = client.cookies.get("rl_vid", "")
    visitor = token.rsplit(".", 1)[0] if "." in token else ""
    if not visitor:
        print("no rl_vid cookie: the app did not mint a visitor", file=sys.stderr)
        return 1
    # `checkout_complete` with an empty session id is the rehearsal recording path: it writes the
    # row, the answers snapshot and sends the trial receipt, and asks nothing of a processor.
    main.billing.checkout_complete(
        visitor_key=visitor, session_id="", email="member@example.com",
        answers={str(i): (dict(pairs) if len(pairs) > 1 else pairs[0][1])
                 for i, pairs in enumerate(ANSWERS)},
        coach_note="Lead with the short sessions.", plan_signature="capture")

    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "visitor").write_text(visitor)
    (STATE / "secret").write_text(main.settings.session_secret or "")
    print(f"seeded {visitor} at {main.settings.billing_database_path}")
    print(f"  dashboard: {main.settings.public_url}/dashboard")
    print(f"  next: node capture/member_walk.js capture/member 390")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_run())
