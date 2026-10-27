import os
import sys

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting server on port {port} (Python {sys.version})...")
    try:
        from backend.app import app
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        import traceback
        print("=" * 60, file=sys.stderr)
        print("ERROR STARTING APP:", file=sys.stderr)
        traceback.print_exc()
        print("=" * 60, file=sys.stderr)
        sys.exit(1)
