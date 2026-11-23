"""Seed selection and failure traces for the randomized calling tests."""

import json
import os
import shlex
from pathlib import Path
from threading import Lock


def soak_count(name, default):
    value = int(os.environ.get(name, default))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def soak_seeds(count_var, default, pinned=()):
    selected = os.environ.get("SOAK_SEED")
    if selected is not None:
        return [int(selected)]
    start = int(os.environ.get("SOAK_SEED_START", "0"))
    return list(dict.fromkeys((*pinned, *range(start, start + soak_count(count_var, default)))))


def ledger_totals(path):
    """Per test: how many runs passed, and for each effect its total count and how many runs reached it."""
    totals = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = json.loads(raw)
        test = totals.setdefault(line["test"], {"runs": 0, "effects": {}})
        test["runs"] += 1
        for name, count in line["coverage"].items():
            effect = test["effects"].setdefault(name, {"total": 0, "runs": 0})
            effect["total"] += count
            effect["runs"] += bool(count)
    return totals


class StressTrace:
    """Keep an ordered trace of synthetic actions and effects on a failed run."""

    def __init__(self, tmp_path, nodeid, seed, **replay_env):
        self.tmp_path, self.nodeid = tmp_path, nodeid
        self.seed = seed
        self.replay_env = {"SOAK_SEED": seed, **replay_env}
        self.events = []
        self._lock = Lock()

    def record(self, action, **facts):
        with self._lock:
            self.events.append({"step": len(self.events) + 1, "action": action, **facts})

    def __enter__(self):
        return self

    def _ledger(self):
        """With SOAK_LEDGER set, a passing run appends what it reached, so a sweep cannot pass vacuously unseen."""
        path = os.environ.get("SOAK_LEDGER")
        if not path:
            return
        last = next((event for event in reversed(self.events) if event["action"] == "coverage"), {})
        coverage = {name: value for name, value in last.items() if name not in ("step", "action")}
        test = self.nodeid.split("::")[-1].split("[")[0]
        with open(path, "a", encoding="utf-8") as stream:  # One short append per run, safe across workers.
            stream.write(json.dumps({"test": test, "seed": self.seed, "coverage": coverage}) + "\n")

    def __exit__(self, _type, error, _traceback):
        if error is None:
            self._ledger()
            return
        destination = self.tmp_path
        if artifact_dir := os.environ.get("SOAK_ARTIFACTS"):
            destination = Path(artifact_dir)
        case = self.nodeid.split("::")[-1].replace("[", "-").replace("]", "")
        path = destination / f"{case}.json"
        if os.name == "nt":
            variables = " ".join(f"$env:{name} = '{value}';" for name, value in self.replay_env.items())
            target = "'" + self.nodeid.replace("'", "''") + "'"
        else:
            variables = " ".join(
                f"{name}={shlex.quote(str(value))}" for name, value in self.replay_env.items()
            )
            target = shlex.quote(self.nodeid)
        replay = f"{variables} uv run pytest -x -q {target}"
        failure = {"type": type(error).__name__, "message": str(error)}
        report = {
            "seed": self.seed,
            "test": self.nodeid,
            "replay_env": self.replay_env,
            "replay": replay,
            "failure": failure,
            "events": self.events,
        }
        try:
            destination.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            artifact = f"Trace: {path}"
        except OSError as trace_error:
            artifact = f"Trace could not be written: {trace_error}"
        recent = "\n".join(json.dumps(event) for event in self.events[-12:])
        error.add_note(f"CALLING STRESS FAILURE (seed {self.seed})\nReplay: {replay}\n{artifact}\n{recent}")
