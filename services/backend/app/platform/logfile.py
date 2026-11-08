"""One rotating log file per data folder, so no failure is silent (docs/REBUILD_PLAN.md R0.13, G6)."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MARK = "_opendialer_logfile"


def configure(data_dir):
    """Send INFO and above to <data_dir>/logs/opendialer.log; `None` removes the handler (tests)."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _MARK, False)]:
        root.removeHandler(handler)
        handler.close()
    if data_dir is None:
        return None
    folder = Path(data_dir) / "logs"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    path = folder / "opendialer.log"
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _MARK, True)
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return path
