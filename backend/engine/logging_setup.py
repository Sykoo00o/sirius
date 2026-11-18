"""Logging setup: stdout only.

Container stdout is captured by Docker's json-file driver and tailed by the
dashboard, so this process must not also write files: duplicated logs, and the
two copies disagree after a rotation.

Every record carries its own service name because `docker compose logs` output
from six services interleaves, and a line that just says 'ERROR' is useless.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup_logging(service: str = "engine", level: str | None = None) -> None:
    global _CONFIGURED
    log_level = (level or os.getenv("ENGINE_LOG_LEVEL") or "INFO").upper()

    formatter = logging.Formatter(
        fmt=f"%(asctime)s %(levelname)-7s [{service}] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    if _CONFIGURED:
        root.handlers = [handler]
        return
    root.handlers = [handler]
    _CONFIGURED = True

    # These three log request bodies at DEBUG, and a request body contains the
    # bearer token. Keep them at WARNING.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
