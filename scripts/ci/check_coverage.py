"""
Per-package coverage floors, so coverage can only go up.

    uv run coverage json -o tmp/ci/coverage.json
    uv run python scripts/ci/check_coverage.py tmp/ci/coverage.json
    uv run python scripts/ci/check_coverage.py --hosted tmp/ci/coverage.json

coverage.py's own ``fail_under`` is one number for everything; this holds
each package to its own floor, the line coverage measured when the floor was
set (rounded down to a whole percent). Raise a floor when coverage rises;
never lower one to make a run pass.

``migrations`` means the helper modules beside ``versions/`` (the revisions
themselves are omitted in ``[tool.coverage.run]``: they run against data,
in ``scripts/ci/replay_chain.sh``).

``--hosted`` holds the GitHub Actions run (``run_all.sh hosted``) to
`HOSTED_FLOORS` instead: a hosted runner has no hpdbldn, so the read-only
tests that cover the market and address-search SQL do not run there and the
api package measures lower. The same rule applies to both sets: raise a
floor when coverage rises, never lower one.
"""

import json
import sys
from pathlib import Path

PACKAGE_ROOT = "src/hpldn/backend/"

# Measured by `uv run pytest --cov=hpldn.backend` with the
# db-write tests skipped (HPLDN_TEST_DB_WRITES unset): run_all.sh's pg and
# hpdbldn stages only add to these.
# Measured: api 89.86 %, guard 83.67 %, migrations 39.68 %, common 69.57 %.
FLOORS = {
    "api": 89,
    "guard": 83,
    "migrations": 39,
    "common": 69,
}

# Measured by `scripts/ci/run_all.sh hosted` in a clean clone (no hpdbldn, no
# GPU, no Tier A inputs; the ephemeral-cluster tests included), 2026-10-31.
# Measured: api 87.19 %, guard 83.68 %, migrations 45.32 %, common 75.85 %.
# common was 74.80 % on the GPU-less GitHub runner (a CUDA workstation also
# runs the cupy paths in common/accel.py and hardware.py); the hermetic
# tests/common/test_db_urls.py added 58 GPU-independent lines (2026-10-31).
HOSTED_FLOORS = {
    "api": 87,
    "guard": 83,
    "migrations": 45,
    "common": 75,
}


def package_coverage(report: dict, floors: dict = FLOORS) -> dict:
    """Return ``{package: (covered lines, statements)}`` for the packages in `floors`."""
    totals = {name: [0, 0] for name in floors}
    for path, data in report["files"].items():
        relative = path.split(PACKAGE_ROOT, 1)[-1]
        package = relative.split("/", 1)[0]
        if package in totals:
            summary = data["summary"]
            totals[package][0] += summary["covered_lines"]
            totals[package][1] += summary["num_statements"]
    return {name: tuple(value) for name, value in totals.items()}


def main(argv: list[str]) -> int:
    args = argv[1:]
    floors = FLOORS
    if "--hosted" in args:
        args.remove("--hosted")
        floors = HOSTED_FLOORS
    report = json.loads(Path(args[0] if args else "tmp/ci/coverage.json").read_text())
    failed = False
    for package, (covered, statements) in package_coverage(report, floors).items():
        percent = 100.0 * covered / statements if statements else 0.0
        floor = floors[package]
        status = "ok" if percent >= floor else "BELOW FLOOR"
        failed |= percent < floor
        print(f"{package:<12} {percent:6.2f}%  (floor {floor}%, {covered}/{statements} lines)  {status}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
