"""Frozen CPU trainer: the bounded training-child protocol and nothing else.

The authority launches this binary (opendialer-trainer-<triple>) as its training child, with a
JSON request on stdin and a bounded JSON reply on stdout (app.ml.worker). It never imports the
authority, its database or its secrets. ``--self-check`` loads the fitting stack and reports its
versions, so packaging can prove a signed trainer starts.
"""

import json
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/backend"))


def self_check():
    import numpy
    import scipy
    import sklearn
    from app.ml import training  # noqa: F401
    from sklearn.calibration import CalibratedClassifierCV  # noqa: F401 - import is the check
    from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: F401

    versions = {"numpy": numpy.__version__, "scipy": scipy.__version__, "sklearn": sklearn.__version__}
    print(json.dumps({"trainer": "ok", "libraries": versions}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        self_check()
    elif sys.argv[1:]:
        print("opendialer-trainer takes a training request on stdin, or --self-check", file=sys.stderr)
        raise SystemExit(2)
    else:
        from app.ml.worker import main

        main()
