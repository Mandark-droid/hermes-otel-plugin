"""Make the repo root importable as the ``hermes_otel`` package.

The plugin is laid out flat at the repository root (``plugin.yaml`` +
``__init__.py``) so ``hermes plugins install Mandark-droid/hermes-otel-plugin``
and a plain clone into ``~/.hermes/plugins/`` work unchanged. Tests import it
as ``hermes_otel`` — this meta-path finder maps that name to the repo root
without requiring an editable install, and survives the
``sys.modules.pop(...)`` + re-import cycle the adapter tests perform.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PKG = "hermes_otel"


class _RootPackageFinder:
    def find_spec(self, fullname, path=None, target=None):  # noqa: ARG002
        if fullname == _PKG:
            return importlib.util.spec_from_file_location(
                fullname,
                _ROOT / "__init__.py",
                submodule_search_locations=[str(_ROOT)],
            )
        return None


if not any(isinstance(f, _RootPackageFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _RootPackageFinder())
