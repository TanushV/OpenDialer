"""Refuse a release unless Verify passed on this exact commit (docs/REBUILD_PLAN.md R0.13a, D21).

GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/name GITHUB_SHA=... python scripts/release_gate.py
"""

import json
import os
import sys
import urllib.request

WORKFLOW = "ci.yml"


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
    sha = os.environ["GITHUB_SHA"]
    allowed, reason = verdict(fetch(os.environ["GITHUB_REPOSITORY"], sha, os.environ["GITHUB_TOKEN"]), sha)
    print(reason)
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
