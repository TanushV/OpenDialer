"""Verify recorded acceptance evidence without converting missing checks into passes."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = [
    "services/backend/app",
    "services/backend/migrations",
    "apps/desktop/src",
    "apps/desktop/src-tauri/src",
]
TESTS = ["services/backend/tests", "apps/desktop/tests", "tests"]
MUTATIONS = {
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
}


def tree_digest(commit, roots):
    git = shutil.which("git")
    if (
        not git
        or not isinstance(commit, str)
        or len(commit) != 40
        or any(c not in "0123456789abcdef" for c in commit)
    ):
        return None
    result = subprocess.run(  # noqa: S603 - validated commit; read-only tracked tree
        [git, "ls-tree", "-r", commit, "--", *roots], cwd=ROOT, capture_output=True, timeout=20, check=False
    )
    return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 and result.stdout else None


def verify(path):
    errors, checked, external = [], [], []

    def error(code, target, detail):
        errors.append({"code": code, "target": str(target), "detail": detail})

    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {
            "status": "incomplete",
            "errors": [{"code": "invalid_manifest", "detail": type(exc).__name__}],
        }
    if not isinstance(manifest, dict):
        return {
            "status": "incomplete",
            "errors": [{"code": "invalid_manifest", "detail": "Expected an object"}],
        }
    steps = {s.get("id"): s for s in manifest.get("steps", [])}
    for identity in [f"E{i:02d}" for i in range(16)]:
        step = steps.get(identity)
        if not step or step.get("status") not in {"accepted-software", "accepted-external"}:
            error("step_open", identity, "No current accepted execution step")
    declared_source = manifest.get("source_commit")
    expected_production = manifest.get("production_tree_digest")
    expected_tests = manifest.get("test_tree_digest")
    if declared_source and tree_digest(declared_source, PRODUCTION) != expected_production:
        error("stale_source", declared_source, "Manifest digest does not match its tracked source")

    cache = {}

    def receipt(reference, owner, *, current=False):
        if not isinstance(reference, str):
            error("missing_receipt", owner, "Receipt must name a saved file")
            return None
        candidate = Path(reference)
        if not candidate.is_absolute():
            candidate = ROOT / candidate if (ROOT / candidate).exists() else path.parent / candidate
        candidate = candidate.resolve()
        if not (
            candidate.is_relative_to(ROOT / "docs/evidence")
            or candidate.is_relative_to(path.parent.resolve())
        ):
            error("receipt_outside_scope", owner, "Receipt is outside the evidence or manifest directory")
            return None
        if candidate in cache:
            return cache[candidate]
        try:
            data = json.loads(candidate.read_text())
            if not isinstance(data, dict):
                raise ValueError("Expected receipt object")
        except (OSError, ValueError):
            error("missing_receipt", owner, reference)
            return None
        cache[candidate] = data
        profiles = data.get("profiles", [data])
        for profile in profiles:
            for key, roots in (("production_tree_digest", PRODUCTION), ("test_tree_digest", TESTS)):
                value = profile.get(key, data.get(key))
                source = profile.get("source_commit", data.get("source_commit"))
                selected_roots = data.get("digest_roots", {}).get(
                    "production" if roots == PRODUCTION else "tests", roots
                )
                if not value or value != tree_digest(source, selected_roots):
                    error("stale_source", reference, f"{key} is missing or disagrees with recorded source")
                expected = expected_production if key == "production_tree_digest" else expected_tests
                if current and expected and value != expected:
                    error("stale_source", reference, f"{key} is not the final accepted source")
            counts = profile.get("result_counts", data.get("result_counts", {}))
            if (
                not isinstance(counts, dict)
                or not counts.get("passed")
                or counts.get("failed", 0)
                or counts.get("errors", 0)
            ):
                error("unproven_result", reference, "Missing passing result counts or a failing run")
            if profile.get("failed_cases", data.get("failed_cases", [])):
                error("unproven_result", reference, "Receipt retains failed cases")
            if not profile.get("raw_log_sha256", data.get("raw_log_sha256")):
                error("missing_log_digest", reference, "No retained raw-log identity")
        if data.get("external_calls") not in (False, []) and not isinstance(data.get("external_calls"), list):
            error("external_without_evidence", reference, "External effects need explicit receipt IDs")
        checked.append({"reference": reference, "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()})
        return data

    for identity, step in steps.items():
        if step.get("status") not in {"accepted-software", "accepted-external"}:
            continue
        references = step.get("receipts", [])
        if not references:
            error("missing_receipt", identity, "Accepted step has no receipts")
        for reference in references:
            receipt(reference, identity, current=identity in {"E13", "E14", "E15"})

    for group in manifest.get("planned_case_groups", []):
        identity = group.get("id")
        if group.get("status") != "accepted-software":
            error("case_open", identity, "Assertion group has not passed")
        references = group.get("green_receipts", [])
        if not group.get("actual_nodes") or not references:
            error("missing_receipt", identity, "Case requires executable assertions and green receipts")
        for reference in references:
            data = receipt(reference, identity)
            if data and identity not in data.get("case_ids", []):
                error("case_not_covered", identity, reference)

    for row in manifest.get("requirements", []):
        identity, status = row.get("id", ""), row.get("execution_status")
        if identity.startswith("X"):
            if status != "accepted-external":
                external.append(identity)
            elif not row.get("receipts"):
                error("external_without_evidence", identity, "External gate has no account/device receipt")
        elif status != "accepted-software":
            error("requirement_open", identity, "Requirement remains unaccepted")
        if status in {"accepted-software", "accepted-external"}:
            if not row.get("receipts"):
                error("missing_receipt", identity, "Accepted requirement has no evidence")
            for reference in row.get("receipts", []):
                receipt(reference, identity)

    mutations = {m.get("id"): m for m in manifest.get("mutations", [])}
    for identity in sorted(MUTATIONS):
        item = mutations.get(identity, {})
        if not (
            item.get("unmutated_exit_code") == 0
            and item.get("test_exit_code") == 1
            and item.get("designated_test_failed") is True
            and item.get("assertion_failure") is True
            and item.get("disposable_copy") is True
            and item.get("raw_log_sha256")
        ):
            error(
                "mutation_unproven",
                identity,
                "Require unmutated pass and the designated mutated assertion failure",
            )
    if manifest.get("independent_review", {}).get("status") != "accepted":
        error("independent_review_missing", "E13", "Author self-review cannot certify independent review")
    cleanup = manifest.get("cleanup", {})
    for removed in cleanup.get("removed", []):
        if removed not in cleanup.get("allowed", []):
            error("cleanup_outside_allowlist", removed, "Removal is not in the task resource allowlist")
    if not manifest.get("steps") or not manifest.get("planned_case_groups"):
        error("traceability_missing", "manifest", "Execution and case inventories are required")
    return {
        "status": "incomplete" if errors else "accepted-software-delivery",
        "errors": errors,
        "checked_receipts": checked,
        "open_external_gates": external,
        "live_acceptance": "unverified" if external else "recorded",
        "external_calls": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.verify_receipts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "errors": len(report["errors"])}))
    return int(bool(report["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
