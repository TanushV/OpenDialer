"""E13-E15: acceptance tooling must reject incomplete, stale or unsafe evidence."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def run_tool(name, *args):
    script = ROOT / "scripts" / name
    assert script.is_file(), f"Acceptance tool contract not implemented: {name}"
    return subprocess.run(  # noqa: S603 - repository-owned acceptance tool and test arguments
        [sys.executable, str(script), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,  # Hang guard only; a loaded parallel run can start interpreters slowly.
        env={**os.environ, "OPENDIALER_COMPLETION_TEST": "1"},
    )


@pytest.mark.parametrize("covered,branches,expected", [(90, 80, 0), (89, 100, 1), (100, 79, 1), (90, 0, 1)])
def test_coverage_receipt_parser(tmp_path, covered, branches, expected):
    reports = []
    for mode in ("sqlite", "postgres"):
        path = tmp_path / f"{mode}.json"
        path.write_text(
            json.dumps(
                {
                    "totals": {
                        "num_statements": 100,
                        "covered_lines": covered,
                        "missing_lines": 100 - covered,
                        "num_branches": 100,
                        "covered_branches": branches,
                        "missing_branches": 100 - branches,
                    }
                }
            )
        )
        reports.append(path)
    result = run_tool("check_completion_coverage.py", "--statements", 90, "--branches", 80, *reports)
    assert (result.returncode != 0) is bool(expected), result.stdout + result.stderr


@pytest.mark.parametrize(
    "content",
    [
        {},
        {"totals": {}},
        {"totals": {"num_statements": 0, "covered_lines": 0, "num_branches": 0, "covered_branches": 0}},
    ],
)
def test_missing_or_empty_coverage_cannot_certify_anything(tmp_path, content):
    path = tmp_path / "missing.json"
    path.write_text(json.dumps(content))
    result = run_tool("check_completion_coverage.py", "--statements", 90, "--branches", 80, path)
    assert result.returncode != 0
    assert (
        "missing" in (result.stdout + result.stderr).lower()
        or "empty" in (result.stdout + result.stderr).lower()
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "dedupe",
        "source_version",
        "internal_suppression",
        "early_voicemail",
        "general_capacity",
        "human_note",
        "future_label",
        "unreviewed_stream",
        "raw_expiry",
        "uncertainty_hold",
    ],
)
def test_targeted_mutation_detection(tmp_path, mutation):
    # A successful mutated run is evidence that the designated assertion failed
    # to catch a regression. An acceptance parser must refuse such a receipt.
    manifest = {
        "schema_version": 1,
        "next_step": "E15",
        "steps": [{"id": "E13", "status": "accepted-software", "receipts": []}],
        "planned_case_groups": [{"id": "C052", "status": "accepted-software", "green_receipts": []}],
        "mutations": [{"id": mutation, "designated_test_failed": False, "test_exit_code": 0}],
    }
    path = tmp_path / "mutations.json"
    path.write_text(json.dumps(manifest))
    result = run_tool(
        "completion_acceptance.py", "--verify-receipts", path, "--output", tmp_path / "result.json"
    )
    assert result.returncode != 0
    assert "accepted" not in json.loads((tmp_path / "result.json").read_text()).get("status", "")


def test_mutation_inventory_names_real_assertions_and_disposable_execution():
    inventory = ROOT / "docs/evidence/completion/mutation-contracts.json"
    data = json.loads(inventory.read_text())
    assert len(data["mutations"]) == 10
    for row in data["mutations"]:
        source = ROOT / row["source"]
        assert source.is_file() and row["find"] in source.read_text()
        assert row["find"] != row["replace"]
        test_file, test_name = row["designated_test"].split("::")
        assert f"def {test_name}(" in (ROOT / test_file).read_text()
    assert data["execution"] == "disposable_copy_only"
    assert data["success_requires"] == "unmutated_pass_and_designated_mutated_assertion_failure"


def ci_workflow():
    import yaml

    source = (ROOT / ".github/workflows/ci.yml").read_text()
    workflow = yaml.safe_load(source)
    # YAML 1.1 reads the bare `on` key as boolean true.
    return source, workflow, workflow.get("on", workflow.get(True))


def pytest_steps(job):
    return [
        step for step in job["steps"] if "uv run pytest services/backend/tests tests" in step.get("run", "")
    ]


def test_ci_enforces_each_database_statement_and_branch_floor():
    source, workflow, triggers = ci_workflow()
    jobs = workflow["jobs"]
    assert {"pull_request", "schedule", "workflow_dispatch", "push"} <= set(triggers)
    full = workflow["env"]["POSTGRES_FULL"]
    for condition in (
        "github.event_name == 'schedule'",
        "github.event_name == 'workflow_dispatch'",
        "github.event_name == 'pull_request' && github.base_ref == 'main'",
    ):
        assert condition in full, (
            "The complete PostgreSQL suite must run for PRs to main, nightly and on demand"
        )
    coverage_runs = {}
    for name in ("backend-sqlite", "backend-postgres"):
        for step in pytest_steps(jobs[name]):
            command = step["run"]
            assert " -n auto" in command, "Backend suites run on parallel workers"
            if "--cov" in command:
                mode = "sqlite" if name == "backend-sqlite" else "postgres"
                assert " -m " not in command, "A coverage floor must be measured over the complete suite"
                assert "--cov=app --cov-branch" in command
                assert f"--cov-report=json:work/remediation/completion/{mode}-coverage.json" in command
                assert f"--junitxml=work/remediation/completion/{mode}-junit.xml" in command
                coverage_runs[mode] = step
    assert set(coverage_runs) == {"sqlite", "postgres"} and source.count("--cov=app --cov-branch") == 2
    assert "if" not in coverage_runs["sqlite"], "The complete SQLite suite runs on every event"
    assert coverage_runs["postgres"]["if"] == "env.POSTGRES_FULL == 'true'"
    postgres = jobs["backend-postgres"]
    assert postgres["services"]["postgres"]["env"]["POSTGRES_DB"] == "opendialer_test"
    assert postgres["env"]["OPENDIALER_TEST_POSTGRES_URL"].endswith("@localhost:5432/opendialer_test")
    (subset,) = [step for step in pytest_steps(postgres) if step is not coverage_runs["postgres"]]
    assert subset["if"] == "env.POSTGRES_FULL != 'true'" and '-m "postgres or database"' in subset["run"]
    gate = jobs["coverage"]
    assert set(gate["needs"]) == {"backend-sqlite", "backend-postgres"}
    (check,) = [s for s in gate["steps"] if "check_completion_coverage.py" in s.get("run", "")]
    assert "check_completion_coverage.py --statements 90 --branches 80" in check["run"]
    assert "reports=(work/remediation/completion/sqlite-coverage.json)" in check["run"]
    assert (
        'if [ "$POSTGRES_FULL" = "true" ]; then reports+=(work/remediation/completion/postgres-coverage.json)'
        in check["run"]
    ), "PostgreSQL floors are enforced whenever its complete suite ran"
    (receipts,) = [
        s for s in gate["steps"] if s.get("with", {}).get("name") == "software-acceptance-receipts"
    ]
    assert receipts["if"] == "always()"
    assert all(job.get("timeout-minutes") for job in jobs.values()), "Every CI job needs a time limit"


def test_historical_receipt_cache_cannot_bypass_final_source_check(tmp_path, monkeypatch):
    from scripts import completion_acceptance as acceptance

    current, old = "a" * 40, "b" * 40
    monkeypatch.setattr(
        acceptance, "tree_digest", lambda commit, roots: "1" * 64 if commit == current else "2" * 64
    )
    receipt = tmp_path / "history.json"
    receipt.write_text(
        json.dumps(
            {
                "source_commit": old,
                "production_tree_digest": "2" * 64,
                "test_tree_digest": "2" * 64,
                "result_counts": {"passed": 1},
                "raw_log_sha256": "3" * 64,
                "external_calls": False,
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "source_commit": current,
                "production_tree_digest": "1" * 64,
                "test_tree_digest": "1" * 64,
                "steps": [
                    {"id": identity, "status": "accepted-software", "receipts": [str(receipt)]}
                    for identity in ("E00", "E13")
                ],
            }
        )
    )
    result = acceptance.verify(manifest)
    assert any(e["code"] == "stale_source" and e["target"] == str(receipt) for e in result["errors"]), (
        "A receipt first seen as history still needs current-source validation at the freeze gate"
    )


def test_bounded_acceptance_tools(tmp_path):
    unsafe = Path.home() / "Library/Application Support/com.opendialer.desktop/local"
    result = run_tool("measure_completion.py", "--profile", "all", "--seed", 20261018, "--output-dir", unsafe)
    assert result.returncode != 0
    assert "installed" in (result.stdout + result.stderr).lower()
    output = tmp_path / "architecture.json"
    result = run_tool("inspect_architecture.py", "--output", output)
    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text())
    assert data["source_hash"]
    assert data["python"]["parse_errors"] == []
    assert data["typescript"]["parse_errors"] == []
    assert data["python"]["files"] >= 80 and data["typescript"]["files"] >= 40
    assert "COMPLETION_PRIVATE_RAW_CANARY" not in output.read_text()


@pytest.mark.parametrize(
    "bad", ["missing_receipt", "stale_source", "external_without_evidence", "cleanup_outside_allowlist"]
)
def test_handoff_manifest_scope(tmp_path, bad):
    manifest = {
        "schema_version": 1,
        "next_step": "complete",
        "source_commit": "0" * 40,
        "production_tree_digest": "0" * 64,
        "steps": [],
        "requirements": [],
        "cleanup": {"allowed": [], "removed": []},
    }
    if bad == "missing_receipt":
        manifest["steps"] = [{"id": "E15", "status": "accepted-software", "receipts": ["missing.json"]}]
    elif bad == "external_without_evidence":
        manifest["requirements"] = [{"id": "X01", "execution_status": "accepted-external", "receipts": []}]
    elif bad == "cleanup_outside_allowlist":
        manifest["cleanup"]["removed"] = ["unrelated-user-service"]
    else:
        receipt = tmp_path / "stale.json"
        receipt.write_text(
            json.dumps(
                {
                    "step_id": "E15",
                    "production_tree_digest": "1" * 64,
                    "test_tree_digest": "2" * 64,
                    "external_calls": False,
                }
            )
        )
        manifest["steps"] = [{"id": "E15", "status": "accepted-software", "receipts": [str(receipt)]}]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    result = run_tool(
        "completion_acceptance.py", "--verify-receipts", path, "--output", tmp_path / "result.json"
    )
    assert result.returncode != 0
    report = json.loads((tmp_path / "result.json").read_text())
    assert report["status"] == "incomplete" and report["errors"]
    assert bad in {e["code"] for e in report["errors"]}, "Each distinct defect needs its own diagnostic"


def test_ci_installs_the_locked_ast_parser_before_backend_acceptance():
    _, workflow, _ = ci_workflow()
    for name in ("backend-sqlite", "backend-postgres"):
        steps = workflow["jobs"][name]["steps"]
        installs = [i for i, s in enumerate(steps) if s.get("run") == "npm ci"]
        suites = [i for i, s in enumerate(steps) if s in pytest_steps(workflow["jobs"][name])]
        assert suites and installs and installs[0] < min(suites), (
            "The backend acceptance includes the TypeScript AST parser and needs locked node_modules",
            name,
        )
        assert all(s.get("working-directory") == "apps/desktop" for s in steps if s.get("run") == "npm ci")
