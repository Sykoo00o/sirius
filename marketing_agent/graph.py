"""The only module in this package that talks to the network, and it can only read.

Meta's Graph API serves both reads this agent uses:

* the Ad Library (`GET /ads_archive`), for competitor *patterns*, and
* the Marketing API (`GET /act_{id}/insights`), for the agent's own ads' performance.

Three properties make that safe to keep in a draft-only agent:

1. **GET only.** `Transport` takes a URL and query parameters and nothing else; there is no method
   argument, no body, and `urllib_get` builds a `Request` with `method="GET"`. A pause, an upload or
   a budget change cannot be expressed through this module.
2. **One host.** Only `https://graph.facebook.com` is accepted, including for the `paging.next`
   links Meta returns, so a crafted response cannot redirect the token elsewhere.
3. **The token never leaves this module in text.** It is sent as the `access_token` query
   parameter (Graph's own convention) and scrubbed from every error message; it is never stored on
   settings, logged, or returned.

Tests inject a fake `Transport`; nothing in the test suite reaches the network.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlencode, urlsplit

GRAPH_HOST = "graph.facebook.com"
MAX_PAGES = 20

#: (url, params, timeout_seconds) -> decoded JSON object. GET by construction.
Transport = Callable[[str, dict[str, str], float], dict[str, Any]]


class GraphError(RuntimeError):
    """A read failed. The message never contains the access token."""


def _scrub(text: str, token: str) -> str:
    text = text.replace(token, "<redacted>") if token else text
    return re.sub(r"access_token=[^&\s\"']+", "access_token=<redacted>", text)


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != GRAPH_HOST:
        raise GraphError(f"refusing non-Graph URL host {parts.netloc!r}")


def urllib_get(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
    """Default transport: a single HTTPS GET with the stdlib, no redirects followed off-host."""
    import urllib.error  # noqa: PLC0415 - imported here so no other module can reuse it
    import urllib.request  # noqa: PLC0415

    _check_url(url)
    full = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}" if params else url
    request = urllib.request.Request(full, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - host checked
            if urlsplit(response.geturl()).netloc != GRAPH_HOST:
                raise GraphError("redirected off the Graph host")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise GraphError(f"HTTP {exc.code}: {body}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GraphError(f"{type(exc).__name__}: {exc}") from None


class GraphReader:
    """Paged GETs against one Graph API version with one token."""

    def __init__(self, token: str, *, version: str = "v21.0", transport: Transport | None = None,
                 timeout: float = 30.0, max_pages: int = MAX_PAGES) -> None:
        if not token:
            raise GraphError("no access token configured")
        if not re.fullmatch(r"v\d+\.\d+", version):
            raise GraphError(f"unexpected Graph API version {version!r}")
        self._token = token
        self.version = version
        self._transport = transport or urllib_get
        self.timeout = timeout
        self.max_pages = max_pages

    def url(self, path: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+(/[A-Za-z0-9_]+)?", path):
            raise GraphError(f"unexpected Graph path {path!r}")
        return f"https://{GRAPH_HOST}/{self.version}/{path}"

    def _get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        _check_url(url)
        try:
            data = self._transport(url, params, self.timeout)
        except GraphError as exc:
            raise GraphError(_scrub(str(exc), self._token)) from None
        except Exception as exc:  # noqa: BLE001 - any transport failure is a read failure
            raise GraphError(_scrub(f"{type(exc).__name__}: {exc}", self._token)) from None
        if not isinstance(data, dict):
            raise GraphError("Graph response was not a JSON object")
        if "error" in data:
            err = data["error"] if isinstance(data["error"], dict) else {"message": data["error"]}
            raise GraphError(_scrub(f"Graph error {err.get('code', '')}: "
                                    f"{str(err.get('message', ''))[:300]}", self._token))
        return data

    def paged(self, path: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        """Every item of `data` across pages, at most `max_pages` pages."""
        url = self.url(path)
        query = {**params, "access_token": self._token}
        for _ in range(self.max_pages):
            page = self._get(url, query)
            items = page.get("data") or []
            if not isinstance(items, list):
                raise GraphError("Graph response `data` was not a list")
            yield from (item for item in items if isinstance(item, dict))
            nxt = (page.get("paging") or {}).get("next")
            if not nxt:
                return
            # `next` already carries every parameter, the token included; host-checked in `_get`.
            url, query = str(nxt), {}
