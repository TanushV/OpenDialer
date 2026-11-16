"""Refuse a release unless Verify passed on this exact commit (docs/REBUILD_PLAN.md R0.13a, D21).

GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/name GITHUB_SHA=... python scripts/release_gate.py
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

WORKFLOW = "ci.yml"


def version_allowed(version):
    return bool(re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.[0-9]", version))


def verdict(runs, sha):
    """(allowed, reason) from the Verify runs GitHub lists for this commit."""
    mine = [run for run in runs if run.get("head_sha") == sha]
    if not mine:
        return False, f"No Verify run exists for {sha[:12]}. Push it and let CI finish first."
    if any(run.get("status") == "completed" and run.get("conclusion") == "success" for run in mine):
        return True, f"Verify passed on {sha[:12]}."
    states = sorted({f"{run.get('status')}/{run.get('conclusion') or '-'}" for run in mine})
    return False, f"Verify has not passed on {sha[:12]} ({', '.join(states)}). Fix main before releasing."


def fetch(repository, sha, token):
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/{WORKFLOW}/runs"
        f"?head_sha={sha}&per_page=50"
    )
    request = urllib.request.Request(  # noqa: S310 - fixed GitHub API host
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as reply:  # noqa: S310
        return json.load(reply)["workflow_runs"]


def main():
    manifest = Path(__file__).resolve().parents[1] / "apps/desktop/package.json"
    version = json.loads(manifest.read_text())["version"]
    if not version_allowed(version):
        print("Use a patch from 0 through 9. After .9, bump the minor version and reset the patch to 0.")
        return 1
    sha = os.environ["GITHUB_SHA"]
    allowed, reason = verdict(fetch(os.environ["GITHUB_REPOSITORY"], sha, os.environ["GITHUB_TOKEN"]), sha)
    print(reason)
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
