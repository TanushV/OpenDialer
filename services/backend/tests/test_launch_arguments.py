"""The desktop shell's sidecar arguments are ones the backend accepts.

Regression: R2.1 removed `--mode` from the backend while `lib.rs` still passed `--mode local`, so the packaged backend
would exit on launch. The package smoke test started the sidecar with its own arguments and missed it.
"""

import re
from datetime import timedelta
from pathlib import Path

from app import cli
from app.cli import parser

LIB = Path(__file__).resolve().parents[3] / "apps/desktop/src-tauri/src/lib.rs"


def shell_arguments():
    source = LIB.read_text()
    block = source[source.index("let mut args = vec![") :]
    block = block[: block.index("];")]
    pushed = re.findall(r'args\.push\("([^"]*)"\.into\(\)\)', source)
    return [match.group(1) for match in re.finditer(r'"([^"]*)"', block)] + pushed


def test_the_shell_passes_only_arguments_the_backend_accepts():
    args = shell_arguments()
    assert args[0] == "serve" and "--data-dir" in args
    # The port and data folder are computed in Rust; give each a stand-in value.
    filled = []
    for arg in args:
        filled += [arg, *{"--port": ["1"], "--data-dir": ["data"], "--migrate-from": ["old"]}.get(arg, [])]
    parsed = parser().parse_args(filled)
    assert parsed.command == "serve"
    assert {"--migrate-from", "--simulation"} <= set(args)


def test_simulation_clock_advances_for_delayed_helper_jobs(monkeypatch):
    ticks = iter((50.0, 50.0, 51.25))
    monkeypatch.setattr(cli, "monotonic", lambda: next(ticks))
    clock = cli.simulation_clock()
    start = clock()
    assert clock() - start == timedelta(seconds=1.25)
