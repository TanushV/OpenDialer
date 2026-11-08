"""Code-rule bans and size caps (docs/REBUILD_PLAN.md section 4 rules 4 to 7 and 10, section 12).

uv run python scripts/rules.py            report; fails only on a hit inside the strict packages (R3.10)
uv run python scripts/rules.py --strict   fail on any ban hit or cap overrun anywhere

Under D24 the rules bind code as it moves: every ban and the per-file and per-function caps fail in the new packages
now; the legacy modules and the whole-app caps stay warnings until the phases that rewrite them.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "services/backend/app"
# The section 4 packages. Bans on new core code apply here as each package appears.
CORE = ["platform", "calling", "leads", "queue", "numbers", "learning", "prep", "helper", "aftercall"]
# Packages where a hit fails the check today. `platform` holds the document store, which reads `.data` by design, and
# still carries `tenant_id` and the simulation setting until R3.4 and R4.7; there only the silent-except ban binds.
STRICT = [p for p in CORE if p != "platform"]
PLATFORM_STRICT_BANS = {"silent except Exception"}
# Rule 6: the carrier connection is the only fake.
SIMULATION_ALLOWED = {"providers/replay_carrier.py", "providers/factory.py"}
CAPS = {
    "backend lines": 18_000,
    "frontend lines (with CSS)": 7_500,
    "test lines": 12_000,
    "tables": 30,
    "routes": 75,
    "largest function": 120,
    "largest file": 700,
}
# Rule 10 allows a short named list of longer functions; each entry needs a reason.
FUNCTION_EXCEPTIONS: dict[str, str] = {}
ROUTE = re.compile(r"@app\.(get|post|put|patch|delete|websocket)\(")


def python_files(folder):
    return [p for p in folder.rglob("*.py") if "__pycache__" not in p.parts]


def lines(paths):
    return sum(p.read_text().count("\n") for p in paths)


def silent_broad_excepts(text):
    """Line numbers of `except Exception` blocks that neither log nor raise (rule 5)."""
    found = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        names = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        if not any(isinstance(n, ast.Name) and n.id in {"Exception", "BaseException"} for n in names):
            continue
        body = ast.Module(body=node.body, type_ignores=[])
        raises = any(isinstance(n, ast.Raise) for n in ast.walk(body))
        logs = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"debug", "info", "warning", "error", "exception", "critical", "log"}
            for n in ast.walk(body)
        )
        if not raises and not logs:
            found.append(node.lineno)
    return found


FUNCTION = ast.FunctionDef | ast.AsyncFunctionDef


def _nested(node):
    """Functions defined directly inside `node`, not inside those."""
    found, stack = [], list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, FUNCTION):
            found.append(child)
        else:
            stack.extend(ast.iter_child_nodes(child))
    return found


def function_sizes(text):
    """(name, own length in lines) for every function; nested functions count on their own."""
    return [
        (
            node.name,
            node.end_lineno - node.lineno + 1 - sum(n.end_lineno - n.lineno + 1 for n in _nested(node)),
        )
        for node in ast.walk(ast.parse(text))
        if isinstance(node, FUNCTION)
    ]


def bans():
    hits = {
        "silent except Exception": [],
        ".data.get( in core": [],
        "simulation outside the replay carrier": [],
    }
    hits["tenant_id in core"] = []
    for path in python_files(APP):
        rel = path.relative_to(APP).as_posix()
        text = path.read_text()
        hits["silent except Exception"] += [f"{rel}:{n}" for n in silent_broad_excepts(text)]
        in_core = rel.split("/")[0] in CORE
        for number, line in enumerate(text.splitlines(), 1):
            if in_core and ".data.get(" in line:
                hits[".data.get( in core"].append(f"{rel}:{number}")
            if in_core and "tenant_id" in line:
                hits["tenant_id in core"].append(f"{rel}:{number}")
            if rel not in SIMULATION_ALLOWED and re.search(r"\bsimulation\b|\bsimulated\b", line):
                hits["simulation outside the replay carrier"].append(f"{rel}:{number}")
    return hits


def measures():
    sys.path.insert(0, str(ROOT / "services/backend"))
    from app.platform.db import Base

    backend = python_files(APP)
    frontend = [*(ROOT / "apps/desktop/src").rglob("*.ts*"), *(ROOT / "apps/desktop/src").rglob("*.css")]
    tests = [
        *python_files(ROOT / "services/backend/tests"),
        *python_files(ROOT / "tests"),
        *(ROOT / "apps/desktop/tests").rglob("*.ts"),
    ]
    functions = [
        (f"{p.relative_to(APP).as_posix()}:{name}", size)
        for p in backend
        for name, size in function_sizes(p.read_text())
        if f"{p.relative_to(APP).as_posix()}:{name}" not in FUNCTION_EXCEPTIONS
    ]
    longest = max(functions, key=lambda item: item[1])
    biggest = max(((p, p.read_text().count("\n")) for p in [*backend, *frontend]), key=lambda item: item[1])
    return {
        "backend lines": (lines(backend), ""),
        "frontend lines (with CSS)": (lines(frontend), ""),
        "test lines": (lines(tests), ""),
        "tables": (len(Base.metadata.tables), ""),
        "routes": (sum(len(ROUTE.findall(p.read_text())) for p in backend), ""),
        "largest function": (longest[1], longest[0]),
        "largest file": (biggest[1], biggest[0].relative_to(ROOT).as_posix()),
    }


def strict_hits(hits, sizes):
    """Ban hits and per-file or per-function cap overruns inside the packages that are strict today."""
    found = []
    for name, places in hits.items():
        for place in places:
            package = place.split("/")[0]
            if package in STRICT or (package == "platform" and name in PLATFORM_STRICT_BANS):
                found.append(f"{name}: {place}")
    for place, size, cap in sizes:
        if place.split("/")[0] in STRICT and size > cap:
            found.append(f"{size} lines over {cap}: {place}")
    return found


def core_sizes():
    sizes = []
    for package in STRICT:
        for path in python_files(APP / package):
            rel = path.relative_to(APP).as_posix()
            text = path.read_text()
            sizes.append((rel, text.count("\n"), CAPS["largest file"]))
            sizes += [
                (f"{rel}:{name}", size, CAPS["largest function"])
                for name, size in function_sizes(text)
                if f"{rel}:{name}" not in FUNCTION_EXCEPTIONS
            ]
    return sizes


def main(argv):
    strict = "--strict" in argv
    failed = False
    print("Size caps (section 12)")
    for name, (value, detail) in measures().items():
        over = value > CAPS[name]
        failed |= over
        print(f"  {'OVER' if over else 'ok':4}  {name:26} {value:>7,} / {CAPS[name]:,}  {detail}")
    print("Code-rule bans (section 4)")
    hits = bans()
    for name, found in hits.items():
        failed |= bool(found)
        print(f"  {'HIT' if found else 'ok':4}  {name:38} {len(found):>5}  {', '.join(found[:3])}")
    binding = strict_hits(hits, core_sizes())
    for line in binding:
        print(f"  FAIL  {line}")
    if failed and not strict and not binding:
        print("Legacy hits and whole-app caps are warnings until the phases that rewrite that code (D24).")
    return 1 if binding or (failed and strict) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
