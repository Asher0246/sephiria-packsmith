"""Reproducible GPU seed + CP-SAT vs CP-SAT benchmark.

Run: runtime/Scripts/python.exe -m tools.benchmark_gpu --repeats 5
Outputs raw trials and medians; no disk result cache or game connection is used.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import random
from statistics import median
import time

from app.catalog import artifact_types, tablet_types
from app.models import ArtifactInstance, SolveRequest, TabletInstance
from app.repair import repair_layout
from app.solver import solve
from app.validation import validate_result
from tools.gpu_search import GpuSearch


def cases(artifacts, tablets):
    pool = sorted((i for i in artifacts.values() if i.cap > 0 and not i.criteria), key=lambda i: i.id)
    tablet_pool = sorted(tablets.values(), key=lambda i: i.id)
    # High-occupancy inventories: base backpack, mid-game expansion, late game.
    for name, rows, cols, a, t, seed in (("base30", 5, 6, 23, 5, 20260950),
                                         ("expanded42", 7, 6, 33, 7, 20260962),
                                         ("late60", 10, 6, 48, 10, 20260980)):
        rng = random.Random(seed)
        chosen = rng.sample(pool, a)
        tids = [item.id for item in rng.sample(tablet_pool, t)]
        yield name, SolveRequest(
            rows, cols,
            tuple(ArtifactInstance(f"a{i}", item.id, weight=5,
                                   base_level=rng.choice((0, 0, 0, 1, 1, 2)))
                  for i, item in enumerate(chosen[:a])),
            tuple(TabletInstance(f"t{i}", tid) for i, tid in enumerate(tids[:t])),
            worker_count=8, fast_mode=True,
        )


def trial(request, artifacts, tablets, budget, gpu, seed, steps, *, cold_start=False):
    started = time.perf_counter()
    deadline = started + budget / 1000
    if cold_start:
        gpu = GpuSearch()
    initialization_ms = (time.perf_counter() - started) * 1000 if cold_start else 0
    initial = gpu.search(request, artifacts, tablets, seed=seed, steps=steps) if gpu else None
    remaining = (deadline - time.perf_counter()) * 1000
    if remaining <= 0:
        return {"status": "BUDGET_EXHAUSTED", "seedScore": initial["score"],
                "totalMs": (time.perf_counter() - started) * 1000}
    request = replace(request, time_limit_ms=max(1, int(remaining)))

    def limit(target):
        target.parameters.max_time_in_seconds = min(
            target.parameters.max_time_in_seconds,
            max(0.001, deadline - time.perf_counter()),
        )

    result = solve(request, artifacts, tablets, on_solver=limit,
                   initial_placements=initial["placements"] if initial else None)
    result = repair_layout(request, artifacts, tablets, result)
    if initial:
        seed_result = repair_layout(request, artifacts, tablets, initial["result"])
        def quality(value):
            return (value["specialObjective"], value["primaryObjective"],
                    value["secondaryObjective"], sum(i["active"] for i in value["artifacts"]),
                    -value["tertiaryObjective"], value["emptyCellObjective"])
        if quality(seed_result) > quality(result):
            seed_result["diagnostics"]["replacedCpuResult"] = True
            seed_result["diagnostics"]["discardedCpuQuality"] = quality(result)
            seed_result["buildMs"], seed_result["solveMs"] = result["buildMs"], result["solveMs"]
            result = seed_result
    # Includes CPU verification in measured request latency for both modes.
    problems = validate_result(request, artifacts, tablets, result)
    if problems:
        raise AssertionError(problems)
    total = (time.perf_counter() - started) * 1000
    score = None
    if result["placements"]:
        score = [result["specialObjective"], result["primaryObjective"],
                 result["secondaryObjective"], sum(i["active"] for i in result["artifacts"]),
                 -result["tertiaryObjective"], result["emptyCellObjective"]]
    return {
        "status": result["solutionStatus"], "score": score,
        "totalMs": round(total, 2), "buildMs": result["buildMs"],
        "solveMs": result["solveMs"],
        "gpuMs": round(initial["elapsedMs"], 2) if initial else 0,
        "gpuInitializationMs": round(initialization_ms, 2),
        "seedScore": initial["score"] if initial else None,
        "hintVariables": result["diagnostics"].get("initialHintVariables", 0),
        "usedGpuFallback": result["diagnostics"].get("replacedCpuResult", False),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--budgets", type=int, nargs="+", default=[15000])
    parser.add_argument("--steps", type=int, default=512)
    parser.add_argument("--full-time", action="store_true",
                        help="Disable the stall stopper and use the full quality budget")
    parser.add_argument("--cold-start", action="store_true",
                        help="Run one dense30 pair at the largest budget, including first GPU initialization")
    parser.add_argument("--output", type=Path, default=Path("artifacts/gpu-benchmark.json"))
    args = parser.parse_args()
    if args.repeats < 1 or any(b < 100 for b in args.budgets):
        parser.error("Use positive repeats and budgets of at least 100 ms")
    artifacts = {i.id: i for i in artifact_types()}
    tablets = {i.id: i for i in tablet_types()}
    examples = list(cases(artifacts, tablets))
    if args.full_time:
        examples = [(name, replace(request, fast_mode=False)) for name, request in examples]
    if args.cold_start:
        request = examples[-1][1]
        budget = max(args.budgets)
        report = {
            "case": "late60", "budgetMs": budget,
            "cpu": trial(request, artifacts, tablets, budget, None, 1, args.steps),
            "hybrid": trial(request, artifacts, tablets, budget, None, 1, args.steps, cold_start=True),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf8")
        print(json.dumps(report, indent=2), flush=True)
        return
    cold_started = time.perf_counter()
    gpu = GpuSearch()
    init_ms = (time.perf_counter() - cold_started) * 1000
    first = gpu.search(examples[-1][1], artifacts, tablets, steps=args.steps)
    cold_ms = (time.perf_counter() - cold_started) * 1000
    print(json.dumps({"gpuInitMs": round(init_ms, 2),
                      "firstSeedIncludingInitMs": round(cold_ms, 2),
                      "firstSeedScore": first["score"]}), flush=True)
    trials = []
    for name, request in examples:
        for budget in args.budgets:
            for repeat in range(args.repeats):
                # Alternate order to reduce systematic warm-up/thermal bias.
                modes = ("cpu", "hybrid") if repeat % 2 == 0 else ("hybrid", "cpu")
                for mode in modes:
                    row = trial(request, artifacts, tablets, budget,
                                gpu if mode == "hybrid" else None, repeat+1, args.steps)
                    row.update(case=name, budgetMs=budget, mode=mode, repeat=repeat+1)
                    trials.append(row)
                    print(json.dumps(row), flush=True)
    summaries = []
    for name, _ in examples:
        for budget in args.budgets:
            for mode in ("cpu", "hybrid"):
                group = [r for r in trials if (r["case"], r["budgetMs"], r["mode"]) == (name, budget, mode)]
                valid = [r for r in group if r.get("score") is not None]
                summary = {
                    "case": name, "budgetMs": budget, "mode": mode,
                    "primaryMedian": median(r["score"][1] for r in valid) if valid else None,
                    "totalMsMedian": median(r["totalMs"] for r in group),
                    "gpuMsMedian": median(r.get("gpuMs", 0) for r in group),
                    "feasible": len(valid), "optimal": sum(r["status"] == "OPTIMAL" for r in group),
                }
                summaries.append(summary)
    report = {
        "device": gpu.cp.cuda.runtime.getDeviceProperties(0)["name"].decode(),
        "cupy": gpu.cp.__version__, "repeats": args.repeats, "steps": args.steps,
        "gpuInitMs": init_ms, "firstSeedIncludingInitMs": cold_ms,
        "method": f"High-occupancy 30/42/60-cell inventories; deterministic independent artifact/tablet samples and base levels using seeds 20260950/20260962/20260980; default weight 5; {'full-time quality mode' if args.full_time else 'fast mode'}; 15-second default limit; alternating modes; 8 CPU workers. GPU preparation, transfers, seed check, CPU building/search/repair and final validation included in totalMs. Import/catalog loading excluded equally. Solver deadlines are soft; actual overrun is reported. First-use GPU startup is reported separately. No precomputed reference solution.",
        "summary": summaries, "trials": trials,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps({"summary": summaries, "output": str(args.output)}, indent=2), flush=True)


if __name__ == "__main__":
    main()
