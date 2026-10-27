import os
import sys
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Stock Alert Server on port {port} (Python {sys.version.split()[0]})...")
    
    # Import app directly from app.py
    try:
        from app import app
        print("[OK] Successfully imported app from app.py")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        import traceback
        print("=" * 60, file=sys.stderr)
        print("ERROR STARTING SERVER:", file=sys.stderr)
        traceback.print_exc()
        print("=" * 60, file=sys.stderr)
        sys.exit(1)
