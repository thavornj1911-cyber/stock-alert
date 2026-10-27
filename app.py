import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")

if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

try:
    from backend.app import app
except (ImportError, ModuleNotFoundError):
    # Fallback to direct file loading
    import importlib.util
    backend_app_path = os.path.join(BACKEND_DIR, "app.py")
    if os.path.exists(backend_app_path):
        spec = importlib.util.spec_from_file_location("backend_app_mod", backend_app_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        app = getattr(mod, "app", None)
    else:
        raise
