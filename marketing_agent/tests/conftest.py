from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from marketing_agent import loader  # noqa: E402
from marketing_agent.config import MarketingSettings  # noqa: E402
from marketing_agent.offer import load_offer  # noqa: E402

APPS = ("funnel_app", "kegeldoc_app")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No test may see the developer's real switches or credentials."""
    for key in list(os.environ):
        if key.startswith(("MARKETING_", "META_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MARKETING_DB_PATH", str(tmp_path / "marketing.sqlite3"))
    yield


@pytest.fixture(params=APPS)
def app(request, monkeypatch):
    monkeypatch.setenv("MARKETING_FUNNEL_APP_DIR", str(REPO / request.param))
    app_dir = REPO / request.param
    return {
        "name": request.param,
        "dir": app_dir,
        "claims": loader.claims_module(app_dir),
        "quiz": loader.quiz_module(app_dir),
        "offer": load_offer(app_dir),
        "settings": MarketingSettings.from_env(),
    }


def load_funnel_module(name: str):
    """Load a stdlib-only funnel module (telemetry, billing_store) by path, for fixtures."""
    path = REPO / "funnel_app" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_funnel_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
