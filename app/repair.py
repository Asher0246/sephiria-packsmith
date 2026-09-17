"""Post-solve tie-break repair.

Phase 1 scores weighted levels only, where a negative-effect cell costs exactly
as much as a neutral cell; the refinement layer is what prices negative cells,
and it may still end up time-limited.  This pass makes the cheap part
deterministic: it relocates artifacts between cells while keeping the primary
and secondary objectives *identical*, so a proven optimum stays optimal and a
time-limited layout is no worse.  Only the tie-breakers - activation count,
side-effect penalty, empty-cell score, in that order - may improve.

The pass is skipped whenever special-effect priorities are enabled, because
those scores depend on relative placement (neighbours, rows, sides) and moving
an artifact could change them.
"""

from __future__ import annotations

from .models import ArtifactType, SolveRequest, TabletType
from .validation import LayoutFacts, artifact_state_at, layout_facts

MAX_PASSES = 4
APPLICABLE_STATUSES = ("OPTIMAL", "FEASIBLE", "STOPPED")
# primary, secondary, active count, then descending side-effect penalty and
# ascending empty-cell score - the same order the solver's objectives use.
Score = tuple[int, int, int, int, int]


def _side_effect_penalty(request: SolveRequest, facts: LayoutFacts, artifact_cells: set[int]) -> int:
    return sum(
        (-min(0, facts.effects[cell]) + int(cell in facts.disabled)
         if cell in artifact_cells else max(0, facts.effects[cell]) + facts.multipliers[cell])
        for cell in range(request.cell_count)
    )


def _evaluate(
    request: SolveRequest,
    artifacts_by_id: dict[str, ArtifactType],
    tablets_by_id: dict[str, TabletType],
    stationery: list[dict],
    assignment: dict[str, int],
    baseline: LayoutFacts,
) -> tuple[Score, LayoutFacts] | None:
    """Score a candidate assignment, or None when it is not admissible."""
    placements = list(stationery) + [
        {"kind": "artifact", "instanceId": instance_id, "cell": cell}
        for instance_id, cell in assignment.items()
    ]
    facts = layout_facts(request, tablets_by_id, placements)
    if facts.problems:
        return None
    # The tablets must keep applying to exactly the same cells, otherwise the
    # level of every artifact could shift and the pinned score would be void.
    if (facts.effects != baseline.effects or facts.multipliers != baseline.multipliers
            or facts.disabled != baseline.disabled or facts.unlocks != baseline.unlocks):
        return None
    occupied = {entry["cell"] for entry in placements}
    artifact_cells = set(assignment.values())
    primary = secondary = active_count = 0
    for item in request.artifacts:
        artifact = artifacts_by_id[item.type_id]
        level, active = artifact_state_at(
            request, artifact, item.base_level, assignment[item.instance_id],
            facts, occupied, artifact_cells,
        )
        if item.min_level is not None and level < item.min_level:
            return None
        if item.exact_level is not None and level != item.exact_level:
            return None
        if not active:
            continue
        active_count += 1
        if level > 0:
            primary += level * item.weight
            secondary += level
    empty_cell_score = sum(
        facts.effects[cell] for cell in range(request.cell_count) if cell not in occupied
    )
    score = (primary, secondary, active_count,
             -_side_effect_penalty(request, facts, artifact_cells), empty_cell_score)
    return score, facts


def repair_layout(
    request: SolveRequest,
    artifacts_by_id: dict[str, ArtifactType],
    tablets_by_id: dict[str, TabletType],
    result: dict,
) -> dict:
    """Improve tie-breakers without touching the score. Returns the same dict."""
    placements = result.get("placements")
    if not placements or result.get("solutionStatus") not in APPLICABLE_STATUSES:
        return result
    if any(item.special_priority for item in request.artifacts):
        return result
    if not request.artifacts:
        return result

    tablet_placements = [entry for entry in placements if entry.get("kind") == "tablet"]
    assignment = {
        entry["instanceId"]: entry["cell"]
        for entry in placements if entry.get("kind") == "artifact"
    }
    if set(assignment) != {item.instance_id for item in request.artifacts}:
        return result
    movable = {
        item.instance_id for item in request.artifacts if item.fixed_cell is None
    }
    baseline = layout_facts(request, tablets_by_id, placements)
    if baseline.problems:
        return result
    evaluated = _evaluate(
        request, artifacts_by_id, tablets_by_id, tablet_placements, assignment, baseline,
    )
    if evaluated is None:
        return result
    score, _ = evaluated

    for _ in range(MAX_PASSES):
        improved = False
        for item in request.artifacts:
            instance_id = item.instance_id
            if instance_id not in movable:
                continue
            occupied = {entry["cell"] for entry in tablet_placements} | set(assignment.values())
            for target in range(request.cell_count):
                if target in occupied:
                    continue
                trial = dict(assignment)
                trial[instance_id] = target
                candidate = _evaluate(
                    request, artifacts_by_id, tablets_by_id, tablet_placements, trial, baseline,
                )
                if candidate is None:
                    continue
                candidate_score, _ = candidate
                if candidate_score[:2] != score[:2] or candidate_score <= score:
                    continue
                assignment, score = trial, candidate_score
                improved = True
                break
            if improved:
                break
        if not improved:
            break

    if assignment == {
        entry["instanceId"]: entry["cell"]
        for entry in placements if entry.get("kind") == "artifact"
    }:
        return result
    return _apply(request, artifacts_by_id, tablets_by_id, result, assignment)


def _apply(
    request: SolveRequest,
    artifacts_by_id: dict[str, ArtifactType],
    tablets_by_id: dict[str, TabletType],
    result: dict,
    assignment: dict[str, int],
) -> dict:
    """Write a repaired assignment back into the result, recomputing every value."""
    placements = result["placements"]
    for entry in placements:
        if entry.get("kind") == "artifact" and entry["instanceId"] in assignment:
            entry["cell"] = assignment[entry["instanceId"]]
    facts = layout_facts(request, tablets_by_id, placements)
    occupied = {entry["cell"] for entry in placements}
    artifact_cells = set(assignment.values())
    details = {entry["instanceId"]: entry for entry in result.get("artifacts", [])}
    for item in request.artifacts:
        detail = details.get(item.instance_id)
        if detail is None:
            continue
        artifact = artifacts_by_id[item.type_id]
        cell = assignment[item.instance_id]
        level, active = artifact_state_at(
            request, artifact, item.base_level, cell, facts, occupied, artifact_cells,
        )
        detail["cell"] = cell
        detail["rawBonus"] = facts.effects[cell]
        detail["multiplier"] = max(1, facts.multipliers[cell])
        detail["disabled"] = cell in facts.disabled
        detail["level"] = level
        detail["active"] = active
        detail["contribution"] = max(0, level) * item.weight if active else 0
        detail["tabletEffects"] = facts.tablet_effects[cell]
    result["cellEffects"] = facts.effects
    result["cellMultipliers"] = facts.multipliers
    result["disabledCells"] = sorted(facts.disabled)
    result["unlockedCells"] = sorted(facts.unlocks)
    result["secondaryObjective"] = sum(
        detail["level"] for detail in result.get("artifacts", [])
        if detail["active"] and detail["level"] > 0
    )
    result["tertiaryObjective"] = _side_effect_penalty(request, facts, artifact_cells)
    result["emptyCellObjective"] = sum(
        facts.effects[cell] for cell in range(request.cell_count) if cell not in occupied
    )
    return result
