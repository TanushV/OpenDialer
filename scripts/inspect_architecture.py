"""Source-only ownership inventory using Python AST and the locked TypeScript parser."""

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = ("services/backend/app", "apps/desktop/src", "apps/desktop/src-tauri/src")
LOCKS = (
    "pyproject.toml",
    "uv.lock",
    "apps/desktop/package.json",
    "apps/desktop/package-lock.json",
    "apps/desktop/src-tauri/Cargo.toml",
    "apps/desktop/src-tauri/Cargo.lock",
)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def source_files():
    return sorted(
        p
        for prefix in SOURCE_ROOTS
        for p in (ROOT / prefix).rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".ts", ".tsx", ".rs"}
        and not any(part in {"tests", "__pycache__"} for part in p.parts)
        and not p.name.endswith((".test.ts", ".test.tsx"))
    )


def digest(paths):
    value = hashlib.sha256()
    for path in sorted(paths):
        value.update(str(path.relative_to(ROOT)).encode() + b"\0")
        value.update(hashlib.sha256(path.read_bytes()).digest())
    return value.hexdigest()


def own_nodes(root):
    yield root
    for child in ast.iter_child_nodes(root):
        if isinstance(child, (*FUNCTIONS, ast.ClassDef)):
            continue
        yield from own_nodes(child)


def python_inventory(paths):
    output = {"files": len(paths), "lines": 0, "parse_errors": [], "functions": [], "imports": []}
    for path in paths:
        source = path.read_text()
        relative = str(path.relative_to(ROOT))
        output["lines"] += len(source.splitlines())
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            output["parse_errors"].append({"file": relative, "line": exc.lineno, "error": exc.msg})
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                output["imports"].extend({"file": relative, "module": item.name} for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                output["imports"].append({"file": relative, "module": "." * node.level + (node.module or "")})
            if not isinstance(node, FUNCTIONS):
                continue
            children = list(own_nodes(node))
            nested_lines = set()
            for child in ast.walk(node):
                if child is not node and isinstance(child, (*FUNCTIONS, ast.ClassDef)):
                    nested_lines.update(range(child.lineno, child.end_lineno + 1))
            output["functions"].append(
                {
                    "file": relative,
                    "name": getattr(node, "name", "<lambda>"),
                    "line": node.lineno,
                    "end_line": node.end_lineno,
                    "own_lines": node.end_lineno - node.lineno + 1 - len(nested_lines),
                    "branches": sum(
                        isinstance(n, (ast.If, ast.IfExp, ast.ExceptHandler, ast.Match)) for n in children
                    )
                    + sum(len(n.values) - 1 for n in children if isinstance(n, ast.BoolOp)),
                    "loops": sum(
                        isinstance(n, (ast.For, ast.AsyncFor, ast.While, ast.comprehension)) for n in children
                    ),
                    "calls": sorted({ast.unparse(n.func) for n in children if isinstance(n, ast.Call)}),
                }
            )
    return output


TS_PROGRAM = r"""
const fs = require('fs');
const ts = require('./apps/desktop/node_modules/typescript');
const paths = JSON.parse(fs.readFileSync(0, 'utf8'));
const output = {files: paths.length, lines: 0, parse_errors: [], functions: [], imports: [], compiler_version: ts.version};
const isFunction = n => ts.isFunctionDeclaration(n) || ts.isFunctionExpression(n) || ts.isArrowFunction(n) || ts.isMethodDeclaration(n) || ts.isGetAccessor(n) || ts.isSetAccessor(n) || ts.isConstructorDeclaration(n);
for (const file of paths) {
  const text = fs.readFileSync(file, 'utf8');
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const line = pos => source.getLineAndCharacterOfPosition(pos).line + 1;
  output.lines += text.split(/\r?\n/).length - (text.endsWith('\n') ? 1 : 0);
  for (const d of source.parseDiagnostics) output.parse_errors.push({file, line: line(d.start || 0), error: ts.flattenDiagnosticMessageText(d.messageText, '\n')});
  function visit(node) {
    if (ts.isImportDeclaration(node)) output.imports.push({file, module: node.moduleSpecifier.text});
    if (isFunction(node) && node.body) {
      const record = {file, name: node.name?.getText(source) || node.parent?.name?.getText(source) || '<anonymous>', line: line(node.getStart(source)), end_line: line(node.end), branches: 0, loops: 0, calls: []};
      const excluded = new Set();
      function own(n) {
        if (n !== node && (isFunction(n) || ts.isClassDeclaration(n) || ts.isClassExpression(n))) {
          for (let i = line(n.getStart(source)); i <= line(n.end); i++) excluded.add(i);
          return;
        }
        if (ts.isIfStatement(n) || ts.isConditionalExpression(n) || ts.isCaseClause(n) || ts.isCatchClause(n)) record.branches++;
        if (ts.isBinaryExpression(n) && [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken].includes(n.operatorToken.kind)) record.branches++;
        if (ts.isForStatement(n) || ts.isForOfStatement(n) || ts.isForInStatement(n) || ts.isWhileStatement(n) || ts.isDoStatement(n)) record.loops++;
        if (ts.isCallExpression(n)) record.calls.push(n.expression.getText(source));
        ts.forEachChild(n, own);
      }
      own(node);
      record.own_lines = Math.max(0, record.end_line - record.line + 1 - excluded.size);
      record.calls = [...new Set(record.calls)].sort();
      output.functions.push(record);
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
}
process.stdout.write(JSON.stringify(output));
"""


def inspect(baseline):
    paths = source_files()
    python = python_inventory([p for p in paths if p.suffix == ".py"])
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node and locked desktop TypeScript dependencies are required")
    result = subprocess.run(  # noqa: S603 - repository-owned parser; source is parsed, never executed
        [node, "-e", TS_PROGRAM],
        input=json.dumps([str(p.relative_to(ROOT)) for p in paths if p.suffix in {".ts", ".tsx"}]),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=25,
    )
    typescript = json.loads(result.stdout)
    dependencies = []
    git = shutil.which("git")
    for name in LOCKS:
        current = (ROOT / name).read_bytes()
        before = (
            subprocess.run(  # noqa: S603 - read-only Git revision selected by operator
                [git, "show", f"{baseline}:{name}"], cwd=ROOT, capture_output=True, check=False, timeout=10
            )
            if git
            else None
        )
        dependencies.append(
            {
                "file": name,
                "sha256": hashlib.sha256(current).hexdigest(),
                "baseline_sha256": hashlib.sha256(before.stdout).hexdigest()
                if before and before.returncode == 0
                else None,
                "changed": current != before.stdout if before and before.returncode == 0 else None,
            }
        )
    return {
        "source_hash": digest(paths),
        "source_roots": SOURCE_ROOTS,
        "baseline": baseline,
        "method": "Structural indicators, not certified cyclomatic complexity. Own bodies exclude nested definitions; calls are syntactic references, not dynamic dispatch proof.",
        "python": python,
        "typescript": typescript,
        "dependencies": dependencies,
        "rust": {
            "files": sum(p.suffix == ".rs" for p in paths),
            "validation": "cargo test and clippy receipts required separately",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", default="63f4d4242f60672777f596177fe98fe57f715bf9")
    args = parser.parse_args()
    report = inspect(args.baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "source_hash": report["source_hash"],
                "files": {k: report[k]["files"] for k in ("python", "typescript")},
            }
        )
    )
    return int(bool(report["python"]["parse_errors"] or report["typescript"]["parse_errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
