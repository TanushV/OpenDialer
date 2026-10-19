"""Independent statement and branch floors; no combined-percent substitution."""

import argparse
import json
from pathlib import Path


def check(path, statements, branches):
    try:
        totals = json.loads(Path(path).read_text())["totals"]
        values = {}
        for label, numerator, denominator, target in (
            ("statements", "covered_lines", "num_statements", statements),
            ("branches", "covered_branches", "num_branches", branches),
        ):
            covered, total = totals[numerator], totals[denominator]
            if type(total) is not int or type(covered) is not int or not 0 <= covered <= total or total <= 0:
                raise ValueError("empty or invalid coverage counts")
            values[label] = {
                "covered": covered,
                "total": total,
                "percent": 100 * covered / total,
                "required": target,
                "passed": 100 * covered >= target * total,
            }
        return {"file": str(path), "metrics": values, "passed": all(v["passed"] for v in values.values())}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {
            "file": str(path),
            "passed": False,
            "error": f"missing, empty or invalid coverage: {type(exc).__name__}",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--statements", type=float, default=90)
    parser.add_argument("--branches", type=float, default=80)
    parser.add_argument("reports", nargs="+")
    args = parser.parse_args()
    if not all(0 < x <= 100 for x in (args.statements, args.branches)):
        parser.error("Coverage floors must be finite percentages in (0,100]")
    results = [check(p, args.statements, args.branches) for p in args.reports]
    print(json.dumps(results, indent=2))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
