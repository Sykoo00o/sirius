"""Which .env the funnel reads, and in what priority.

These exist because of a measured production failure, not a hypothetical. On the deployed host the
repository root carried its own `.env` (the engine's: LLM and Telegram keys, zero `FUNNEL_*` lines),
and the loader stopped at the first file it found. So a shell-launched funnel process read
`FUNNEL_EMAIL_OUTBOX` / `FUNNEL_DB_PATH` / `FUNNEL_BILLING_DB_PATH` as unset and wrote state to
repo-relative `funnel_app/data/...` paths, i.e. the ones `git clean -fdx` deletes. systemd hid it,
because `EnvironmentFile=` injects the real values and the running process env was correct; a mail
probe run by hand is what exposed it.

The test tree mirrors that layout on purpose: a root `.env` whose keys are *not* funnel keys. An
earlier version used a root `.env` that also defined the same FUNNEL names, which passes with the
old break-on-first-file code and so proves nothing.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

CONFIG_SRC = Path(__file__).resolve().parents[1] / "config.py"


@pytest.fixture
def load_config(tmp_path, monkeypatch):
    """Import config.py against a synthetic repo layout, with the process env cleared of FUNNEL_*.

    A fresh module object per test: `FunnelSettings.from_env()` calls `load_dotenv()`, which mutates
    `os.environ` via `setdefault`, so the environment is the shared state here and leaking it between
    tests would make the assertions depend on execution order.
    """
    def _load(root_env: dict[str, str] | None, app_env: dict[str, str] | None,
              process_env: dict[str, str] | None = None):
        for key in [k for k in os.environ if k.startswith("FUNNEL_")]:
            monkeypatch.delenv(key, raising=False)
        # Applied AFTER the clear: these simulate the real process environment (a compose
        # `environment:` block, or systemd EnvironmentFile=), which the funnel files must not beat.
        # Setting them before the clear wiped the caller's intent — the clear does not know a value
        # was placed on purpose — which is exactly why this parameter exists rather than a bare
        # monkeypatch.setenv in the test body.
        for key, value in (process_env or {}).items():
            monkeypatch.setenv(key, value)
        repo = tmp_path / "repo"
        app = repo / "funnel_app"
        app.mkdir(parents=True, exist_ok=True)
        if root_env is not None:
            (repo / ".env").write_text(
                "".join(f"{k}={v}\n" for k, v in root_env.items()), encoding="utf-8")
        if app_env is not None:
            (app / ".env").write_text(
                "".join(f"{k}={v}\n" for k, v in app_env.items()), encoding="utf-8")
        module_name = f"funnel_config_{abs(hash(str(tmp_path)))}"
        spec = importlib.util.spec_from_file_location(module_name, CONFIG_SRC)
        module = importlib.util.module_from_spec(spec)
        # Register before exec: `FunnelSettings` is a @dataclass, and at class-creation time
        # dataclasses resolves `cls.__module__` through sys.modules to find InitVar. Unregistered,
        # the import itself dies with "'NoneType' object has no attribute '__dict__'", which reads
        # like a config.py bug and is really the harness's.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)   # __file__ is the real config.py, so patch ROOT below
        finally:
            sys.modules.pop(module_name, None)
        monkeypatch.setattr(module, "ROOT", app)
        monkeypatch.setattr(
            module, "ENV_PATHS", (app / ".env", repo / ".env"), raising=False)
        return module

    yield _load
    for key in [k for k in os.environ if k.startswith("FUNNEL_")]:
        monkeypatch.delenv(key, raising=False)




def test_a_repo_root_env_does_not_shadow_the_funnels_own_state_paths(load_config):
    """The production case, reproduced exactly: a root .env that speaks none of the funnel's
    language must not cost the funnel its absolute state paths."""
    cfg = load_config(
        root_env={"OPENAI_API_KEY": "sk-engine-key", "TELEGRAM_BOT_TOKEN": "123:engine"},
        app_env={"FUNNEL_EMAIL_OUTBOX": "/var/lib/funnel-app/outbox",
                 "FUNNEL_DB_PATH": "/var/lib/funnel-app/responses.sqlite3"})
    settings = cfg.FunnelSettings.from_env()
    assert str(settings.email_outbox_abspath) == "/var/lib/funnel-app/outbox"
    assert settings.database_path == "/var/lib/funnel-app/responses.sqlite3", (
        "the funnel read the repo root's .env and booted on a repo-relative default, which a "
        "`git clean -fdx` on the deploy checkout deletes")


def test_no_root_env_still_reads_the_funnels_own(load_config):
    cfg = load_config(root_env=None,
                      app_env={"FUNNEL_EMAIL_OUTBOX": "/var/lib/funnel-app/outbox"})
    assert str(cfg.FunnelSettings.from_env().email_outbox_abspath) == "/var/lib/funnel-app/outbox"


def test_shared_names_resolve_to_the_funnels_value_then_the_roots(load_config):
    """Priority is funnel-first, and the root file still contributes what only it defines. That is
    the behaviour the ordering comment promises; if `break` comes back, the second assert dies."""
    cfg = load_config(root_env={"FUNNEL_LITE_AMOUNT_MINOR": "990", "SHARED_REGION": "eu"},
                      app_env={"FUNNEL_LITE_AMOUNT_MINOR": "1400"})
    settings = cfg.FunnelSettings.from_env()
    assert settings.lite_amount_minor == 1400, "the root .env overrode the funnel's own value"
    assert os.environ.get("SHARED_REGION") == "eu", (
        "only the first file was loaded: the break is back, so a root .env means the funnel "
        "silently ignores its own file when the order ever changes again")


def test_process_environment_beats_both_files(load_config):
    """Container/override semantics: an `Environment=` override or an exported var must beat both
    files, which is how FUNNEL_CHARGES_ENABLED is meant to be forced true without editing the file
    (funnel-app.service points at this exact mechanism in its own comments).

    On a path each reader genuinely takes. An earlier version of this test used
    FUNNEL_LITE_AMOUNT_MINOR against its dataclass default and found a real asymmetry rather than a
    bug: `_int()` returns the *code* default when a name is absent from the environment, so a
    dataclass field default can never be beaten by process env for a numeric — the value has to be
    in a file or exported to be read at all. Paths (`FUNNEL_DB_PATH`) go through `os.getenv(name,
    default)` and take process env directly. Exporting only a numeric default therefore proves
    nothing about precedence; a numeric that exists in a file does."""
    cfg = load_config(root_env={"FUNNEL_DB_PATH": "/from/root/sqlite"},
                      app_env={"FUNNEL_DB_PATH": "/from/app/sqlite",
                               "FUNNEL_LITE_AMOUNT_MINOR": "1400"},
                      process_env={"FUNNEL_DB_PATH": "/var/run/funnel-live/responses.sqlite3"})
    settings = cfg.FunnelSettings.from_env()
    assert settings.database_path == "/var/run/funnel-live/responses.sqlite3", (
        f"process env lost to a file: {settings.database_path}")
    # The file-supplied numeric still loads, so the assertion above did not pass by reading nothing.
    assert settings.lite_amount_minor == 1400


def test_app_env_path_is_first_in_env_paths():
    """A fact about the constant, cheap enough to keep next to the behavioural tests above: if
    someone reorders ENV_PATHS back, the priority test can still pass on a checkout with no root
    .env, which is every dev laptop and precisely not the deploy host."""
    src = CONFIG_SRC.read_text(encoding="utf-8")
    line = [l for l in src.splitlines() if l.startswith("ENV_PATHS = ")][0]
    assert line.index("ROOT / \".env\"") < line.index("ROOT.parent / \".env\""), line


def test_from_env_twice_keeps_the_same_precedence(load_config):
    """`from_env()` is not idempotent in the shipped code, and the funnel's own README invites the
    second call: a script that constructs settings and then imports something that constructs them
    again. Plain `setdefault` makes the *first file that wrote a name* beat everything afterwards,
    so the second read disagrees with the first about which .env is authoritative. Production is
    spared only because systemd delivers the file as process env; that is a circumstance, not a
    guarantee, and it is invisible from inside the app."""
    cfg = load_config(root_env={"FUNNEL_BRAND_NAME": "Root Brand", "FUNNEL_LITE_AMOUNT_MINOR": "990"},
                      app_env={"FUNNEL_BRAND_NAME": "Funnel Brand"})
    first = cfg.FunnelSettings.from_env()
    second = cfg.FunnelSettings.from_env()
    assert first.brand_name == second.brand_name == "Funnel Brand"
    assert first.lite_amount_minor == second.lite_amount_minor == 990
    assert first.database_path == second.database_path
