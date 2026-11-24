"""Load a funnel app's `claims.py` and `quiz.py` by file path.

Same technique as `funnel_app/llm_flavour.py` loading `backend/llm_router.py`: the agent never
imports the funnel package (which would pull FastAPI and read the funnel's env), and each app gets
its own module name so Routine Lab's and kegeldoc's rules can be loaded in one process without one
shadowing the other. Both modules are stdlib-only in both apps.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


class FunnelModuleMissing(RuntimeError):
    pass


def _load(app_dir: Path, name: str) -> ModuleType:
    path = app_dir / f"{name}.py"
    module_name = f"marketing_{app_dir.name}_{name}"
    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", "") == str(path):
        return cached
    if not path.is_file():
        raise FunnelModuleMissing(f"{path} not found; set MARKETING_FUNNEL_APP_DIR")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise FunnelModuleMissing(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[module_name]
        raise
    return module


def claims_module(app_dir: Path) -> ModuleType:
    return _load(app_dir, "claims")


def quiz_module(app_dir: Path) -> ModuleType:
    return _load(app_dir, "quiz")
