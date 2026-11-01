"""
The fit-strategy gate and sweep for `hpp_cb`.

A faster fit strategy is adopted only if the backtest's scoped MdAPE moves by
at most `MAX_MDAPE_DELTA_PP` percentage points against the baseline run::

    # one gate decision from two `hpp_cb --backtest --results-json` files
    python -m hpldn.backend.scripts.cb_fit_gate gate baseline.json candidate.json

    # run the baseline and every candidate, then gate them (hours: one backtest each)
    python -m hpldn.backend.scripts.cb_fit_gate sweep --out tmp/sweeps/fit_gate -- --iterations 20000
    python -m hpldn.backend.scripts.cb_fit_gate sweep --dry-run

The candidates (`CANDIDATES`) are the CTR sweep (``--max-ctr-complexity`` /
``--one-hot-max-size``, which also shrinks the model), ``--fold-refit
final-only`` and ``--quantize-once``. Each run's output is streamed to
``<out>/<name>.log`` and writes ``<name>.results.json`` and
``<name>.profile.json`` (`common.profiling`); the sweep writes
``summary.json`` with every verdict, the wall-time ratio and, with
``--save-models``, the ``model.cbm`` size.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

MAX_MDAPE_DELTA_PP = 0.1

# name -> extra hpp_cb arguments.
CANDIDATES: Dict[str, List[str]] = {
    "ctr1": ["--max-ctr-complexity", "1"],
    "ctr2": ["--max-ctr-complexity", "2"],
    "ctr1_onehot10": ["--max-ctr-complexity", "1", "--one-hot-max-size", "10"],
    "ctr2_onehot10": ["--max-ctr-complexity", "2", "--one-hot-max-size", "10"],
    "fold_refit_final_only": ["--fold-refit", "final-only"],
    "quantize_once": ["--quantize-once"],
}
BASELINE = "baseline"


def scoped_mdape(results: Mapping) -> float:
    """Return a backtest's scoped MdAPE (weighted mean over the folds), as a fraction."""
    try:
        return float(results["aggregate_scoped"]["mdape"]["weighted_mean"])
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "not a --backtest results file: no aggregate_scoped.mdape.weighted_mean "
            "(run hpp_cb --backtest --results-json)"
        ) from exc


def fit_gate(baseline: Mapping, candidate: Mapping, max_delta_pp: float = MAX_MDAPE_DELTA_PP) -> Dict[str, object]:
    """
    Return the gate decision for `candidate` against `baseline` (both backtest results).

    ``passed`` is True when the candidate's scoped MdAPE is at most
    `max_delta_pp` percentage points above the baseline's; an improvement
    always passes.
    """
    base, cand = scoped_mdape(baseline), scoped_mdape(candidate)
    delta_pp = (cand - base) * 100.0
    return {
        "baseline_mdape": base,
        "candidate_mdape": cand,
        "delta_pp": round(delta_pp, 4),
        "max_delta_pp": max_delta_pp,
        "passed": delta_pp <= max_delta_pp,
    }


def _wall_s(profile_path: Path) -> Optional[float]:
    try:
        return float(json.loads(profile_path.read_text())["wall_s"])
    except (OSError, KeyError, ValueError):
        return None


def sweep_commands(out: Path, extra: Sequence[str], names: Sequence[str], save_models: bool = False) -> Dict[str, List[str]]:
    """Return the ``hpp_cb`` command line of the baseline and each named candidate."""
    commands = {}
    for name in [BASELINE, *names]:
        command = [
            sys.executable,
            "-m",
            "hpldn.backend.scripts.hpp_cb",
            "--backtest",
            *extra,
            *(CANDIDATES.get(name, [])),
            "--results-json",
            str(out / f"{name}.results.json"),
            "--profile-json",
            str(out / f"{name}.profile.json"),
        ]
        if save_models:
            command += ["--model-out", str(out / "models" / name)]
        commands[name] = command
    return commands


def run_sweep(
    out: Path, extra: Sequence[str], names: Sequence[str], save_models: bool = False, dry_run: bool = False
) -> Dict[str, object]:
    """Run the baseline and the candidates one after another (logs streamed per run), then gate each."""
    out.mkdir(parents=True, exist_ok=True)
    commands = sweep_commands(out, extra, names, save_models)
    if dry_run:
        for name, command in commands.items():
            print(f"{name}: {' '.join(command)} > {out / (name + '.log')} 2>&1")
        return {"dry_run": True, "commands": commands}
    for name, command in commands.items():
        results_path = out / f"{name}.results.json"
        if results_path.exists():
            print(f"[sweep] {name}: {results_path} exists, not re-run", flush=True)
            continue
        print(f"[sweep] {name}: running, log {out / (name + '.log')}", flush=True)
        started = time.time()
        with open(out / f"{name}.log", "w") as log_file:
            code = subprocess.call(command, stdout=log_file, stderr=subprocess.STDOUT)
        print(f"[sweep] {name}: exit {code} after {time.time() - started:,.0f} s", flush=True)
    baseline_path = out / f"{BASELINE}.results.json"
    baseline = json.loads(baseline_path.read_text())
    base_wall = _wall_s(out / f"{BASELINE}.profile.json")
    summary: Dict[str, object] = {"baseline": {"scoped_mdape": scoped_mdape(baseline), "wall_s": base_wall}, "candidates": {}}
    for name in names:
        path = out / f"{name}.results.json"
        if not path.exists():
            summary["candidates"][name] = {"error": f"no {path.name}; see {name}.log"}
            continue
        verdict = fit_gate(baseline, json.loads(path.read_text()))
        wall = _wall_s(out / f"{name}.profile.json")
        verdict["wall_s"] = wall
        verdict["speedup"] = round(base_wall / wall, 2) if base_wall and wall else None
        model = out / "models" / name / "model.cbm"
        if model.exists():
            verdict["model_mib"] = round(model.stat().st_size / 2**20, 1)
        summary["candidates"][name] = verdict
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("gate", help="Gate one candidate results file against a baseline")
    gate.add_argument("baseline", type=Path)
    gate.add_argument("candidate", type=Path)
    gate.add_argument("--max-delta-pp", type=float, default=MAX_MDAPE_DELTA_PP)
    sweep = commands.add_parser("sweep", help="Run the baseline and candidates, then gate them")
    sweep.add_argument("--out", type=Path, default=Path("tmp/sweeps/fit_gate"))
    sweep.add_argument("--candidates", default=",".join(CANDIDATES), help="Comma-separated names from CANDIDATES")
    sweep.add_argument("--save-models", action="store_true", help="Also save each model, to report its size")
    sweep.add_argument("--dry-run", action="store_true", help="Print the commands only")
    sweep.add_argument("extra", nargs=argparse.REMAINDER, help="hpp_cb arguments every run shares (after --)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "gate":
        verdict = fit_gate(
            json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text()), args.max_delta_pp
        )
        print(json.dumps(verdict, indent=2))
        return 0 if verdict["passed"] else 1
    names = [n.strip() for n in args.candidates.split(",") if n.strip()]
    unknown = sorted(set(names) - set(CANDIDATES))
    if unknown:
        raise SystemExit(f"Unknown candidate(s) {unknown}; expected some of {sorted(CANDIDATES)}")
    extra = [a for a in args.extra if a != "--"]
    summary = run_sweep(args.out, extra, names, save_models=args.save_models, dry_run=args.dry_run)
    if not args.dry_run:
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
