"""Tests for the post-solve tie-break repair."""

from app.catalog import artifact_types, tablet_types
from app.models import ArtifactInstance, SolveRequest, TabletInstance, TabletType
from app.repair import repair_layout
from app.solver import solve
from app.validation import validate_result


def catalog():
    return {item.id: item for item in artifact_types()}, {item.id: item for item in tablet_types()}


def negative_cell_request():
    """One row, a fixed tablet that puts -2 on cell 2, everything else neutral."""
    artifacts, tablets = catalog()
    negative = TabletType("tablet-negative", "左-2", "rare", False, None, None, ((-1, -2),))
    request = SolveRequest(
        1, 6,
        (ArtifactInstance("a1", "artifact-eye_crystal_necklace", weight=5),),
        (TabletInstance("t1", negative.id, fixed_cell=3),),
        3000,
    )
    return request, artifacts, {**tablets, negative.id: negative}


def test_repair_moves_artifact_off_a_negative_effect_cell():
    request, artifacts, tablets = negative_cell_request()
    result = solve(request, artifacts, tablets)
    assert result["solutionStatus"] == "OPTIMAL"
    # Park the artifact on the -2 cell, emulating a layout that was never polished.
    for entry in result["placements"]:
        if entry["kind"] == "artifact":
            entry["cell"] = 2
    repaired = repair_layout(request, artifacts, tablets, result)
    detail = repaired["artifacts"][0]
    assert repaired["cellEffects"][detail["cell"]] == 0
    assert detail["level"] == 0
    assert repaired["tertiaryObjective"] == 0
    assert validate_result(request, artifacts, tablets, repaired) == []


def test_repair_keeps_the_score_and_the_result_valid():
    artifacts, tablets = catalog()
    pool = [item for item in artifacts.values() if item.cap >= 2 and not item.criteria]
    request = SolveRequest(
        3, 6,
        tuple(ArtifactInstance(f"a{i}", item.id, weight=5 + i % 3)
              for i, item in enumerate(pool[:10])),
        (TabletInstance("t1", "tablet-advance"), TabletInstance("t2", "tablet-fate")),
        3000,
    )
    result = solve(request, artifacts, tablets)
    primary, secondary = result["primaryObjective"], result["secondaryObjective"]
    repaired = repair_layout(request, artifacts, tablets, result)
    assert repaired["primaryObjective"] == primary
    assert repaired["secondaryObjective"] == secondary
    assert validate_result(request, artifacts, tablets, repaired) == []


def test_repair_is_skipped_when_special_effects_are_enabled():
    """Special scores depend on relative placement, so the repair must not move
    anything while they are in play."""
    artifacts, tablets = catalog()
    request = SolveRequest(
        2, 6,
        (ArtifactInstance("a1", "artifact-crystal_of_harmony", weight=5, special_priority=True),),
        (), 3000,
    )
    result = solve(request, artifacts, tablets)
    assert repair_layout(request, artifacts, tablets, result) is result
