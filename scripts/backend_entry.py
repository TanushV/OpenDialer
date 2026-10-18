import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/backend"))
if __name__ == "__main__":
    if sys.argv[1:] == ["--train-child"]:
        from app.ml.worker import main
    else:
        from app.cli import main
    main()
