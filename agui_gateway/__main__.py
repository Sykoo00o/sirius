"""python -m agui_gateway  (run from the repo root so `marketing_agent` is importable)."""

from __future__ import annotations

import sys

import uvicorn

from .config import GatewaySettings, gateway_token
from .server import create_app

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def main() -> int:
    settings = GatewaySettings.from_env()
    if not gateway_token():
        print("refusing to start: AGUI_GATEWAY_TOKEN is not set", file=sys.stderr)
        return 1
    if settings.host not in LOOPBACK:
        print(f"warning: binding {settings.host}; put Cloudflare Access (or equivalent) in front",
              file=sys.stderr)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
