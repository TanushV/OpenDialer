"""R3.3: imports run downward only (section 4), and new packages never reach back into the legacy mixin modules (D24)."""

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# Lower ranks are lower layers. A module may import its own package or a lower rank.
RANK = {
    "platform": 0,
    "providers": 1,
    "leads": 2,
    "numbers": 2,
    "learning": 3,
    "prep": 3,
    "queue": 4,
    "calling": 5,
    "helper": 6,
    "aftercall": 6,
    "api": 9,
}

# Known edges into legacy code, each removed by the step named. The list only shrinks: a stale entry fails too.
KNOWN = {
    ("platform/db.py", "app.operational_schema"): "R3.7 folds the schema into platform",
    ("platform/db.py", "app.privacy"): "R10.5 replaces the privacy module",
    ("platform/jobs.py", "app.privacy"): "R10.5 replaces the privacy module",
    ("providers/inference.py", "app.model_tokens"): "R9.10 builds the chat agent",
}


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "app":
                yield from (f"app.{alias.name}" for alias in node.names)
            else:
                yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def bridge(target):
    """`app.ops` imports nothing when loaded, and a leaf module imports nothing from the app: neither reaches back
    into legacy code at import time."""
    if target == "app.ops":
        return True
    path = APP.joinpath(*target.split(".")[1:]).with_suffix(".py")
    return path.is_file() and not any(t.startswith("app") for t in imports(path))


def violations():
    found = []
    for package, rank in RANK.items():
        for path in (APP / package).rglob("*.py"):
            for target in imports(path):
                parts = target.split(".")
                if parts[0] != "app" or len(parts) < 2:
                    continue
                other = parts[1]
                where = f"{path.relative_to(APP).as_posix()} imports {target}"
                if other not in RANK:
                    if not bridge(target):
                        found.append(f"{where}: legacy module")
                elif other != package and RANK[other] >= rank:
                    found.append(f"{where}: upward or sideways")
    return found


def edges(found):
    return {tuple(line.split(": ")[0].split(" imports ")) for line in found}


def test_layered_packages_import_downward_only():
    found = violations()
    assert [line for line in found if tuple(line.split(": ")[0].split(" imports ")) not in KNOWN] == []
    assert set(KNOWN) - edges(found) == set(), "a known edge is gone: remove it from KNOWN"


def test_the_check_catches_an_upward_import(tmp_path):
    fake = tmp_path / "app"
    for package in RANK:
        (fake / package).mkdir(parents=True)
    (fake / "platform" / "bad.py").write_text(
        "from app.calling import state\nfrom app.service import Service\n"
    )
    globals()["APP"], original = fake, APP
    try:
        found = violations()
    finally:
        globals()["APP"] = original
    assert found == [
        "platform/bad.py imports app.calling: upward or sideways",
        "platform/bad.py imports app.service: legacy module",
    ]


def test_every_operation_home_is_bundled_in_the_frozen_sidecar():
    """`app.ops` loads homes by name at run time; PyInstaller only bundles what an import statement names."""
    import inspect

    from app import ops

    bundled = set(re.findall(r"import (app\.[a-z_.]+)", inspect.getsource(ops._bundle)))
    assert set(ops.HOMES.values()) <= bundled
    for name, home in ops.HOMES.items():
        assert callable(getattr(ops, name)), f"{home}.{name}"
