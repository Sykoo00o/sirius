"""Environment-only configuration for the kegeldoc funnel (a fork of the Lane C Rank-4 funnel).

Rule 2 of CLAUDE.md: no hardcoded secrets. Everything sensitive comes from the
environment, and the defaults below are chosen so that a missing variable means
"do less", never "assume permission": charges default to off, and no LLM key means
the deterministic plan builder serves the preview instead of erroring.

Phase 2 posture, in one sentence: money may now move, but only when a human has set
`FUNNEL_CHARGES_ENABLED=true` *and* put a funnel-account secret key in the environment, and
the save-offer discounts stay parked until their Stripe coupons exist.

Phase 0/1 rules that Phase 2 does not relax:
  * no live ad spend,
  * `SUPERVISOR_ENABLED` untouched,
  * every default still means "do less", never "assume permission".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# The app's OWN .env first, then the repository root's — the reverse order of the original, and the
# order matters more than it looks. `load_dotenv` below takes the FIRST file that exists and does not
# merge, so on any checkout that has a root `.env` (this one does: the engine's, holding LLM and
# Telegram keys, with zero FUNNEL_* lines) the old order found the engine's file, matched no funnel
# variables in it, and stopped. Result measured on the deployed host: `FUNNEL_EMAIL_OUTBOX`,
# `FUNNEL_DB_PATH` and `FUNNEL_BILLING_DB_PATH` were all unset, so the mailer wrote
# `funnel_app/data/outbox/` and telemetry wrote `funnel_app/data/responses.sqlite3` — repo-relative
# paths that a `git clean -fdx` takes with it, which is the exact thing StateDirectory= exists to
# prevent. Invisible in production, because systemd's `EnvironmentFile=` supplies the real values,
# and the app process env was correct throughout; it surfaced from a one-line mail probe run in a
# shell. That is the class of bug: correct under systemd, wrong for every operator who follows the
# README and runs a script by hand, and wrong in the direction of losing state rather than erroring.
#
# A root .env still applies for anything it defines (shared proxy/region settings, say) because
# `load_dotenv` now loads both files, and `setdefault` keeps the funnel's own value as the winner.
ENV_PATHS = (ROOT / ".env", ROOT.parent / ".env")


def load_dotenv() -> None:
    """Load the funnel's .env, then the repo root's, without clobbering real environment variables.

    Process env wins over any file, which is what a container expects: a compose `environment:`
    block must be able to override whatever is parked in .env. Within the files, earlier wins, so
    `funnel_app/.env` beats the root's for any name they share — see ENV_PATHS for why the order is
    load-bearing. The precedence survives being called twice, which plain `setdefault` does not
    manage once two files are in play (see the `_PRISTINE_ENV` note below).

    This loop deliberately does NOT `break` after the first file. It used to, when order alone looked
    harmless; with a root .env present that first-found file was the engine's and contained no
    FUNNEL_* keys, so the funnel booted on every default while reading a config file it assumed was
    its own. Loading both is what makes "earlier wins" a real priority order rather than a lottery
    on which file happens to exist.
    """
    # Precedence, highest first: real process env, then funnel_app/.env, then the repo root .env.
    # Two tiers of "don't overwrite" are needed and they are not the same check:
    #   * a *file* must never beat real process env (a compose `environment:` block, or systemd's
    #     EnvironmentFile=, which IS process env and is the production path);
    #   * an *earlier file* must never be beaten by a later file (funnel's own file outranks root's).
    # `setdefault` alone expresses both on the FIRST call: real process env already occupies the
    # key so no file writes it, and the funnel file writes first so root's line is a no-op. It breaks
    # on the SECOND call (a second `from_env()` in one interpreter — see the tests): names the first
    # call wrote into os.environ from a file now *look* like process env, so a bare `setdefault`
    # still skips them (correct by accident) but any *cleanup-and-reload* (tests, or code that
    # delenvs then re-reads) makes the later root file win, because nothing distinguishes "came from
    # process env" from "we wrote it ourselves last time". `_LOADED_KEYS` records what the files put
    # there, so a re-read reproduces the same precedence instead of a lucky first-call ordering.
    pristine = globals().setdefault("_PRISTINE_ENV", dict(os.environ))
    loaded = globals().setdefault("_LOADED_KEYS", set())
    for path in ENV_PATHS:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if key in pristine:
                continue                 # real process env outranks every file, always
            if key in loaded:
                continue                 # an earlier file already set it; funnel beats root
            os.environ[key] = value
            loaded.add(key)
        # No `break`: both files load so a root .env contributes anything only it defines (shared
        # proxy/region settings), while the earlier funnel file keeps precedence on shared names.
        # Reordering ENV_PATHS without removing this break was the half-fix that read well on a dev
        # laptop and still lost state on the deploy host, where a root .env always exists.


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _int(*names: str, default: int) -> int:
    """Integer that may legitimately be 0, so the empty-string-means-unset rule applies
    here and a stray non-number never crashes the app at import time.

    `FUNNEL_TRIAL_AMOUNT_MINOR=0` is a real offer (a free trial), which is why this cannot
    be `int(os.getenv(name, default) or default)` with the value read as falsy.

    Several names, first non-empty wins. The offer trio is read under both the app's own
    `FUNNEL_`-prefixed spelling and the unprefixed names `LANE_C_PHASE0_STEP1-2.md` writes
    them with, because a `.env` pasted from the spec should price the funnel correctly. The
    prefixed name is listed first, so it wins when both are set: this app's own variable
    beats a bare `TRIAL_AMOUNT_MINOR` that a shared host `export`s for some other service.
    """
    for name in names:
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            # Loud, and defaulting to the conservative value rather than the typed one.
            # A misread price is a disclosure problem, so the fallback is the spec's number.
            return default
    return default


@dataclass(frozen=True)
class FunnelSettings:
    """Everything the funnel needs to know, all of it from the environment."""

    brand_name: str = "kegeldoc"
    #: Public site origin. Feeds canonical URLs and the confirmation page copy, so
    #: a staging deploy cannot accidentally advertise the production domain.
    public_url: str = "http://localhost:8091"
    host: str = "127.0.0.1"
    port: int = 8091

    #: Preview generation. `llm` tries the `fast` tier first and falls back to the
    #: deterministic builder on any failure; `local` never calls a model.
    preview_mode: str = "llm"
    llm_timeout_seconds: int = 25

    #: Phase 2 stays off until flipped deliberately: it needs a funnel-account secret key
    #: present as well. With either missing, checkout records intent and says so.
    charges_enabled: bool = False

    #: Offer terms: the exact Phase 0 baseline, $1 for 7 days then $39/mo. Kept here
    #: rather than in templates so a price change is one edit and stays consistent across
    #: every disclosure slot (badge, checkout card, receipt, policy pages, emails).
    trial_amount_minor: int = 100
    trial_days: int = 7
    rebill_amount_minor: int = 3900
    currency: str = "usd"
    refund_window_days: int = 14

    # ---- save-offer sequence (`LANE_C_PHASE0_STEP1-2.md` §2.3) --------------
    # The three retention options are shown only when the corresponding Stripe object is
    # configured. Spec rule 4: "downgrade must be a real, reachable state"; offering a
    # discount the processor does not apply is that rule broken with extra steps, so an
    # unconfigured option drops off the screen instead of being faked locally.

    #: Option B, the lite tier. Amount only: the coupon/product side is the processor's.
    lite_amount_minor: int = 1900
    #: Option A, pause length. 30 days per the spec's wording.
    pause_days: int = 30
    #: Option B, Stripe coupon id for 50% off for 2 months. Created in the funnel account;
    #: duration is set on the coupon itself ("repeat for 2 months"), not here, so the
    #: screen cannot claim a length the processor does not enforce.
    discount_coupon_id: str = ""
    #: The four options, in spec order. A/B/C are conditional on their config; D never is.
    save_offer_enabled: bool = True

    # ---- lifecycle email ----------------------------------------------------
    #: Empty = write .eml files under `email_outbox_path` and log. Delivery never blocks a
    #: customer-facing action: a cancel that succeeded but could not email still succeeded,
    #: and the dashboard repeats everything the email would have said.
    smtp_url: str = ""
    email_from: str = ""
    #: Where unwritten mail lands when `smtp_url` is empty. Local rehearsal only.
    email_outbox_path: str = "data/outbox"

    #: Days before the period end that the pre-billing notice goes out (spec §2.1: day −2).
    prebilling_notice_days: int = 2
    #: A card decline gets one retry at day +3 and then stops (spec §2.1). Enforced by
    #: recording the intent here; the retry itself is the processor's smart dunning, and
    #: aggressive dunning is what the spec calls ratio poison.
    decline_retry_days: int = 3

    #: SQLite file for quiz responses. Relative paths resolve under `funnel_app/`,
    #: so a default deploy never writes into the repo root by accident.
    database_path: str = "data/responses.sqlite3"

    #: SQLite file for subscription state, notifications and save-offer events.
    billing_database_path: str = "data/billing.sqlite3"

    #: Secret for the signed quiz cookie. Empty means "generate one at boot":
    #: responses still save, but a restart loses a half-finished quiz. It is a
    #: convenience, not a record, so a missing secret is tolerated and logged.
    session_secret: str = ""

    support_email: str = ""

    @property
    def database_abspath(self) -> Path:
        path = Path(self.database_path).expanduser()
        return path if path.is_absolute() else ROOT / path

    @property
    def billing_database_abspath(self) -> Path:
        """Separate file from the quiz responses, on purpose.

        Responses are append-only experiment data; subscriptions are billing state that the
        cancel button writes synchronously. Sharing one file means a slow analytics read can
        sit in front of a cancellation, and a cancellation is the one write here that a
        customer is waiting on.
        """
        path = Path(self.billing_database_path).expanduser()
        return path if path.is_absolute() else ROOT / path

    @property
    def email_outbox_abspath(self) -> Path:
        path = Path(self.email_outbox_path).expanduser()
        return path if path.is_absolute() else ROOT / path

    @classmethod
    def from_env(cls) -> "FunnelSettings":
        load_dotenv()
        return cls(
            brand_name=os.getenv("FUNNEL_BRAND_NAME", cls.brand_name).strip() or cls.brand_name,
            public_url=os.getenv("FUNNEL_PUBLIC_URL", cls.public_url).rstrip("/"),
            host=os.getenv("FUNNEL_HOST", cls.host),
            port=_int("FUNNEL_PORT", default=cls.port),
            preview_mode=os.getenv("FUNNEL_PREVIEW_MODE", cls.preview_mode).strip().lower(),
            llm_timeout_seconds=_int("FUNNEL_LLM_TIMEOUT_SECONDS", default=25),
            charges_enabled=_flag("FUNNEL_CHARGES_ENABLED"),
            trial_amount_minor=_int("FUNNEL_TRIAL_AMOUNT_MINOR", "TRIAL_AMOUNT_MINOR",
                                    default=cls.trial_amount_minor),
            trial_days=_int("FUNNEL_TRIAL_DAYS", "TRIAL_DAYS", default=cls.trial_days),
            rebill_amount_minor=_int("FUNNEL_REBILL_AMOUNT_MINOR", "REBILL_AMOUNT_MINOR",
                                     default=cls.rebill_amount_minor),
            currency=os.getenv("FUNNEL_CURRENCY", "usd").lower(),
            refund_window_days=_int("FUNNEL_REFUND_WINDOW_DAYS",
                                    default=cls.refund_window_days),
            lite_amount_minor=_int("FUNNEL_LITE_AMOUNT_MINOR", default=cls.lite_amount_minor),
            pause_days=_int("FUNNEL_PAUSE_DAYS", default=cls.pause_days),
            discount_coupon_id=os.getenv("FUNNEL_DISCOUNT_COUPON_ID", "").strip(),
            save_offer_enabled=_flag("FUNNEL_SAVE_OFFER_ENABLED", "true"),
            smtp_url=os.getenv("FUNNEL_SMTP_URL", "").strip(),
            email_from=os.getenv("FUNNEL_EMAIL_FROM", "").strip(),
            email_outbox_path=os.getenv("FUNNEL_EMAIL_OUTBOX", cls.email_outbox_path),
            prebilling_notice_days=_int("FUNNEL_PREBILLING_NOTICE_DAYS",
                                        default=cls.prebilling_notice_days),
            decline_retry_days=_int("FUNNEL_DECLINE_RETRY_DAYS", default=cls.decline_retry_days),
            database_path=os.getenv("FUNNEL_DB_PATH", cls.database_path),
            billing_database_path=os.getenv("FUNNEL_BILLING_DB_PATH", cls.billing_database_path),
            session_secret=os.getenv("FUNNEL_SESSION_SECRET", ""),
            support_email=os.getenv("FUNNEL_SUPPORT_EMAIL", ""),
        )
