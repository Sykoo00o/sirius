"""Digital arbitrage worker (Hustle B): source, price, and log resale opportunities.

What this file does, and only this
----------------------------------
Scrape target listings (event ticket drops, software bundle deals, expired
domains), compute the expected margin between the acquisition cost and a
secondary-market floor price, and log the discoveries. Three verbs: scrape,
calculate, log. Specifically NOT done here:

* Buying. The spec's "instant checkout bot" is a separate process. Discovery is
  cheap and buying is not, so this worker never calls the spend gate, which is
  why it runs happily with FINANCE_AGENT_URL unset.
* Validation. The spec's validation gate (secondary-market liquidity, listing
  restrictions, resale velocity) needs data a scraper cannot see. This file
  subtracts *known* costs from an observed floor price and keeps every stored
  number labelled expected, never realized.
* Relisting. That belongs to whatever holds inventory.

Where the discoveries are logged
--------------------------------
The architecture doc names an `arbitrage_targets` table; schema.sql never
installed one, and the schema.sql workers' pattern (see funnel_worker.py) is to
own their tables via idempotent DDL. So this worker creates and owns
`engine.arbitrage_targets`: one row per (source, listing identity), an
append-mostly status column, and the full cost breakdown stored as JSONB next to
the decision columns so the arithmetic is auditable per row rather than
reconstructable only from log prose.

It also feeds the columns hustle_metrics reserves for this hustle, per the
schema's own comment ("how many sourced deals converted, and what the realized
margin was against what validation predicted"):

    targets_discovered   = opportunities logged (additive flow, per tick)
    expected_margin_pct  = mean expected margin of the day's opportunities (stock)

The gauge is recomputed from arbitrage_targets every tick rather than
accumulated: an average cannot be summed, and this table holds the per-row
margins, so recomputation is idempotent across restarts and double ticks.

Playwright and httpx, split by what the endpoint is
---------------------------------------------------
Bundle feeds (deal pages, domain auction indexes) are ordinary HTTP and get
httpx: cheaper, faster, no browser. Ticket drops (Ticketmaster/AXS-class pages)
render client-side and bot-check plain fetches, so they get headless Chromium.
The browser launches once per process and all browser-routed pages in one tick
share one context (one fingerprint per tick, one launch); it is created lazily
on the first browser source and closed in the shutdown path.

Blocking and the loop
---------------------
Playwright sync, psycopg, and httpx are all synchronous. This worker therefore
does NOT subclass common.BaseWorker, whose tick loop is asyncio-native: on
Windows the Playwright sync API refuses to start inside a running event loop at
all, and on every platform an asyncio loop pins every blocking scrape to a
thread hop anyway. Instead this file owns a plain-thread loop with the same
shape (freshness cut, identity dedupe via WorkerStore.seen/mark_seen, error
streaks, agent_logs heartbeats) and reuses everything in common.py that is
orthogonal to the loop. The main thread only ever sleeps on a stop Event, which
is exactly where a SIGTERM lands.

Env (all read via common.load_dotenv; no secrets hardcoded)
-----------------------------------------------------------
ARBITRAGE_TARGETS_JSON      REQUIRED. JSON array of source configs:
    [{"name": "axs-drop", "route": "browser", "url": "https://example.com/shows",
      "listing_selector": "div.event-card", "title_selector": "h3",
      "price_selector": ".price", "link_selector": "a",
      "sell_url_template": "https://secondary/q?q={query}",
      "max_acquire_minor": 15000, "query": "concert", "requires_login": false}]
  Unique `name` drives every per-source cursor and dedupe key. `route` is
  "http" or "browser". Selectors are CSS (optional; sane defaults try the
  common microdata/JSON-LD shapes). `requires_login` rows are skipped with a
  warning rather than scraped anonymously into a phantom zero-price universe.
ARBITRAGE_COMP_SOURCES_JSON  Optional, ordered floor-price comparators; first
  numeric answer wins:
    [{"type": "http_json", "name": "floor-api",
      "url": "https://api.example.com/floor?slug={slug}", "path": "data.floor"},
     {"type": "static_minor", "name": "manual-floor", "value_minor": 20000}]
  http_json GETs `url` ({slug}/{query}/{source} filled from the listing), walks
  `path` ("data.floor"), and takes the first number found. static_minor is a
  documented manual floor for staging. Unset means nothing clears the margin
  bar and the tick says so loudly once per process.
ARBITRAGE_MIN_EXPECTED_MARGIN_PCT  Default 15.0. Below it a candidate is logged
  at debug only and does not reach arbitrage_targets: a 4% spread is fee noise,
  and storing noise as opportunity trains people to ignore the table.
ARBITRAGE_MAX_ACQUIRE_MINOR        Default 50000. Acquisition ceiling; the
  spec's card envelope for this worker is small, so a four-figure face is out
  of this worker's world whatever its margin. Per-source max_acquire_minor
  narrows it further.
ARBITRAGE_LISTINGS_CAP             Default 50 per source per tick, 500 max.
ARBITRAGE_FRESHNESS_HOURS          Default 6.0; a listing older than this is a
  different market already (same premise as DEFAULT_SPEND_FRESHNESS_HOURS).
  Feeds without timestamps pass one; the cutoff is discovery time in that case.
ARBITRAGE_SCRAPE_TIMEOUT_MS        Default 25000 per goto/response.
ARBITRAGE_PLATFORM_FEE_PCT / ARBITRAGE_PER_ITEM_FEE_MINOR / ARBITRAGE_SHIPPING_MINOR
  Cost stack subtracted from the floor before the spread. Defaults 15.0 / 0 / 0:
  digital goods ship free; shipping is nonzero only for physical-adjacent
  listings the source list might grow to include.
ARBITRAGE_CURRENCY                 Default USD, used where neither the feed nor
  the config names one.
ARBITRAGE_TICK_SECONDS / WORKER_TICK_SECONDS   Default 300.
WORKER_SCHEMA   Installed schema, validated by common.py.

Run:  python -m workers.digital_arbitrage_worker [--once] [--dry-run]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

try:
    from .common import (
        WorkerStore,
        _float_env,
        _int_env,
        build_database_url,
        env,
        fmt_minor,
        load_dotenv,
    )
except ImportError:  # pragma: no cover - `python digital_arbitrage_worker.py` from this dir
    from common import (  # type: ignore[no-redef]
        WorkerStore,
        _float_env,
        _int_env,
        build_database_url,
        env,
        fmt_minor,
        load_dotenv,
    )

log = logging.getLogger("workers.arbitrage")

#: Consecutive failing ticks before errors are reported at 'error' level.
#: Same threshold and reasoning as common.ERRORS_BEFORE_UNWELL.
ERRORS_BEFORE_UNWELL = 3

DEFAULT_TICK_SECONDS = 300
DEFAULT_LISTINGS_CAP = 50
MAX_LISTINGS_CAP = 500
DEFAULT_MIN_MARGIN_PCT = 15.0
DEFAULT_MAX_ACQUIRE_MINOR = 50_000
DEFAULT_FRESHNESS_HOURS = 6.0
DEFAULT_SCRAPE_TIMEOUT_MS = 25_000
#: Marketplace take-rate assumptions. A percentage plus a flat per-item fee is
#: the actual shape of every resale platform's fee schedule.
DEFAULT_PLATFORM_FEE_PCT = 15.0
DEFAULT_PER_ITEM_FEE_MINOR = 0
DEFAULT_SHIPPING_MINOR = 0

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")

_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class ConfigError(RuntimeError):
    """A config mistake, not a transient failure. Retrying the tick cannot fix it."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Listing:
    """One candidate as scraped: identity, price, and provenance. No opinions."""

    source: str
    identity: str
    title: str
    acquire_minor: int
    currency: str
    url: str | None
    sell_url: str | None
    observed_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class Opportunity:
    """A listing plus the margin arithmetic that made it worth logging."""

    listing: Listing
    floor_minor: int
    fee_minor: int
    per_item_fee_minor: int
    shipping_minor: int
    net_proceeds_minor: int
    expected_profit_minor: int
    expected_margin_pct: float
    comp_source: str

    @property
    def identity_hash(self) -> str:
        return identity_hash_for(self.listing.source, self.listing.identity)

    def to_row(self) -> dict[str, Any]:
        lst = self.listing
        return {
            "source": lst.source,
            "identity": lst.identity,
            "identity_hash": self.identity_hash,
            "title": lst.title[:500],
            "source_url": lst.url,
            "sell_url": lst.sell_url,
            "acquire_cost_minor": lst.acquire_minor,
            "comp_floor_minor": self.floor_minor,
            "fee_minor": self.fee_minor,
            "currency": lst.currency,
            "expected_profit_minor": self.expected_profit_minor,
            "expected_margin_pct": round(self.expected_margin_pct, 2),
            "calc": {
                "platform_fee_pct": _float_env("ARBITRAGE_PLATFORM_FEE_PCT",
                                               DEFAULT_PLATFORM_FEE_PCT),
                "per_item_fee_minor": self.per_item_fee_minor,
                "shipping_minor": self.shipping_minor,
                "net_proceeds_minor": self.net_proceeds_minor,
                "formula": ("expected_profit = floor - platform_fee(floor) "
                            "- per_item_fee - shipping - acquire_cost; "
                            "margin_pct = expected_profit / acquire_cost * 100"),
                "comp_source": self.comp_source,
                "observed_at": lst.observed_at.isoformat(),
                **lst.payload,
            },
        }


def identity_hash_for(source: str, identity: str) -> str:
    """Same construction as Lead.identity_hash(), so keys agree across sources."""
    import hashlib

    return hashlib.sha256(f"{source}:{identity}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(r"\d[\d,.]*")


def parse_price_minor(raw: str) -> int | None:
    """Extract minor units from free-form price text: '$1,299.00' -> 129900.

    Deliberately dumb about separators: keep digits and the last separator, and
    decide by its position whether it was a thousands comma or a decimal point
    ('1.299,00' is European for 1299.00, '1,299.00' is not 1.29900). If the
    price cannot be read, None means "no price", never 0: a 0-price ticket logs
    an infinite margin and buys the wrong thing at full speed.
    """
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.,]", "", raw)
    if not cleaned:
        return None
    digits = re.sub(r"[.,]", "", cleaned)
    if not digits:
        return None
    majors = [i for i, ch in enumerate(cleaned) if ch in ".,"]
    frac = ""
    if majors:
        last = majors[-1]
        tail = cleaned[last + 1:]
        # Exactly three trailing digits is ambiguous ('1.299'): every feed we
        # actually scrape uses two-digit money, so three is treated as a
        # thousands group, matching the European reading above.
        if re.fullmatch(r"\d{1,2}", tail):
            frac = tail
            digits = re.sub(r"[.,]", "", cleaned[:last])
    minor = int(digits) * 100 + int((frac + "00")[:2])
    return minor if minor > 0 else None


def parse_money_fields(item: dict[str, Any]) -> tuple[int, str] | None:
    """(acquire_minor, currency) from a scraped dict like {"price": "$49", ...}."""
    currency = str(item.get("currency") or "").strip().upper()
    if currency and (len(currency) != 3 or not currency.isalpha()):
        currency = ""
    raw = item.get("acquire_minor")
    if raw is not None:
        try:
            minor = int(float(raw))
        except (TypeError, ValueError):
            return None
        return (minor, currency or env("ARBITRAGE_CURRENCY", "USD")) if minor > 0 else None
    for key in ("price", "salePrice", "sale_price", "currentPrice", "buyNow", "cost"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            # round() on a float already yields int; the outer cast was decorative.
            minor = round(float(value) * 100)
        elif isinstance(value, str):
            minor = parse_price_minor(value) or 0
        else:
            continue
        if minor > 0:
            return minor, currency or env("ARBITRAGE_CURRENCY", "USD")
    return None


def extract_jsonld(html_text: str) -> list[dict[str, Any]]:
    """All JSON-LD objects in a page, flattened past @graph wrappers."""
    found: list[dict[str, Any]] = []

    def harvest(node: Any) -> None:
        if isinstance(node, dict):
            if "@graph" in node:
                harvest(node["@graph"])
            elif node.get("@type"):
                found.append(node)
        elif isinstance(node, list):
            for child in node:
                harvest(child)

    for match in re.finditer(
            r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            html_text, re.DOTALL | re.IGNORECASE):
        try:
            harvest(json.loads(match.group(1)))
        except (json.JSONDecodeError, RecursionError):
            continue  # one malformed block must not drop the page's other blocks
    return found


def offers_of(jsonld: dict[str, Any]) -> list[dict[str, Any]]:
    raw = jsonld.get("offers")
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [o for o in raw if isinstance(o, dict)]
    return []


def normalized_title(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def canonical_identity(url: str | None, title: str, source: str) -> str:
    """Stable id for one listing. URL minus tracking params, else a title hash.

    Ticketmaster-style URLs carry city/occasion ids that survive re-publishing
    the same event, while UTM params rotate per crawl; either one drifting turns
    `seen()` into amnesia and re-logs the same deal as new opportunity every
    tick. A title-only identity (bundle feeds rarely expose stable per-item
    urls) hashes the normalized title so casing changes do not mint a new deal.
    """
    if url:
        parts = urlsplit(url)
        if parts.netloc and parts.path and parts.path != "/":
            return f"{parts.netloc.lower()}{parts.path.rstrip('/').lower()}"
    digest = identity_hash_for(source, normalized_title(title).lower())
    return f"title:{digest}"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class DigitalArbitrageWorker:
    """Scrape -> margin -> log, on a plain-thread tick loop."""

    HANDLE = "worker:digital_arbitrage"
    HUSTLE = "arbitrage"
    JOB = "digital_arbitrage_scrape"

    def __init__(self, database_url: str, *, tick_seconds: int = DEFAULT_TICK_SECONDS,
                 batch_size: int = DEFAULT_LISTINGS_CAP,
                 freshness_hours: float = DEFAULT_FRESHNESS_HOURS,
                 dry_run: bool = False) -> None:
        self.store = WorkerStore(database_url)
        self.database_url = database_url
        self.tick_seconds = max(15, int(tick_seconds))
        self.batch_size = max(1, min(MAX_LISTINGS_CAP, int(batch_size)))
        self.freshness_hours = freshness_hours
        self.dry_run = bool(dry_run)

        self.schema = env("WORKER_SCHEMA", "engine")
        if not _IDENT_RE.fullmatch(self.schema):  # belt and braces; common validates too
            self.schema = "engine"
        self.targets_table = f"{self.schema}.arbitrage_targets"

        self.min_margin_pct = _float_env("ARBITRAGE_MIN_EXPECTED_MARGIN_PCT",
                                         DEFAULT_MIN_MARGIN_PCT)
        self.max_acquire_minor = _int_env("ARBITRAGE_MAX_ACQUIRE_MINOR",
                                          DEFAULT_MAX_ACQUIRE_MINOR)
        self.fee_pct = _float_env("ARBITRAGE_PLATFORM_FEE_PCT", DEFAULT_PLATFORM_FEE_PCT)
        self.per_item_fee_minor = _int_env("ARBITRAGE_PER_ITEM_FEE_MINOR",
                                           DEFAULT_PER_ITEM_FEE_MINOR)
        self.shipping_minor = _int_env("ARBITRAGE_SHIPPING_MINOR", DEFAULT_SHIPPING_MINOR)
        self.default_currency = norm_currency(env("ARBITRAGE_CURRENCY", "USD"))
        self.timeout_ms = _int_env("ARBITRAGE_SCRAPE_TIMEOUT_MS", DEFAULT_SCRAPE_TIMEOUT_MS)
        self.http_timeout = httpx.Timeout(self.timeout_ms / 1000.0, connect=5.0)

        self.sources = load_sources()
        self.comp_sources = load_comp_sources()
        self._comps_warned = False
        self._errors = 0
        self._stopping = threading.Event()
        # Browser plumbing lives entirely in the tick thread: Playwright's sync
        # API binds its dispatcher greenlet to the starting thread, and the
        # http_transport error message that results from crossing threads is
        # famously inscrutable, so the objects are created, used and closed
        # on exactly one thread.
        self._browser_ctx: tuple[Any, Any] | None = None  # (playwright, browser)
        self._tick_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ schema

    def ensure_tables(self) -> None:
        """Own arbitrage_targets. Idempotent, cheap enough to run per tick.

        Columns mirror schema.sql house style (minor units, text identity,
        jsonb detail, partial indexes on the hot reads). UNIQUE (source,
        identity) is the second dedupe net below seen(): seen() consults
        agent_logs, which has retention pressure and no uniqueness, so without
        this index a seen() miss would grow the table with duplicates of the
        same deal and quietly skew the margin gauge.
        """
        sql = f"""
            create table if not exists {self.targets_table} (
              id                   bigint generated always as identity primary key,
              source               text        not null,
              identity             text        not null,
              identity_hash        text        not null,
              title                text        not null,
              source_url           text,
              sell_url             text,
              acquire_cost_minor   bigint      not null check (acquire_cost_minor > 0),
              comp_floor_minor     bigint      not null check (comp_floor_minor > 0),
              -- Marketplace take on the floor, stored separately so a change to
              -- the assumed fee schedule never silently rewrites history.
              fee_minor            bigint      not null default 0 check (fee_minor >= 0),
              currency             text        not null default 'USD'
                                   check (length(currency) = 3 and currency ~ '^[A-Z]+$'),
              expected_profit_minor bigint,
              expected_margin_pct  numeric(8,2),
              calc                 jsonb       not null default '{{}}'::jsonb,
              -- Append-mostly. A real 'executed' transition will come from the
              -- buy process via UPDATE, which this worker never issues.
              status               text        not null default 'discovered'
                                   check (status in ('discovered','validated',
                                                     'executed','passed','stale')),
              discovered_at        timestamptz not null default now(),
              unique (source, identity)
            );
            -- "What did we find today, best first" is the dashboard query and
            -- the margin-gauge query; one partial index serves both.
            create index if not exists arbitrage_targets_day_idx
              on {self.targets_table} (discovered_at desc, expected_margin_pct desc)
              where status = 'discovered';
            create index if not exists arbitrage_targets_seen_idx
              on {self.targets_table} (identity_hash);
        """  # table name is a validated constant, interpolated into DDL
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql)
        except Exception as exc:  # a missing create here surfaces on the first insert too
            log.warning("ensure_tables failed: %s", exc)

    # ---------------------------------------------------------------- scraping

    def scrape_http(self, url: str) -> list[Listing]:
        """httpx path for bundle/auction feeds: JSON arrays or JSON-LD HTML.

        Content-type decides the parser rather than the config repeating it:
        a feed that switches from HTML to JSON should keep working through a
        deploy, not need one.
        """
        with httpx.Client(timeout=self.http_timeout, headers={"User-Agent": UA},
                          follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code == 403:
            # The classic sign the page wanted a browser. Wrong-tool failure,
            # named as such rather than "got 0 listings".
            raise RuntimeError(
                f"http 403 on {url}: this source likely needs route=browser")
        if response.status_code >= 400:
            raise RuntimeError(f"source fetch http {response.status_code}: {url}")
        text = response.text
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            items = data if isinstance(data, list) else (
                data.get("listings") or data.get("items") or data.get("results") or [])
            listings = [parsed for item in items if isinstance(item, dict)
                        and (parsed := listing_from_dict(item, fallback_source=""))]
            if listings or isinstance(data, list):
                return listings
            # A JSON body with no recognizable items falls through to JSON-LD
            # rather than reporting a clean zero.
        return listings_from_jsonld(extract_jsonld(text))

    def scrape_browser(self, source: dict[str, Any]) -> list[Listing]:
        """Playwright path for JS-rendered, bot-checked pages (ticket drops).

        One page per source per tick, waited on until the listing selector
        appears rather than a fixed sleep: too short and the tick races the
        hydrating app and reports zero listings, which seen() then records as a
        fully-scraped source for this tick.
        """
        name = str(source["name"])
        listing_sel = str(source.get("listing_selector") or "[itemscope],[itemprop]")
        _context, browser = self._browser()
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
        try:
            page.goto(str(source["url"]), timeout=self.timeout_ms,
                      wait_until="domcontentloaded")
            with contextlib.suppress(Exception):
                page.wait_for_selector(listing_sel, timeout=min(self.timeout_ms, 15_000))
            html_text = page.content()
            els = page.query_selector_all(str(source.get("listing_selector") or "script"))
            cards: list[dict[str, Any]] = []
            for el in els[: self.batch_size]:
                cards.append(read_dom_card(el, source))
            scraped = [parsed for card in cards if (parsed := listing_from_card(card, name))]
            if scraped:
                return scraped
            # Selectors match nothing but the page is open: fall back to the
            # JSON-LD embedded in the rendered DOM, which ticket sites ship for
            # the bots they do let in.
            listings = listings_from_jsonld(extract_jsonld(html_text), source=source)
            if listings:
                log.info("source %s: selectors found 0, JSON-LD found %d",
                         name, len(listings))
            return listings
        finally:
            # A page that will not close must not strand the tick.
            with contextlib.suppress(Exception):
                page.close()

    def _browser(self) -> tuple[Any, Any]:
        """Lazily launch one shared browser. Caller thread: the tick thread only."""
        if self._browser_ctx is not None:
            return self._browser_ctx
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # a named fix beats a traceback per restart
            raise RuntimeError(
                "the arbitrage worker's browser route needs Playwright "
                "(pip install playwright && playwright install chromium)") from exc
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception:
            playwright.stop()
            raise
        self._browser_ctx = (playwright, browser)
        log.info("headless chromium launched for browser-routed sources")
        return self._browser_ctx

    def close_browser(self) -> None:
        if self._browser_ctx is None:
            return
        playwright, browser = self._browser_ctx
        self._browser_ctx = None
        # Shutting down; nothing downstream depends on these closing cleanly.
        with contextlib.suppress(Exception):
            browser.close()
            playwright.stop()

    # --------------------------------------------------------------- margins

    def evaluate(self, listing: Listing) -> Opportunity | None:
        """Floor price, cost stack, margin. None if no floor price is obtainable.

        Margin is on ACQUISITION COST, not on the sale price: with the fee
        subtracted the two differ by roughly a factor of (1 - fee), and a
        resale operator's decision ("$80 in, how much out?") is the first one.
        The breakdown goes into calc so the second reading is always one
        subtraction away.
        """
        floor, comp_source = self.floor_price(listing)
        if floor is None or floor <= 0:
            return None
        cost = listing.acquire_minor
        fee_minor = round(floor * self.fee_pct / 100.0)
        net_proceeds = floor - fee_minor - self.per_item_fee_minor - self.shipping_minor
        profit = net_proceeds - cost
        margin_pct = (profit / cost * 100.0) if cost > 0 else 0.0
        return Opportunity(
            listing=listing, floor_minor=floor, fee_minor=fee_minor,
            per_item_fee_minor=self.per_item_fee_minor, shipping_minor=self.shipping_minor,
            net_proceeds_minor=net_proceeds, expected_profit_minor=profit,
            expected_margin_pct=margin_pct, comp_source=comp_source)

    def floor_price(self, listing: Listing) -> tuple[int | None, str]:
        """First comparator that yields a positive number, in config order."""
        if not self.comp_sources:
            if not self._comps_warned:
                self._comps_warned = True
                log.warning("ARBITRAGE_COMP_SOURCES_JSON unset: no floor prices, "
                            "so nothing can clear the margin bar")
                self.log("warning", "arbitrage.comps.unconfigured",
                         "no floor comparators configured; discovery will log nothing")
            return None, ""
        for comp in self.comp_sources:
            kind = str(comp.get("type") or "")
            try:
                if kind == "static_minor":
                    value = int(comp.get("value_minor") or 0)
                    if value > 0:
                        return value, str(comp.get("name") or "static_minor")
                elif kind == "http_json":
                    value = self.floor_via_http(comp, listing)
                    if value and value > 0:
                        return value, str(comp.get("name") or "http_json")
                else:
                    log.warning("unknown comp source type %r", kind)
            except Exception as exc:  # one dead comparator must not blind the whole gate
                log.warning("comp %s failed for %s: %s",
                            comp.get("name"), listing.identity[:60], exc)
        return None, ""

    def floor_via_http(self, comp: dict[str, Any], listing: Listing) -> int | None:
        template = str(comp.get("url") or "")
        if not template:
            return None
        fields = {
            "slug": slugify(listing.title),
            "query": listing.title,
            "source": listing.source,
            "identity": listing.identity,
        }
        url = template
        for key, value in fields.items():
            url = url.replace("{" + key + "}", quote(value, safe=""))
        with httpx.Client(timeout=self.http_timeout, headers={"User-Agent": UA},
                          follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code >= 400:
            raise RuntimeError(f"comp http {response.status_code}: {url[:160]}")
        number = first_number(walk_path(response.json(), str(comp.get("path") or "")))
        if number is None:
            return None
        # Accept both spellings of money; a floor of "210.00" and one of 21000
        # arrive here equally, and guessing per-response would flip readings.
        return int(number) if number >= 1000 else round(number * 100)

    # ------------------------------------------------------------------ store

    def insert_target(self, opp: Opportunity) -> bool:
        """Log one opportunity. False if (source, identity) already logged.

        ON CONFLICT DO NOTHING rather than a pre-check: seen() already filters
        the repeat case, so landing here for an existing key means seen() lost
        the row (agent_logs pruning) and this is the last net. RETURNING makes
        the statement extended-protocol, which common.py's notes show autocommit
        pipeline mode needs for reliable rowcount reads.
        """
        row = opp.to_row()
        sql = (f"insert into {self.targets_table}"  # noqa: S608 - table name is a validated constant
               " (source, identity, identity_hash, title, source_url, sell_url,"
               " acquire_cost_minor, comp_floor_minor, fee_minor, currency,"
               " expected_profit_minor, expected_margin_pct, calc)"
               " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
               " on conflict (source, identity) do nothing returning id")
        args = (row["source"], row["identity"], row["identity_hash"], row["title"],
                row["source_url"], row["sell_url"], row["acquire_cost_minor"],
                row["comp_floor_minor"], row["fee_minor"], row["currency"],
                row["expected_profit_minor"], row["expected_margin_pct"],
                json.dumps(row["calc"], default=str))
        if self.dry_run:
            log.info("[dry-run] would log %s: %s acq %s floor %s margin %.1f%%",
                     row["source"], row["title"][:60],
                     fmt_minor(row["acquire_cost_minor"], row["currency"]),
                     fmt_minor(row["comp_floor_minor"], row["currency"]),
                     row["expected_margin_pct"])
            return True
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone() is not None

    def log(self, level: str, event: str, message: str, **extra: Any) -> None:
        self.store.log(self.HANDLE, level, event, message, hustle=self.HUSTLE,
                       extra=extra or None)

    # --------------------------------------------------------------- metrics

    def refresh_margin_gauge(self) -> dict[str, Any]:
        """Recompute today's mean expected margin and stamp it into metrics.

        set_gauges (overwrite) not add_metrics (sum): expected_margin_pct is an
        average, and a per-tick ADD of an average inflates it once per tick,
        which is the stock-vs-flow trap common.py warns about by name.
        Recomputing from arbitrage_targets converges no matter how many times
        this runs today, which also repairs a restart mid-day for free.
        """
        if self.dry_run:
            return {}
        sql = (f"select count(*)::int as n, round(avg(expected_margin_pct), 2) as avg_pct,"  # noqa: S608
               " sum(expected_profit_minor)::bigint as pipeline_minor"
               f" from {self.targets_table}"
               " where discovered_at >= date_trunc('day', now())")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone() or {}
        except Exception as exc:  # the tick's own counters still land; the gauge waits
            log.warning("margin gauge read failed: %s", exc)
            return {}
        n = int(row.get("n") or 0)
        if not n:
            return {}
        gauges = {"expected_margin_pct": float(row["avg_pct"] or 0.0)}
        summary = {"opportunities_today": n,
                   "avg_expected_margin_pct": gauges["expected_margin_pct"],
                   "pipeline_profit_minor": int(row.get("pipeline_minor") or 0)}
        try:
            self.store.set_gauges(self.HANDLE, self.HUSTLE, gauges)
        except Exception as exc:
            log.warning("margin gauge write failed: %s", exc)
        log.info("%s gauge %s", self.HANDLE, summary)
        return summary

    # ------------------------------------------------------------------ tick

    def tick_once(self) -> dict[str, Any]:
        """One full pass. Raises only when EVERY source failed; returns counts otherwise.

        A single dead source is a Tuesday (pages move, hosts 403); a pass where
        nothing was scraped at all is the loop talking to the supervisor, so
        that is what gets raised into the error-streak counter.
        """
        self.ensure_tables()
        now = datetime.now(UTC)
        cutoff = self.freshness_hours * 3600.0
        counts = {"sources": 0, "sources_failed": 0, "listings": 0, "below_margin": 0,
                  "logged": 0, "dupes": 0, "stale": 0, "over_ceiling": 0}
        errors: list[str] = []

        for source in self.sources:
            name = str(source["name"])
            if source.get("requires_login"):
                self.log("warning", "arbitrage.source.login_skipped",
                         f"source {name} needs a session; anonymous scrape would "
                         "report phantom prices, skipping until storage_state is wired")
                continue
            counts["sources"] += 1
            try:
                listings = (self.scrape_browser(source) if source.get("route") == "browser"
                            else self.scrape_http(str(source["url"])))
            except Exception as exc:
                counts["sources_failed"] += 1
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                self.log("warning", "arbitrage.source.failed",
                         f"source {name}: {type(exc).__name__}: {exc}"[:2000])
                continue
            counts["listings"] += len(listings)
            ceiling = min(self.max_acquire_minor,
                          int(source.get("max_acquire_minor") or self.max_acquire_minor))
            for listing in listings[: self.batch_size]:
                age = max(0.0, (now - listing.observed_at).total_seconds())
                if age > cutoff:
                    counts["stale"] += 1
                    continue
                if listing.acquire_minor > ceiling:
                    counts["over_ceiling"] += 1
                    continue
                opp = self.evaluate(listing)
                if opp is None:
                    continue
                if opp.expected_margin_pct < self.min_margin_pct:
                    counts["below_margin"] += 1
                    self.log("debug", "arbitrage.candidate.below_margin",
                             f"{listing.title[:80]} margin "
                             f"{opp.expected_margin_pct:.1f}% < {self.min_margin_pct}%",
                             identity=listing.identity[:120])
                    continue
                if not self.is_new(listing):
                    counts["dupes"] += 1
                    continue
                if self.insert_target(opp):
                    counts["logged"] += 1
                    self.log("info", "arbitrage.opportunity.logged",
                             f"{listing.title[:100]} | acquire "
                             f"{fmt_minor(listing.acquire_minor, listing.currency)}, floor "
                             f"{fmt_minor(opp.floor_minor, listing.currency)}, expected "
                             f"{fmt_minor(opp.expected_profit_minor, listing.currency)} "
                             f"({opp.expected_margin_pct:.1f}%)",
                             source=listing.source, identity_hash=opp.identity_hash,
                             margin_pct=round(opp.expected_margin_pct, 2))

        if counts["logged"]:
            # Discovered = sourced deals entered the pipeline today. Additive
            # flow, so this is the one add_metrics call this worker makes.
            self.store.add_metrics(self.HANDLE, self.HUSTLE,
                                   {"targets_discovered": counts["logged"]})
        gauge = self.refresh_margin_gauge()

        self.log("info", "worker.tick",
                 f"sources={counts['sources']} listings={counts['listings']} "
                 f"logged={counts['logged']} below_margin={counts['below_margin']} "
                 f"dupes={counts['dupes']}", **counts)
        log.info("%s tick %s", self.HANDLE, counts)

        if counts["sources"] and counts["sources_failed"] == counts["sources"]:
            raise RuntimeError("every source failed: " + "; ".join(errors)[:400])
        return {"counts": counts, "gauge": gauge}

    def is_new(self, listing: Listing) -> bool:
        """Not seen before. Marks seen as it answers True (common.py semantics).

        Marking happens at scrape-decision time, not after the insert, because
        every path between the two is a place a duplicate could slip: seen()
        plus the table's unique index together are the dedupe, and either one
        failing alone still costs at most one row.
        """
        digest = identity_hash_for(listing.source, listing.identity)
        if self.store.seen(self.HANDLE, digest):
            return False
        self.store.mark_seen(self.HANDLE, self.HUSTLE, digest,
                             f"{listing.source}|{listing.identity}")
        return True

    # ------------------------------------------------------------------- run

    def run_forever(self) -> None:
        log.info("worker %s starting | hustle=%s tick=%ss cap=%s min_margin=%.1f%% "
                 "sources=%s comps=%s%s",
                 self.HANDLE, self.HUSTLE, self.tick_seconds, self.batch_size,
                 self.min_margin_pct, len(self.sources), len(self.comp_sources),
                 " DRY RUN" if self.dry_run else "")
        try:
            while not self._stopping.is_set():
                self.run_tick_blocking()
                self._stopping.wait(timeout=max(5, self.tick_seconds))
        finally:
            self.close_browser()

    def run_tick_blocking(self) -> dict[str, Any] | None:
        """Run one tick ON THIS THREAD and swallow expected failures.

        Playwright's sync API binds to the thread that started it, so ticks
        cannot round-robin across a pool: the loop runs them all on the tick
        thread (run_forever's thread, or main under --once). Every source
        failure is already caught inside tick_once, so reaching the handler
        here means the unexpected; dropping the shared browser on the second
        one in a row is the cheap hedge against a tick that died mid-page and
        left the context wedged for everything after it.
        """
        self._tick_thread = threading.current_thread()
        try:
            metrics = self.tick_once()
            self._errors = 0
            return metrics
        except Exception as exc:
            self._errors += 1
            level = logging.ERROR if self._errors >= ERRORS_BEFORE_UNWELL else logging.WARNING
            log.log(level, "tick failed (%d in a row): %s", self._errors, exc)
            self.log("error" if level >= logging.ERROR else "warning",
                     "worker.tick.failed", f"{type(exc).__name__}: {exc}",
                     consecutive_errors=self._errors)
            if self._errors >= 2:
                self.close_browser()
            return None

    def _thread_main(self) -> None:
        self.run_forever()

    def run(self) -> None:
        """Block on the tick loop, main thread free for signals."""
        # Ctrl-C is a supported stop; run_forever's finally closed the browser.
        with contextlib.suppress(KeyboardInterrupt):
            self.run_forever()

    def stop(self) -> None:
        self._stopping.set()

    def close(self) -> None:
        self.stop()
        if self._tick_thread is threading.current_thread():
            self.close_browser()  # close() only unwinds on the thread that owns the context


# ---------------------------------------------------------------------------
# Listing extraction (DOM cards and dicts)
# ---------------------------------------------------------------------------


def read_dom_card(el: Any, source: dict[str, Any]) -> dict[str, Any]:
    """Pull title/price/link out of one matched element, per source selectors."""
    def text_of(selector: str | None, default: str = "") -> str:
        if not selector:
            return default
        sub = el.query_selector(selector)
        # text_content, not inner_text: inner_text can raise for elements off
        # the rendered layout, and a scraper that dies on one hidden node loses
        # the page. Whitespace normalisation happens in normalized_title.
        return ((sub.text_content() if sub else "") or "") or default

    title = text_of(source.get("title_selector"))
    if not title:  # microdata default: most ticket/deal cards carry itemProps
        title = text_of("[itemprop=name], h2, h3", default="")
    price = text_of(source.get("price_selector"))
    if not price:
        price = text_of("[itemprop=price], .price, [class*=price]")
    link = source.get("link_selector")
    href = ""
    anchor = el.query_selector(link) if link else el.query_selector("a[href]")
    if anchor is None and el.get_attribute("href"):
        href = str(el.get_attribute("href"))
    elif anchor is not None:
        href = str(anchor.get_attribute("href") or "")
    return {"title": normalized_title(title), "price_raw": price.strip(), "href": href}


def listing_from_card(card: dict[str, Any], source: str) -> Listing | None:
    title = card.get("title") or ""
    parsed = parse_price_minor(str(card.get("price_raw") or ""))
    if not title or parsed is None:
        # No title or unreadable price is "not a listing", not a zero-price one;
        # see parse_price_minor on why 0 is the worst possible fallback here.
        return None
    # norm_currency("") falls through to ARBITRAGE_CURRENCY: a bare DOM card
    # rarely states its currency, and guessing USD here would split the table
    # across two spellings of whatever the operator actually configured.
    minor, currency = parsed, norm_currency("")
    url = card.get("href") or None
    return Listing(
        source=source, identity=canonical_identity(url, title, source),
        title=title[:500], acquire_minor=minor, currency=currency, url=url,
        sell_url=None, observed_at=datetime.now(UTC),
        payload={"parser": "dom"})


def listing_from_dict(item: dict[str, Any], fallback_source: str = "",
                      source: str = "") -> Listing | None:
    money = parse_money_fields(item)
    if money is None:
        return None
    minor, currency = money
    title = normalized_title(str(item.get("title") or item.get("name")
                                or item.get("domain") or ""))
    if not title:
        return None
    url = item.get("url") or item.get("link") or item.get("permalink")
    url = str(url) if url else None
    src = source or str(item.get("source") or fallback_source) or "feed"
    return Listing(
        source=src, identity=canonical_identity(url, title, src), title=title[:500],
        acquire_minor=minor, currency=norm_currency(currency), url=url, sell_url=None,
        observed_at=parse_timestamp(item) or datetime.now(UTC),
        payload={"parser": "feed"})


def listings_from_jsonld(nodes: list[dict[str, Any]],
                         source: dict[str, Any] | None = None) -> list[Listing]:
    """Event/Offer/Product JSON-LD to listings. Handles nested offer lists.

    Ticketmaster/AXS-class pages ship Event schema with nested Offer arrays
    (each with price, priceCurrency and availability); AppSumo-style pages ship
    Product. Both are scraped here so the browser path has a second parser when
    CSS selectors drift, which they always eventually do.
    """
    src = str((source or {}).get("name") or "jsonld")
    listings: list[Listing] = []
    for node in nodes:
        node_type = str(node.get("@type") or "")
        types = node_type if isinstance(node_type, list) else [node_type]
        if not any(t in ("Event", "Product", "AggregateOffer", "Offer", "SpecialAnnouncement")
                   for t in types):
            continue
        title = normalized_title(str(node.get("name") or ""))
        if not title:
            continue
        url = node.get("url") or node.get("mainEntityOfPage")
        url = str(url) if isinstance(url, (str, dict)) else None
        if isinstance(url, dict):
            url = str(url.get("@id") or "") or None
        offers = offers_of(node) or [{}]
        for offer in offers:
            raw = offer.get("price")
            minor = (round(float(raw) * 100)
                     if isinstance(raw, (int, float)) else parse_price_minor(str(raw or "")))
            if minor is None:
                continue
            currency = norm_currency(str(offer.get("priceCurrency") or ""))
            avail = str(offer.get("availability") or "").rsplit("/", 1)[-1]
            listings.append(Listing(
                source=src, identity=canonical_identity(url, title, src),
                title=title[:500], acquire_minor=minor, currency=currency,
                url=str(url) if url else None, sell_url=None,
                observed_at=datetime.now(UTC),
                payload={"parser": "jsonld", "jsonld_type": ",".join(types),
                         "availability": avail}))
    return listings


def parse_timestamp(item: dict[str, Any]) -> datetime | None:
    for key in ("observed_at", "starts_at", "on_sale_date", "posted_at", "end_date",
                "updated_at", "created_at", "date"):
        raw = item.get(key)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                continue
        elif isinstance(raw, (int, float)):
            with contextlib.suppress(ValueError, OSError, OverflowError):
                return datetime.fromtimestamp(float(raw), UTC)
    return None


# ---------------------------------------------------------------------------
# Comparator helpers
# ---------------------------------------------------------------------------


def walk_path(data: Any, path: str) -> Any:
    """Follow 'data.floor' through dicts and list indices; None if absent."""
    node = data
    for part in filter(None, path.split(".")):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            with contextlib.suppress(ValueError, IndexError):
                node = node[int(part)]
                continue
            return None
        else:
            return None
    return node


def first_number(node: Any) -> float | None:
    """Depth-first first positive number. Strings parse ('$210.00' -> 210.0)."""
    if isinstance(node, bool):
        return None
    if isinstance(node, (int, float)):
        return float(node) if node > 0 else None
    if isinstance(node, str):
        # parse_price_minor returns minor units; the callers multiply by 100
        # themselves only for raw floats, and a doubled value here (or a
        # doubled CALL, as an earlier draft had) would 100x the floor.
        parsed = parse_price_minor(node)
        return float(parsed) if parsed is not None else None
    if isinstance(node, dict):
        for value in node.values():
            found = first_number(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = first_number(value)
            if found is not None:
                return found
    return None


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def norm_currency(raw: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z]", "", str(raw or ""))[:3].upper()
    return cleaned if len(cleaned) == 3 else env("ARBITRAGE_CURRENCY", "USD")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_sources() -> list[dict[str, Any]]:
    """Parse and validate ARBITRAGE_TARGETS_JSON. Raises ConfigError on garbage.

    Loud at boot beats silently discovering at 03:00 that the JSON has been
    malformed since the last deploy and every tick has scraped nothing.
    """
    raw = env("ARBITRAGE_TARGETS_JSON")
    if not raw:
        raise ConfigError(
            "ARBITRAGE_TARGETS_JSON is unset; this worker has nothing to scrape. "
            "See the module docstring for the config shape.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"ARBITRAGE_TARGETS_JSON is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ConfigError("ARBITRAGE_TARGETS_JSON must be a non-empty JSON array")
    sources: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ConfigError(f"source entry needs at least a name: {entry!r}")
        name = str(entry["name"])
        if name in seen_names:
            raise ConfigError(f"duplicate source name {name!r}: names key every "
                              "cursor and seen() entry, so they must be unique")
        seen_names.add(name)
        route = str(entry.get("route") or "http").lower()
        if route not in ("http", "browser"):
            raise ConfigError(f"source {name}: route must be 'http' or 'browser'")
        if not entry.get("url"):
            raise ConfigError(f"source {name}: url is required")
        sources.append({**entry, "name": name, "route": route})
    return sources


def load_comp_sources() -> list[dict[str, Any]]:
    raw = env("ARBITRAGE_COMP_SOURCES_JSON")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"ARBITRAGE_COMP_SOURCES_JSON is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError("ARBITRAGE_COMP_SOURCES_JSON must be a JSON array")
    for entry in data:
        kind = str(entry.get("type") or "")
        if kind not in ("http_json", "static_minor"):
            raise ConfigError(f"comp source type must be http_json or static_minor, "
                              f"got {kind!r}")
    return data


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="workers.digital_arbitrage_worker",
        description=("Digital arbitrage worker: scrape target listings, compute "
                     "expected margins, log opportunities to arbitrage_targets."),
        epilog="All configuration comes from the environment (.env is read for you).")
    parser.add_argument("--once", action="store_true",
                        help="run a single tick and exit (cron / smoke test)")
    parser.add_argument("--tick-seconds", type=int, default=None,
                        help="override ARBITRAGE_TICK_SECONDS")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="max listings per source per tick")
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape and calculate for real; write nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    dsn = build_database_url()
    if not dsn:
        print("DATABASE_URL (or SUPABASE_DB_PASSWORD) is unset; nowhere to log. "
              "Refusing to start worker:digital_arbitrage.", file=sys.stderr)
        return 2

    try:
        worker = DigitalArbitrageWorker(
            dsn,
            tick_seconds=args.tick_seconds
            or _int_env("ARBITRAGE_TICK_SECONDS", 0)
            or _int_env("WORKER_TICK_SECONDS", DEFAULT_TICK_SECONDS),
            batch_size=args.batch_size
            or _int_env("ARBITRAGE_LISTINGS_CAP", DEFAULT_LISTINGS_CAP),
            freshness_hours=_float_env("ARBITRAGE_FRESHNESS_HOURS",
                                       DEFAULT_FRESHNESS_HOURS),
            dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.once:
        # Runs on this thread and closes the browser before exit: a leaked
        # Chromium child outliving `--once` would accumulate under cron.
        metrics = worker.run_tick_blocking()
        worker.close_browser()
        if metrics is None:
            print("tick failed; see logs above", file=sys.stderr)
            return 1
        print(json.dumps({"worker": worker.HANDLE, "metrics": metrics}, default=str))
        return 0

    def _shutdown(signum: int, _frame: Any) -> None:
        log.info("signal %s received, finishing current work and stopping", signum)
        worker.stop()

    try:
        import signal as _signal
        _signal.signal(_signal.SIGTERM, _shutdown)
        _signal.signal(_signal.SIGINT, _shutdown)
    except ValueError:  # pragma: no cover - non-main thread
        pass

    # One thread does the blocking work (and owns the Playwright context); the
    # main thread sleeps on the stop event, which is where signals are handled
    # and where a KeyboardInterrupt lands.
    loop = threading.Thread(target=worker._thread_main, name="arbitrage-tick",
                            daemon=True)
    loop.start()
    grace_seconds = max(60.0, worker.timeout_ms / 1000.0 + 30.0)
    while not worker._stopping.is_set():
        worker._stopping.wait(timeout=1.0)
    loop.join(timeout=grace_seconds)
    if loop.is_alive():
        # A wedged Playwright call ignores us; say so instead of exiting quietly
        # and letting the supervisor think the process left cleanly. The thread
        # is daemonic, so the interpreter still exits.
        log.warning("tick thread still busy %.0fs after stop; exiting anyway",
                    grace_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
