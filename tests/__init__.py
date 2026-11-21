import importlib.machinery
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("grafana", "bios-guard"):
    sys.path.insert(0, os.path.join(ROOT, sub))


def load(relpath, name):
    """Import a script whose file name isn't a module name (dashes, no .py)."""
    path = os.path.join(ROOT, relpath)
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod
