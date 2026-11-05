"""A shared, hand-oracle check used by both synthetic variants.

The runner measures whole-process time. This script supplies correctness, not
invented timing numbers. The tiny cases do not establish general performance.
"""

import argparse
import importlib
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", required=True, choices=["baseline", "candidate"])
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    function = importlib.import_module(args.variant).unique_in_order
    cases = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    failed = []
    for index, case in enumerate(cases):
        actual = function(case["input"])
        if actual != case["expected"]:
            failed.append({"case": index, "actual": actual, "expected": case["expected"]})
    result = {
        "correctness": {"status": "failed" if failed else "passed", "checks": len(cases)},
        "metrics": {},
        "failed_cases": failed,
    }
    target = Path(args.result)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps({"variant": args.variant, "checks": len(cases), "failed": len(failed)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
