"""Compatibility entry point for the implementation in ``adya-share``.

The shared implementation lives beside the tests so both ``python server.py``
from the repository root and the test package use the same runtime.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_IMPLEMENTATION = Path(__file__).resolve().parent / "adya-share" / "harness.py"
_SPEC = importlib.util.spec_from_file_location("_adya_share_harness", _IMPLEMENTATION)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - packaging failure
    raise ImportError(f"Unable to load ADYA harness implementation from {_IMPLEMENTATION}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

for _name in dir(_MODULE):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_MODULE, _name)

__all__ = [name for name in dir(_MODULE) if not name.startswith("_")]
