#!/usr/bin/env python3
"""Summarise repeated benchmark runs, so the headline numbers carry a spread.

Every figure in `REPORT.md` comes from a single run, and a single run of a
sampling process is an anecdote. This reads the canonical `logs/run_log.json`
plus any `logs/repeats/run_*.json` and reports each rate across runs.

    python benchmark.py --out logs/repeats/run_1.json     # collect one
    python repeat_summary.py                              # summarise them all

The raw repeat logs are large and gitignored; the small summary this writes
to `logs/repeats/summary.json` is committed, because it is what the README
quotes.
"""

import argparse
import glob
import json
import statistics
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
PRICE_IN_PER_MTOK = 2.00
PRICE_OUT_PER_MTOK = 10.00


def summarise_run(path: Path) -> dict:
    """The few numbers worth comparing across runs, from one run log."""
    run = json.loads(path.read_text(encoding="utf-8"))
    results = run["results"]
    usage = run.get("api_usage") or {}
    cost = (usage.get("input_tokens", 0) / 1e6 * PRICE_IN_PER_MTOK
            + usage.get("output_tokens", 0) / 1e6 * PRICE_OUT_PER_MTOK)
    spec_known = [r for r in results if (r.get("spec_check") or {}).get("pass") is not None]
    return {
        "file": path.name,
        "circuits": len(results),
        "simulated": sum(1 for r in results if r["success"]),
        "spec_checked": len(spec_known),
        "spec_passed": sum(1 for r in spec_known if r["spec_check"]["pass"]),
        "attempts": sum(r["attempts_used"] for r in results),
        "seconds": round(sum(r["total_seconds"] for r in results), 1),
        "api_calls": usage.get("calls", 0),
        "cost_usd": round(cost, 4),
        # Which circuits failed their spec: the interesting part is whether
        # it is the *same* ones each time.
        "spec_failures": sorted(r["id"] for r in spec_known
                                if not r["spec_check"]["pass"]),
    }


def spread(values: list) -> str:
    """`n/n` when every run agreed, else the range."""
    if not values:
        return "-"
    lo, hi = min(values), max(values)
    if lo == hi:
        return f"{lo}"
    return f"{lo}-{hi} (mean {statistics.mean(values):.1f})"


def collect(paths) -> dict:
    runs = [summarise_run(p) for p in paths]
    if not runs:
        return {"runs": []}
    n = runs[0]["circuits"]
    sim = [r["simulated"] for r in runs]
    spec = [r["spec_passed"] for r in runs]
    failure_sets = {tuple(r["spec_failures"]) for r in runs}
    return {
        "n_runs": len(runs),
        "circuits": n,
        "simulated": spread(sim),
        "spec_passed": spread(spec),
        "attempts": spread([r["attempts"] for r in runs]),
        "total_cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
        # The stability question: same circuits failing spec every time, or
        # a different set each run?
        "same_circuits_fail_each_run": len(failure_sets) == 1,
        "spec_failure_sets": sorted(list(s) for s in failure_sets),
        "runs": runs,
    }


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="repeat-summary", description="Spread of the benchmark across repeat runs.")
    ap.add_argument("--write", action="store_true",
                    help="write logs/repeats/summary.json")
    args = ap.parse_args(argv)

    paths = []
    canonical = LOG_DIR / "run_log.json"
    if canonical.exists():
        paths.append(canonical)
    paths += [Path(p) for p in sorted(glob.glob(str(LOG_DIR / "repeats" / "run_*.json")))]
    if not paths:
        print("no run logs found")
        return 1

    data = collect(paths)
    print(f"{'run':<16} {'sim':>7} {'spec':>7} {'attempts':>9} {'calls':>6} {'cost':>8}")
    for r in data["runs"]:
        print(f"{r['file']:<16} {r['simulated']}/{r['circuits']:<5} "
              f"{r['spec_passed']}/{r['spec_checked']:<5} {r['attempts']:>9} "
              f"{r['api_calls']:>6} ${r['cost_usd']:>7.4f}")
    print(f"\nacross {data['n_runs']} runs of {data['circuits']} circuits:")
    print(f"  simulate cleanly : {data['simulated']}")
    print(f"  meet spec        : {data['spec_passed']}")
    print(f"  attempts used    : {data['attempts']}")
    print(f"  same circuits fail spec every run: {data['same_circuits_fail_each_run']}")
    for s in data["spec_failure_sets"]:
        print(f"    failing set: {', '.join(s) if s else '(none)'}")
    print(f"  total API cost   : ${data['total_cost_usd']:.2f}")

    if args.write:
        out = LOG_DIR / "repeats" / "summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
