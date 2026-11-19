"""Signed visitor cookie: HMAC-signed, no server-side session table.

Why a signed cookie rather than a session row: Phase 1 needs continuity across four
pages and nothing else. A session table would need clearing, expiring and an admin
surface, and every one of those is code that can be wrong. The cookie holds an opaque
visitor key plus an HMAC; the answers themselves live in SQLite, keyed by that value.

Identity note for later: `engine.trial_events` uses a `visitor_key`, and lane C's
durable identity is the Stripe event id (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §3.2).
So this cookie is a convenience key, never the dedupe key: it is regenerated if the
signature fails, and no money decision will ever be taken from it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

log = logging.getLogger("funnel.session")

COOKIE_NAME = "rl_vid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_SEPARATOR = "."


class SessionCodec:
    def __init__(self, secret: str) -> None:
        if secret:
            self._secret = secret.encode()
            self.ephemeral = False
        else:
            # Absent secret is tolerated, not fatal: the quiz still works, a restart
            # just costs an in-progress quiz. Loud enough in the log that nobody
            # mistakes a losing cookie for a storage bug.
            self._secret = secrets.token_bytes(32)
            self.ephemeral = True
            log.warning(
                "FUNNEL_SESSION_SECRET is unset, using a random per-process secret: "
                "quiz progress will not survive a restart"
            )

    def issue(self) -> str:
        return secrets.token_urlsafe(12)

    def sign(self, visitor_key: str) -> str:
        mac = hmac.new(self._secret, visitor_key.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{visitor_key}{_SEPARATOR}{mac}"

    def verify(self, token: str | None) -> str | None:
        """Return the visitor key when the signature matches, else None.

        `compare_digest` rather than `==`, so a forged cookie cannot be probed by
        timing, and a tampered cookie falls back to a fresh visitor instead of being
        trusted with a partial match.
        """
        if not token or _SEPARATOR not in token:
            return None
        visitor_key, _, mac = token.rpartition(_SEPARATOR)
        if not visitor_key or len(visitor_key) > 64:
            return None
        expected = hmac.new(self._secret, visitor_key.encode(), hashlib.sha256).hexdigest()[:32]
        return visitor_key if hmac.compare_digest(mac, expected) else None

    def cookie_kwargs(self) -> dict[str, object]:
        """Cookie attributes. `secure` is off because Phase 1 runs on http://localhost;
        the deployment note in `funnel_app/README.md` says to turn it on with TLS."""
        return {
            "max_age": COOKIE_MAX_AGE,
            "httponly": True,
            "samesite": "lax",
            "path": "/",
        }


def renewal_timestamp(seconds_ahead: int) -> int:
    """Monotonic-free epoch marker used for day-level copy. Kept tiny on purpose: the
    real renewal date must come from the processor's subscription, never from a
    locally-stored guess, so this only drives 'about N days' phrasing."""
    return int(time.time()) + seconds_ahead
