"""PyInstaller entry point for the browser-shaped local Pet Core."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.local_core.launcher import main


if __name__ == "__main__":
    raise SystemExit(main())
