"""Optional GPU seed experiment; not imported by the production server."""
from pathlib import Path
import time

import numpy as np

from app.solver import build_candidates_cached
from app.validation import artifact_state_at, layout_facts


class GpuSearch:
    def __init__(self):
        import cupy as cp
        self.cp = cp
        self.kernel = cp.RawKernel(
            Path(__file__).with_suffix(".cu").read_text(encoding="utf8"), "search",
        )
        self.kernel.compile()
        cp.cuda.runtime.deviceSynchronize()

    def search(self, request, artifacts, tablets, *, count=4096, steps=256, seed=1):
        started = time.perf_counter()
        if not 1 <= request.cell_count <= 60 or not 1 <= count <= 65536 or not 0 <= steps <= 4096:
            raise ValueError("GPU experiment size outside supported bounds")
        if any(item.fixed_cell is not None for item in (*request.artifacts, *request.tablets)):
            raise ValueError("GPU experiment does not yet support fixed cells")
        if any(item.special_priority or item.min_level is not None or item.exact_level is not None
               or artifacts[item.type_id].criteria for item in request.artifacts):
            raise ValueError("GPU experiment supports unconstrained ordinary artifacts only")
        if any(item.fixed_rotation is not None for item in request.tablets):
            raise ValueError("GPU experiment does not yet support fixed rotations")
        n, a, t = request.cell_count, len(request.artifacts), len(request.tablets)
        if a + t > n:
            raise ValueError("Too many items")
        valid = np.zeros((t, n, 4), dtype=np.int32)
        effects = np.zeros((t, n, 4, n), dtype=np.int32)
        multipliers = np.zeros_like(effects)
        disabled = np.zeros_like(effects)
        lookup = {}
        for j, item in enumerate(request.tablets):
            candidates = build_candidates_cached(tablets[item.type_id], request.rows, request.cols, n)
            for c in candidates:
                if c.conditions:
                    raise ValueError("GPU experiment does not yet support conditional tablets")
                for values in (c.effects, c.multipliers):
                    if any(abs(value) > 100 for value in values.values()):
                        raise ValueError("GPU experiment effect outside integer safety bounds")
            # Missing rotation slots alias a real candidate at the same cell.
            # Return that candidate's actual rotation to the CPU validator.
            for cell in range(n):
                options = [c for c in candidates if c.cell == cell]
                if not options:
                    continue
                for rotation in range(4):
                    c = next((c for c in options if c.rotation == rotation), options[rotation % len(options)])
                    lookup[j, cell, rotation] = c.rotation
                    valid[j, cell, rotation] = 1
                    for target, value in c.effects.items():
                        effects[j, cell, rotation, target] = value
                    for target, value in c.multipliers.items():
                        multipliers[j, cell, rotation, target] = value
                    for target in c.disables:
                        disabled[j, cell, rotation, target] = 1
        base = np.array([i.base_level for i in request.artifacts], dtype=np.int32)
        caps = np.array([artifacts[i.type_id].cap for i in request.artifacts], dtype=np.int32)
        weights = np.array([i.weight for i in request.artifacts], dtype=np.int32)
        if any(np.any((v < 0) | (v > 100)) for v in (base, caps, weights)):
            raise ValueError("GPU experiment artifact values outside safety bounds")
        # Caps keep the exact lexicographic score well inside signed int64.
        level_scale = int(caps.sum()) + 1
        doubles = np.array([2 if c in request.double_level_cells else 0 for c in range(n)], dtype=np.int32)
        cp = self.cp
        arrays = [cp.asarray(v) for v in (valid, effects, multipliers, disabled, base, caps, weights, doubles)]
        positions = cp.empty((count, n), dtype=cp.int32)
        rotations = cp.empty((count, t), dtype=cp.int32)
        scores = cp.empty(count, dtype=cp.int64)
        self.kernel(((count + 127) // 128,), (128,), (
            np.int32(n), np.int32(a), np.int32(t), np.int32(count), np.int32(steps),
            np.uint32(seed), np.int32(level_scale), *arrays, positions, rotations, scores,
        ))
        best = int(cp.argmax(scores).item())
        best_score = int(scores[best].item())
        if best_score < 0:
            raise RuntimeError("GPU search did not find a legal layout")
        pos, rot = cp.asnumpy(positions[best]), cp.asnumpy(rotations[best])
        placements = [
            {"instanceId": item.instance_id, "kind": "artifact", "cell": int(pos[i])}
            for i, item in enumerate(request.artifacts)
        ] + [
            {"instanceId": item.instance_id, "kind": "tablet", "cell": int(pos[a+j]),
             "rotation": lookup[j, int(pos[a+j]), int(rot[j])]}
            for j, item in enumerate(request.tablets)
        ]
        score = checked_score(request, artifacts, tablets, placements)
        encoded = (score[0] * level_scale + score[1]) * (a + 1) + score[2]
        if encoded != best_score:
            raise AssertionError(f"CPU/GPU scoring mismatch: {encoded} != {best_score}")
        result = result_from_placements(request, artifacts, tablets, placements)
        return {
            "placements": placements, "score": score,
            "result": result,
            "elapsedMs": (time.perf_counter() - started) * 1000,
            "evaluations": count * (steps + 1),
        }


def checked_score(request, artifacts, tablets, placements):
    """Independent CPU check of every exported seed, before handing it to CP-SAT."""
    expected = {i.instance_id for i in (*request.artifacts, *request.tablets)}
    if len(placements) != len(expected) or {p["instanceId"] for p in placements} != expected:
        raise ValueError("Seed instance mismatch")
    occupied = {p["cell"] for p in placements}
    if len(occupied) != len(placements) or not all(0 <= c < request.cell_count for c in occupied):
        raise ValueError("Seed cells overlap or are out of range")
    facts = layout_facts(request, tablets, placements)
    if facts.problems:
        raise ValueError(facts.problems)
    by_id = {p["instanceId"]: p for p in placements}
    artifact_cells = {by_id[i.instance_id]["cell"] for i in request.artifacts}
    primary = secondary = active_count = 0
    for item in request.artifacts:
        level, active = artifact_state_at(
            request, artifacts[item.type_id], item.base_level, by_id[item.instance_id]["cell"],
            facts, occupied, artifact_cells,
        )
        if active:
            primary += max(0, level) * item.weight
            secondary += max(0, level)
            active_count += 1
    return primary, secondary, active_count


def result_from_placements(request, artifacts, tablets, placements):
    """Turn a CPU-verified GPU layout into the normal solver result contract."""
    primary, secondary, active_count = checked_score(request, artifacts, tablets, placements)
    by_id = {p["instanceId"]: p for p in placements}
    occupied = {p["cell"] for p in placements}
    artifact_cells = {by_id[i.instance_id]["cell"] for i in request.artifacts}
    facts = layout_facts(request, tablets, placements)
    output_placements = []
    details = []
    for item in request.artifacts:
        artifact = artifacts[item.type_id]
        cell = by_id[item.instance_id]["cell"]
        level, active = artifact_state_at(
            request, artifact, item.base_level, cell, facts, occupied, artifact_cells,
        )
        output_placements.append({
            "kind": "artifact", "instanceId": item.instance_id,
            "typeId": item.type_id, "cell": cell,
        })
        details.append({
            "instanceId": item.instance_id, "typeId": item.type_id,
            "name": artifact.name, "cell": cell, "baseLevel": item.base_level,
            "rawBonus": facts.effects[cell], "multiplier": max(1, facts.multipliers[cell]),
            "disabled": cell in facts.disabled, "level": level, "cap": artifact.cap,
            "active": active, "weight": item.weight,
            "contribution": max(0, level) * item.weight if active else 0,
            "tabletEffects": facts.tablet_effects[cell],
        })
    for item in request.tablets:
        placed = by_id[item.instance_id]
        candidate = next(c for c in build_candidates_cached(
            tablets[item.type_id], request.rows, request.cols, request.cell_count,
        ) if c.cell == placed["cell"] and c.rotation == placed["rotation"])
        range_cells = (set(candidate.effects) | set(candidate.unlocks) |
                       set(candidate.disables) | set(candidate.multipliers) |
                       {cell for cell, _ in candidate.conditions})
        output_placements.append({
            "kind": "tablet", "instanceId": item.instance_id, "typeId": item.type_id,
            "cell": candidate.cell, "rotation": candidate.rotation,
            "applied": facts.applied[item.instance_id], "rangeCells": sorted(range_cells),
        })
    tertiary = sum(
        (-min(0, facts.effects[cell]) + int(cell in facts.disabled)
         if cell in artifact_cells else max(0, facts.effects[cell]) + facts.multipliers[cell])
        for cell in range(request.cell_count)
    )
    empty = sum(facts.effects[cell] for cell in range(request.cell_count) if cell not in occupied)
    return {
        "solutionStatus": "FEASIBLE", "secondaryStatus": "NOT_RUN",
        "specialStatus": "DISABLED", "tertiaryStatus": "NOT_RUN",
        "emptyCellStatus": "NOT_RUN", "message": "GPU 启发式找到可行排布",
        "primaryObjective": primary, "secondaryObjective": secondary,
        "specialObjective": 0, "specialDetails": [],
        "tertiaryObjective": tertiary, "emptyCellObjective": empty,
        "primaryBestBound": None, "relativeGap": None,
        "placements": output_placements, "artifacts": details,
        "cellEffects": facts.effects, "cellMultipliers": facts.multipliers,
        "disabledCells": sorted(facts.disabled), "unlockedCells": sorted(facts.unlocks),
        "buildMs": 0, "solveMs": 0,
        "diagnostics": {"gpuSeed": True, "activeArtifacts": active_count},
    }
