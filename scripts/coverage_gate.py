#!/usr/bin/env python3
"""Coverage ratchet.

Keeps line coverage monotonically non-decreasing over time without imposing a
fixed minimum:

  * fail if current coverage is below the recorded baseline (a regression), or
  * fail if it is above the baseline, asking for the baseline to be raised so
    the gain is locked in.

Equal coverage passes. Usage:

    coverage_gate.py <current-percent> <baseline-file> [docker-skip-file]

When docker-skip-file exists and contains a non-zero count, the ratchet is
skipped (Docker-backed tests were not run, so coverage is expected to drop).
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 3 or len(argv) > 4:
        print(
            "usage: coverage_gate.py <current-percent> <baseline-file> [docker-skip-file]",
            file=sys.stderr,
        )
        return 2
    current = round(float(argv[1]), 2)
    baseline_file = Path(argv[2])
    baseline = round(float(baseline_file.read_text().strip()), 2)

    docker_skip_file = Path(argv[3]) if len(argv) == 4 else None
    docker_skips = 0
    if docker_skip_file and docker_skip_file.exists():
        try:
            docker_skips = int(docker_skip_file.read_text().strip())
        except ValueError:
            pass

    if docker_skips > 0:
        print(f"Coverage {current}% (baseline {baseline}%).")
        print(
            f"coverage ratchet skipped: {docker_skips} tests skipped "
            "(Docker unavailable); full ratchet runs in CI"
        )
        return 0

    if current < baseline:
        print(f"::error::Coverage regressed: {current}% < baseline {baseline}%")
        return 1
    if current > baseline:
        print(
            f"::error::Coverage rose to {current}% (baseline {baseline}%). "
            f"Update {baseline_file} to {current} to lock in the gain."
        )
        return 1
    print(f"Coverage {current}% meets baseline {baseline}%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
