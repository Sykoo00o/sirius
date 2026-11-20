# Which page views the funnel counts, and how often

Written because two of the three telemetry bugs this phase found were in the *counting rules*
rather than in the money path, and because the rules are the part that makes a ratio mean anything.
A `lp_view` column whose definition nobody can state is a denominator nobody can defend.

## The rules

| Path | Event | Dedupe |
| --- | --- | --- |
| `/` | `lp_view` | visitor x UTC day |
| `/onboarding...` | `quiz_started` | visitor x UTC day |
| `/artefact...` | `quiz_completed` | visitor x UTC day |
| `/checkout/preview...` | `trial_started` | visitor x UTC day |
| `/account...` | *(none)* | |
| `/webhooks/*`, `/static/*`, `/health`, `/dev/*`, `/ops/*` | *(none)* | |

Dedupe means one row per visitor per UTC day, enforced by a partial unique index on
`(event, visitor_key, idempotency_key)` rather than by an application check. Everything else the
funnel records happens at a money moment and is deduped on the processor's own event id, which is
the stronger key: a `checkout.session.completed` delivered five times is one `trial_paid`.

`NON_PAGE_PATHS` is matched on the exact path, not as a prefix, so `/onboarding` is excluded while
`/onboarding/3` still resolves to `quiz_started` through the prefix rule. Getting that backwards
silently erased the quiz-start count for every walk that began with the redirect.

## Redirects count at exactly one page

A bounce is not a page view: a visitor redirected before seeing anything saw nothing, and counting
the destination for both endpoints makes `/ → /onboarding` look like two views from one arrival.
Traffic-to-quiz conversion stops being a conversion.

But a visitor arriving cold at `/account`, or at `/onboarding/5` with a half-finished quiz, reaches
their destination **through** a redirect, and erasing that hop loses the arrival entirely: the
landing page is the top of every funnel ratio, and a count that misses everyone who landed on a
deep link is a count that biases the very ratios the kill metrics are read against.

So one path is exempt and it is named: `REDIRECT_COUNTS_AT = {"lp_view"}`. A redirect that *ends*
at the landing page counts the landing page. Every other destination of a 3xx records nothing,
because the page that rendered is the one the visitor looks at, and the page that bounced is not a
page.

## Why the tracker is a `(visitor_key, path)` function

`count_page_view(visitor_key, path, redirecting=bool)` is the rule. `track_page_view(request)` is
six lines around it. The split exists because the rule involves three facts (the path, whether this
response is a bounce, and what has already been recorded today) and a middleware gives a test
exactly one handle on each. Testing through a `Request` forces either a running app or a fake that
has to behave like one, and the fake becomes the thing under test.

## Reading the numbers

`lp_views`, `opt_ins` and `trials_started` count *distinct visitors*. `trial_starts` counts every
start the funnel saw, committed plus abandoned, so the gap between the two columns is cart
abandonment and has its own column (`checkout_expired`) for cross-checking. A visit with no cookie
yields an event with no `visitor_key`: recorded, visible in `agent_logs`, and excluded from every
distinct-visitor count, because "somebody" is not separable from "somebody twice".

The counts are per UTC day. A visitor at 23:50 who returns at 00:10 is two `lp_view` rows. That is
the definition the sheet is built on (§3.4 exports UTC days), and it is why a day-boundary
disagreement with the processor's dashboard is expected rather than a bug.
