"""R8.10: prove a frozen trainer really trains, through the same stdin protocol the app uses.

Sends the R8.9 planted-pattern calls to the trainer binary, checks the reply and artifact exactly as the app does
(`app.ml.worker.checked_result`), and serves the artifact with the app's own serving code: the lead with the planted
trigger must score clearly higher. Usage: ``uv run python scripts/trainer_check.py <trainer binary>``.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "services/backend"), str(ROOT / "services/backend/tests")]

from app.ml import training, worker  # noqa: E402
from test_learning_end_to_end import planted  # noqa: E402


def check(trainer):
    with tempfile.TemporaryDirectory(prefix="opendialer-trainer-check-", ignore_cleanup_errors=True) as root:
        output = Path(root).resolve() / "candidate"
        output.mkdir(mode=0o700)
        env = {**worker.child_environment(), "PYINSTALLER_RESET_ENVIRONMENT": "1"}
        env.pop("PYTHONPATH", None)  # The frozen trainer carries its own code.
        reply = subprocess.run(  # noqa: S603 - the trainer binary this check was given
            [str(trainer)],
            input=worker.encode(planted(), output),
            capture_output=True,
            env=env,
            timeout=worker.MAX_SECONDS,
            check=False,
        )
        if reply.returncode:
            raise SystemExit("trainer failed: " + reply.stderr.decode(errors="replace")[-2000:])
        result = worker.checked_result(reply.stdout, output)
        if not result["reviewable"]:
            raise SystemExit(
                "trainer did not learn the planted pattern: " + json.dumps(result.get("bootstrap"))
            )
        base = planted(1)[0]["features"]
        scored = training.predict_funnel_batch(
            result, [{**base, "trigger_type": "hiring_sdrs"}, {**base, "trigger_type": "none"}], Path(root)
        )
        if not scored[0]["p_human"] > scored[1]["p_human"] + 0.2:
            raise SystemExit(f"served model does not rank the planted trigger: {scored}")
        return scored


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    found = check(Path(sys.argv[1]))
    print(
        f"  pass  trainer learned the planted pattern (p_human {found[0]['p_human']:.2f} vs {found[1]['p_human']:.2f})"
    )
