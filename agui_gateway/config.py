"""Gateway settings, from the environment only (CLAUDE.md rule 2). Every default means "do less".

  AGUI_GATEWAY_TOKEN      (none)                 bearer token every run must present; unset means
                                                 every run is refused (503), never served open
  AGUI_GATEWAY_HOST       127.0.0.1              bind address for `python -m agui_gateway`
  AGUI_GATEWAY_PORT       8095
  AGUI_FINANCE_URL        http://127.0.0.1:8000  the finance agent, read with GET only
  AGUI_ENVELOPE_HANDLES   worker:funnel          agent handles whose spend envelope may be read
  AGUI_HTTP_TIMEOUT       10                     seconds, per finance read
  AGUI_CHAT_TIMEOUT       60                     seconds, per finance chat turn (model round-trips)
  AGUI_FINANCE_CHAT_TOKEN (none)                 sent to the finance agent's POST /chat/ask; must
                                                 equal its FINANCE_CHAT_TOKEN. Unset = chat off

The token is read on each request and never stored on this object, so `describe()` is safe to print.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def gateway_token() -> str:
    return os.getenv("AGUI_GATEWAY_TOKEN", "").strip()


def finance_chat_token() -> str:
    return os.getenv("AGUI_FINANCE_CHAT_TOKEN", "").strip()


@dataclass(frozen=True)
class GatewaySettings:
    host: str = "127.0.0.1"
    port: int = 8095
    finance_url: str = "http://127.0.0.1:8000"
    envelope_handles: tuple[str, ...] = ("worker:funnel",)
    http_timeout: float = 10.0
    chat_timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        def _num(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, "").strip() or default)
            except ValueError:
                return default

        handles = tuple(h.strip() for h in os.getenv("AGUI_ENVELOPE_HANDLES", "").split(",")
                        if h.strip()) or cls.envelope_handles
        return cls(host=os.getenv("AGUI_GATEWAY_HOST", "").strip() or cls.host,
                   port=int(_num("AGUI_GATEWAY_PORT", cls.port)),
                   finance_url=(os.getenv("AGUI_FINANCE_URL", "").strip()
                                or cls.finance_url).rstrip("/"),
                   envelope_handles=handles,
                   http_timeout=_num("AGUI_HTTP_TIMEOUT", cls.http_timeout),
                   chat_timeout=_num("AGUI_CHAT_TIMEOUT", cls.chat_timeout))

    def describe(self) -> dict[str, object]:
        return {"host": self.host, "port": self.port, "finance_url": self.finance_url,
                "envelope_handles": list(self.envelope_handles),
                "token_configured": bool(gateway_token()),
                "finance_chat_configured": bool(finance_chat_token()), "read_only": True}
