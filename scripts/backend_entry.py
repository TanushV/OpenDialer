import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/backend"))
if __name__ == "__main__":
    if sys.argv[1:] == ["--train-child"]:
        # Retired: the authority carries no fitting libraries. Its child is opendialer-trainer.
        print("Training runs in the separate opendialer-trainer binary, not the authority", file=sys.stderr)
        raise SystemExit(2)
    from app.cli import main

    main()
