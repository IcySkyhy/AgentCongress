from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from pathlib import Path


BASELINE = 147734
THRESHOLDS = (147734, 18532, 2164, 1790, 1579, 1548, 1487, 1363)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: anthropic_performance.py WORKTREE")
    worktree = Path(sys.argv[1]).resolve()
    results: list[int] = []
    diagnostics: list[dict[str, object]] = []
    for seed in (101, 202, 303):
        code = (
            "import random, runpy, sys; "
            "sys.path[:0]=['tests', '.']; "
            f"random.seed({seed}); "
            "sys.argv=['tests/submission_tests.py', 'SpeedTests.test_kernel_speedup']; "
            "runpy.run_path('tests/submission_tests.py', run_name='__main__')"
        )
        run = subprocess.run([sys.executable, "-c", code], cwd=worktree, capture_output=True, text=True, timeout=120)
        output = run.stdout + run.stderr
        values = [int(value) for value in re.findall(r"CYCLES:\s*(\d+)", output)]
        if values:
            results.append(values[-1])
        diagnostics.append({"seed": seed, "returncode": run.returncode, "cycle_observed": values[-1] if values else None})
    valid = len(results) == 3
    value = statistics.median(results) if valid else None
    print(json.dumps({"valid": valid, "value": value, "direction": "lower", "baseline": BASELINE, "success_value": 18531, "thresholds_passed": sum(value < threshold for threshold in THRESHOLDS) if value is not None else 0, "seed_results": results, "diagnostics": diagnostics}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
