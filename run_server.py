import os
import sys
import importlib.util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Add BASE_DIR and backend to sys.path
for path in [BASE_DIR, os.path.join(BASE_DIR, "backend")]:
    if os.path.exists(path) and path not in sys.path:
        sys.path.insert(0, path)

# Also search subdirectories in case user uploaded with parent folder
for item in os.listdir(BASE_DIR):
    item_path = os.path.join(BASE_DIR, item)
    if os.path.isdir(item_path):
        if item_path not in sys.path:
            sys.path.insert(0, item_path)
        sub_backend = os.path.join(item_path, "backend")
        if os.path.exists(sub_backend) and sub_backend not in sys.path:
            sys.path.insert(0, sub_backend)

import uvicorn

def load_app():
    # 1. Try standard backend.app
    try:
        from backend.app import app
        print("[OK] Loaded app from 'backend.app'")
        return app
    except (ImportError, ModuleNotFoundError) as e:
        print(f"Notice: 'from backend.app' returned {e}")

    # 2. Try root level app
    try:
        import app as root_app_module
        app = getattr(root_app_module, "app", None)
        if app:
            print("[OK] Loaded app from 'app.py' (root)")
            return app
    except (ImportError, ModuleNotFoundError) as e:
        print(f"Notice: 'import app' returned {e}")

    # 3. Search disk for app.py
    for root, dirs, files in os.walk(BASE_DIR):
        if "app.py" in files:
            full_path = os.path.join(root, "app.py")
            if root not in sys.path:
                sys.path.insert(0, root)
            parent = os.path.dirname(root)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            try:
                spec = importlib.util.spec_from_file_location("dynamic_app_module", full_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                app = getattr(mod, "app", None)
                if app:
                    print(f"[OK] Dynamically loaded app from '{full_path}'")
                    return app
            except Exception as ex:
                print(f"Error loading {full_path}: {ex}")

    raise RuntimeError("Could not find or import FastAPI 'app' anywhere in project!")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Stock Alert Server on port {port} (Python {sys.version.split()[0]})...")
    print(f"Files in root: {os.listdir(BASE_DIR)}")

    try:
        app_instance = load_app()
        uvicorn.run(app_instance, host="0.0.0.0", port=port)
    except Exception as e:
        import traceback
        print("=" * 60, file=sys.stderr)
        print("CRITICAL ERROR STARTING SERVER:", file=sys.stderr)
        traceback.print_exc()
        print("=" * 60, file=sys.stderr)
        sys.exit(1)
