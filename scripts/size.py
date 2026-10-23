"""Codebase size: lines per area plus Python AST nodes and TypeScript tokens.

.venv/bin/python scripts/size.py [--json]
"""

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AREAS = {
    "backend": ("services/backend/app", "*.py"),
    "backend_tests": ("services/backend/tests", "*.py"),
    "repo_tests": ("tests", "*.py"),
    "frontend": ("apps/desktop/src", "*.ts*"),
    "frontend_tests": ("apps/desktop/tests", "*.ts"),
    "rust": ("apps/desktop/src-tauri/src", "*.rs"),
    "scripts": ("scripts", "*.py"),
    "workflows": (".github/workflows", "*.yml"),
}
TOKEN = re.compile(r"[A-Za-z_$][\w$]*|\d[\w.]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`]*`|\S")


def measure():
    result = {}
    for name, (folder, pattern) in AREAS.items():
        files = [p for p in (ROOT / folder).rglob(pattern) if "__pycache__" not in p.parts]
        texts = [p.read_text() for p in files]
        entry = {"files": len(files), "lines": sum(t.count("\n") for t in texts)}
        if pattern == "*.py":
            entry["ast_nodes"] = sum(sum(1 for _ in ast.walk(ast.parse(t))) for t in texts)
        if pattern.startswith("*.ts"):
            entry["tokens"] = sum(len(TOKEN.findall(t)) for t in texts)
        result[name] = entry
    return result


if __name__ == "__main__":
    sizes = measure()
    if "--json" in sys.argv:
        print(json.dumps(sizes, indent=2))
    else:
        for name, entry in sizes.items():
            print(f"{name:15} " + "  ".join(f"{k}={v:,}" for k, v in entry.items()))
