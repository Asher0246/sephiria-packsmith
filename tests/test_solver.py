import pytest

from app.models import ArtifactInstance, ArtifactType, SolveRequest, TabletInstance, TabletType
from app.solver import (
    Candidate,
    StopController,
    _bounded_relative_gap,
    _primary_bound_from_phase1,
    build_candidates,
    solve,
)
from app.validation import validate_result


ARTIFACT = ArtifactType("artifact-a", "测试神器", cap=3, rarity=0)
EDGE_ARTIFACT = ArtifactType("artifact-edge", "边缘神器", cap=3, rarity=0, criteria=("edge",))
TABLET = TabletType("tablet-t", "右侧+5", "rare", True, None, None, ((1, 5),))


def maps():
    return {ARTIFACT.id: ARTIFACT, EDGE_ARTIFACT.id: EDGE_ARTIFACT}, {TABLET.id: TABLET}


@pytest.mark.parametrize(("value", "bound", "expected"), (
    (100, 100, 0.0),
    (50, 100, 0.5),
    (1, 10_000, 0.9999),
    (0, 10_000, 1.0),
))
def test_relative_gap_is_measured_against_bound_and_never_exceeds_one(value, bound, expected):
    assert _bounded_relative_gap(value, bound) == pytest.approx(expected)


def test_primary_bound_ignores_lexicographic_special_objective_scale():
    assert _primary_bound_from_phase1(
        phase1_bound=2 * 1_000_000 + 90 * 100 + 4,
        special_value=1,
        special_upper=5,
        special_scale=1_000_000,
        primary_value=50,
        primary_upper=100,
        primary_scale=100,
    ) == 100
    assert _primary_bound_from_phase1(
        phase1_bound=1 * 1_000_000 + 90 * 100 + 4,
        special_value=1,
        special_upper=5,
        special_scale=1_000_000,
        primary_value=50,
        primary_upper=100,
        primary_scale=100,
    ) == 90


def test_candidate_rotation_uses_anchor_and_clips_effects():
    candidates = build_candidates(TABLET, 2, 2)
    at_zero = {(candidate.rotation, tuple(candidate.effects.items())) for candidate in candidates if candidate.cell == 0}
    assert (0, ((1, 5),)) in at_zero
    assert (1, ((2, 5),)) in at_zero
    assert any(rotation == 2 and effects == () for rotation, effects in at_zero)


def test_candidates_respect_partial_last_row_cell_count():
    precomputed = TabletType(
        "tablet-partial", "残行", "common", False, None, None,
        candidates={"2x6": (
            (0, 0, ((6, 1), (7, 1)), (6, 7)),
            (7, 0, ((6, 1),), ()),
        )},
    )
    candidates = build_candidates(precomputed, 2, 6, 7)
    assert candidates == (Candidate(0, 0, {6: 1}, frozenset({6})),)

    procedural = TabletType("tablet-right", "右侧", "common", False, None, None, ((1, 1),))
    generated = build_candidates(procedural, 2, 6, 7)
    assert {candidate.cell for candidate in generated} == set(range(7))
    assert next(candidate for candidate in generated if candidate.cell == 6).effects == {}


def test_symmetric_tablet_keeps_explicit_rotation_values():
    symmetric = TabletType("tablet-s", "水平对称", "rare", True, None, None, ((-1, 1), (1, 1)))
    at_center = [candidate for candidate in build_candidates(symmetric, 3, 3) if candidate.cell == 4]
    assert {candidate.rotation for candidate in at_center} == {0, 1, 2, 3}
    request = SolveRequest(3, 3, (), (TabletInstance("t1", symmetric.id, fixed_cell=4, fixed_rotation=2),), 1000)
    result = solve(request, {}, {symmetric.id: symmetric})
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["placements"][0]["rotation"] == 2


def test_unfixed_rotation_deduplicates_behaviorally_identical_candidates():
    symmetric = TabletType(
        "tablet-symmetric-dedup", "对称去重", "rare", True, None, None,
        ((-1, 1), (1, 1)),
    )
    request = SolveRequest(3, 3, (), (TabletInstance("t1", symmetric.id),), 1000)

    result = solve(request, {}, {symmetric.id: symmetric})

    assert result["solutionStatus"] == "OPTIMAL"
    assert result["diagnostics"]["rawTabletCandidates"] == 36
    assert result["diagnostics"]["tabletCandidates"] < 36


def test_candidate_deduplication_keeps_preferred_game_rotation():
    symmetric = TabletType(
        "tablet-preferred-rotation", "方向保持", "rare", True, None, None,
        candidates={"1x1": (
            (0, 0, (), ()), (0, 1, (), ()),
            (0, 2, (), ()), (0, 3, (), ()),
        )},
    )
    request = SolveRequest(
        1, 1, (),
        (TabletInstance("t1", symmetric.id, preferred_rotation=3),),
        1000,
    )

    result = solve(request, {}, {symmetric.id: symmetric})

    assert result["solutionStatus"] == "OPTIMAL"
    assert result["diagnostics"]["tabletCandidates"] == 1
    assert result["placements"][0]["rotation"] == 3


def test_interchangeable_tablets_remain_valid_after_symmetry_breaking():
    tablet = TabletType("tablet-pair", "重复石板", "common", False, None, None, ((1, 1),))
    request = SolveRequest(
        1, 4,
        (ArtifactInstance("a1", ARTIFACT.id),),
        (TabletInstance("left", tablet.id), TabletInstance("right", tablet.id)),
        1000,
    )

    result = solve(request, {ARTIFACT.id: ARTIFACT}, {tablet.id: tablet})
    tablet_cells = [
        item["cell"] for item in result["placements"] if item["kind"] == "tablet"
    ]

    assert result["solutionStatus"] == "OPTIMAL"
    assert tablet_cells == sorted(tablet_cells)
    assert validate_result(request, {ARTIFACT.id: ARTIFACT}, {tablet.id: tablet}, result) == []


def test_curse_tablet_uses_game_checkerboard_range_on_partial_last_row():
    curse = TabletType(
        "tablet-curse", "诅咒", "special", True, None, None,
        (("CHECKERBOARD2", 1), ("CHECKERBOARD", -1)),
    )
    candidates = build_candidates(curse, 6, 6, 35)
    rotations = [candidate for candidate in candidates if candidate.cell == 15]
    assert len(candidates) == 35
    assert {candidate.rotation for candidate in rotations} == {0}
    effects = rotations[0].effects
    assert len(effects) == 34
    assert list(effects.values()).count(1) == 17
    assert list(effects.values()).count(-1) == 17
    assert effects[0] == 1
    assert effects[1] == -1
    assert effects[34] == -1
    assert 15 not in effects


def test_curse_tablet_keeps_explicit_rotation_without_extra_solver_candidates():
    curse = TabletType(
        "tablet-curse", "诅咒", "special", True, None, None,
        (("CHECKERBOARD2", 1), ("CHECKERBOARD", -1)),
    )
    request = SolveRequest(
        2, 3,
        (ArtifactInstance("a1", ARTIFACT.id, fixed_cell=5),),
        (TabletInstance("curse", curse.id, fixed_cell=2, fixed_rotation=3),),
        1000,
    )
    result = solve(request, {ARTIFACT.id: ARTIFACT}, {curse.id: curse})
    tablet = next(item for item in result["placements"] if item["kind"] == "tablet")
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["diagnostics"]["tabletCandidates"] == 1
    assert tablet["rotation"] == 3
    assert validate_result(request, {ARTIFACT.id: ARTIFACT}, {curse.id: curse}, result) == []


@pytest.mark.parametrize("cell_count", [30, 32, 35, 36, 41])
def test_shade_tablet_targets_last_six_actual_inventory_cells(cell_count):
    rows = (cell_count + 5) // 6
    shade = TabletType(
        "tablet-shade", "遮阳", "rare", False, "first_row", None,
        (("BOTTOM", 1),),
    )
    candidates = build_candidates(shade, rows, 6, cell_count)
    assert {candidate.cell for candidate in candidates} == set(range(min(6, cell_count)))
    expected = set(range(max(0, cell_count - 6), cell_count))
    assert all(set(candidate.effects) == expected for candidate in candidates)
    assert all(set(candidate.effects.values()) == {1} for candidate in candidates)


def test_partial_last_row_treats_missing_right_neighbor_as_empty():
    side_free = ArtifactType(
        "artifact-side-free", "左右留空", cap=1, rarity=0, criteria=("side_free",),
    )
    request = SolveRequest(
        2, 6,
        (ArtifactInstance("a1", side_free.id, fixed_cell=6),),
        (), 1000, actual_cell_count=7,
    )
    result = solve(request, {side_free.id: side_free}, {})
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["artifacts"][0]["active"] is True
    assert validate_result(request, {side_free.id: side_free}, {}, result) == []


def test_solver_obeys_fixed_cells_caps_level_and_rotation():
    artifacts, tablets = maps()
    request = SolveRequest(2, 2,
        (ArtifactInstance("a1", ARTIFACT.id, weight=5, min_level=2, exact_level=3, fixed_cell=3),),
        (TabletInstance("t1", TABLET.id, fixed_cell=2, fixed_rotation=0),),
        3000,
    )
    result = solve(request, artifacts, tablets)
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["primaryObjective"] == 15
    assert result["secondaryObjective"] == 3
    detail = result["artifacts"][0]
    assert detail["cell"] == 3
    assert detail["rawBonus"] == 5
    assert detail["level"] == detail["cap"] == 3
    tablet = next(item for item in result["placements"] if item["kind"] == "tablet")
    assert tablet["rotation"] == 0
    assert validate_result(request, artifacts, tablets, result) == []


def test_artifact_details_list_each_applied_tablet_effect():
    positive = TabletType(
        "tablet-positive", "正向石板", "common", False, None, None,
        candidates={"1x3": ((0, 0, ((2, 2),), ()),)},
    )
    negative = TabletType(
        "tablet-negative", "负向石板", "common", False, None, None,
        candidates={"1x3": ((1, 0, ((2, -1),), ()),)},
    )
    request = SolveRequest(
        1, 3,
        (ArtifactInstance("artifact", ARTIFACT.id, fixed_cell=2),),
        (
            TabletInstance("positive", positive.id, fixed_cell=0),
            TabletInstance("negative", negative.id, fixed_cell=1),
        ),
        3000,
    )
    tablets = {positive.id: positive, negative.id: negative}
    result = solve(request, {ARTIFACT.id: ARTIFACT}, tablets)
    assert result["artifacts"][0]["tabletEffects"] == [
        {
            "instanceId": "positive", "typeId": positive.id,
            "name": "正向石板", "cell": 0, "additive": 2, "multiplier": 0,
        },
        {
            "instanceId": "negative", "typeId": negative.id,
            "name": "负向石板", "cell": 1, "additive": -1, "multiplier": 0,
        },
    ]
    assert validate_result(request, {ARTIFACT.id: ARTIFACT}, tablets, result) == []


def test_solver_reports_infeasible_minimum_level():
    artifacts, tablets = maps()
    request = SolveRequest(1, 2,
        (ArtifactInstance("a1", ARTIFACT.id, min_level=3, fixed_cell=0),),
        (TabletInstance("t1", TABLET.id, fixed_cell=1, fixed_rotation=0),),
        1000,
    )
    result = solve(request, artifacts, tablets)
    assert result["solutionStatus"] == "INFEASIBLE"
    assert result["placements"] == []


def test_solver_adds_instance_enchantment_level_before_capping():
    artifact = ArtifactType("artifact-base", "Base level", cap=4, rarity=0)
    tablet = TabletType("tablet-one", "Right +1", "common", False, None, None, ((1, 1),))
    request = SolveRequest(1, 2,
        (ArtifactInstance("a1", artifact.id, fixed_cell=1, base_level=2),),
        (TabletInstance("t1", tablet.id, fixed_cell=0),), 1000)
    result = solve(request, {artifact.id: artifact}, {tablet.id: tablet})
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["artifacts"][0]["rawBonus"] == 1
    assert result["artifacts"][0]["baseLevel"] == 2
    assert result["artifacts"][0]["level"] == 3
    assert validate_result(request, {artifact.id: artifact}, {tablet.id: tablet}, result) == []


def test_solver_obeys_exact_level_constraint():
    artifacts, tablets = maps()
    request = SolveRequest(1, 2,
        (ArtifactInstance("a1", ARTIFACT.id, exact_level=2, fixed_cell=1),),
        (TabletInstance("t1", TABLET.id, fixed_cell=0, fixed_rotation=0),), 1000)
    result = solve(request, artifacts, tablets)
    assert result["solutionStatus"] == "INFEASIBLE"


def test_negative_level_is_allowed_but_disables_artifact_without_forcing_others_positive():
    negative = TabletType("tablet-negative", "右侧-1", "common", False, None, None, ((1, -1),))
    request = SolveRequest(
        1, 3,
        (
            ArtifactInstance("negative", ARTIFACT.id, fixed_cell=1),
            ArtifactInstance("zero", ARTIFACT.id, fixed_cell=2),
        ),
        (TabletInstance("tablet", negative.id, fixed_cell=0),),
        1000,
    )
    result = solve(request, {ARTIFACT.id: ARTIFACT}, {negative.id: negative})
    details = {item["instanceId"]: item for item in result["artifacts"]}
    assert result["solutionStatus"] == "OPTIMAL"
    assert (details["negative"]["level"], details["negative"]["active"]) == (-1, False)
    assert (details["zero"]["level"], details["zero"]["active"]) == (0, True)
    assert result["primaryObjective"] == result["secondaryObjective"] == 0
    assert validate_result(request, {ARTIFACT.id: ARTIFACT}, {negative.id: negative}, result) == []


def test_unlock_bypasses_artifact_activation_condition():
    unlock_tablet = TabletType("tablet-u", "右侧解锁", "rare", False, None, None, ((1, "UNLOCK"),))
    bonus_tablet = TabletType("tablet-b", "右侧+2", "rare", False, None, None, ((1, 2),))
    request = SolveRequest(3, 3,
        (ArtifactInstance("a1", EDGE_ARTIFACT.id, fixed_cell=4),),
        (TabletInstance("u1", unlock_tablet.id, fixed_cell=3), TabletInstance("b1", bonus_tablet.id, fixed_cell=3 + 3)),
        3000,
    )
    result = solve(request, {EDGE_ARTIFACT.id: EDGE_ARTIFACT}, {unlock_tablet.id: unlock_tablet, bonus_tablet.id: bonus_tablet})
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["artifacts"][0]["active"] is True


def test_weighted_objective_prioritizes_requested_artifact():
    one_bonus = TabletType("tablet-one", "右侧+1", "common", False, None, None, ((1, 1),))
    request = SolveRequest(1, 3,
        (ArtifactInstance("low", ARTIFACT.id, weight=1), ArtifactInstance("high", ARTIFACT.id, weight=10)),
        (TabletInstance("bonus", one_bonus.id),), 3000)
    result = solve(request, {ARTIFACT.id: ARTIFACT}, {one_bonus.id: one_bonus})
    details = {item["instanceId"]: item for item in result["artifacts"]}
    assert result["solutionStatus"] == "OPTIMAL"
    assert details["high"]["level"] == 1
    assert details["low"]["level"] == 0
    assert result["primaryObjective"] == 10


def test_combined_level_objective_keeps_primary_strictly_ahead_of_secondary():
    bonus = TabletType(
        "tablet-level-choice", "等级选择", "common", False, None, None,
        candidates={"1x4": (
            (0, 0, ((1, 1),), ()),
            (0, 1, ((2, 1), (3, 1)), ()),
        )},
    )
    valuable = ArtifactType("artifact-valuable", "高权重", cap=1, rarity=0)
    filler = ArtifactType("artifact-filler", "低权重", cap=1, rarity=0)
    request = SolveRequest(
        1, 4,
        (
            ArtifactInstance("valuable", valuable.id, weight=10, fixed_cell=1),
            ArtifactInstance("filler-1", filler.id, weight=1, fixed_cell=2),
            ArtifactInstance("filler-2", filler.id, weight=1, fixed_cell=3),
        ),
        (TabletInstance("bonus", bonus.id, fixed_cell=0),),
        3000,
    )

    result = solve(request, {valuable.id: valuable, filler.id: filler}, {bonus.id: bonus})
    details = {item["instanceId"]: item for item in result["artifacts"]}

    assert result["solutionStatus"] == "OPTIMAL"
    assert result["primaryObjective"] == 10
    assert details["valuable"]["level"] == 1


def test_artifacts_with_the_same_base_level_and_cap_share_level_transforms():
    first = ArtifactType("artifact-shared-a", "共享 A", cap=3, rarity=0)
    second = ArtifactType("artifact-shared-b", "共享 B", cap=3, rarity=0)
    request = SolveRequest(
        1, 2,
        (
            ArtifactInstance("a1", first.id, base_level=1),
            ArtifactInstance("a2", second.id, base_level=1),
        ),
        (),
        1000,
    )

    result = solve(request, {first.id: first, second.id: second}, {})

    assert result["solutionStatus"] == "OPTIMAL"
    assert result["diagnostics"]["levelTransformGroups"] == 1
    assert result["diagnostics"]["optimizationPhases"] <= 2


def test_tertiary_objective_avoids_negative_artifact_and_wasted_positive_effects():
    # Both artifact positions have identical primary/secondary scores (cap 0).
    # The tertiary objective must choose the cell that receives the positive
    # effect instead of the negative effect.
    tablet = TabletType(
        "tablet-mixed", "混合效果", "common", False, None, None,
        candidates={"1x3": ((0, 0, ((1, -2), (2, 3)), ()),)},
    )
    artifact = ArtifactType("artifact-zero", "零等级", cap=0, rarity=0)
    request = SolveRequest(
        1, 3,
        (ArtifactInstance("a1", artifact.id),),
        (TabletInstance("t1", tablet.id, fixed_cell=0),),
        3000,
    )
    result = solve(request, {artifact.id: artifact}, {tablet.id: tablet})
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["primaryObjective"] == result["secondaryObjective"] == 0
    assert result["tertiaryStatus"] == "OPTIMAL"
    assert result["tertiaryObjective"] == 0
    assert result["artifacts"][0]["cell"] == 2
    assert validate_result(request, {artifact.id: artifact}, {tablet.id: tablet}, result) == []


def test_empty_cell_objective_prefers_bonus_on_free_cell_over_tablet_cell():
    choice = TabletType(
        "tablet-choice", "空闲格选择", "common", False, None, None,
        candidates={"1x4": (
            (0, 0, ((2, 2),), ()),
            (1, 0, ((3, 2),), ()),
        )},
    )
    inert = TabletType(
        "tablet-inert", "固定石板", "common", False, None, None,
        candidates={"1x4": ((3, 0, (), ()),)},
    )
    request = SolveRequest(
        1, 4, (),
        (
            TabletInstance("choice", choice.id),
            TabletInstance("inert", inert.id, fixed_cell=3),
        ),
        3000,
    )
    tablets = {choice.id: choice, inert.id: inert}
    result = solve(request, {}, tablets)
    placements = {item["instanceId"]: item["cell"] for item in result["placements"]}
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["tertiaryObjective"] == 2
    assert result["emptyCellStatus"] == "OPTIMAL"
    assert result["emptyCellObjective"] == 2
    assert placements["choice"] == 0
    assert validate_result(request, {}, tablets, result) == []


def test_harmony_priority_maximizes_neighboring_artifact_levels():
    harmony = ArtifactType(
        "artifact-harmony", "和谐之晶", cap=2, rarity=0, special_condition="nearby_levels",
    )
    high = ArtifactType("artifact-high", "高等级", cap=4, rarity=0)
    low = ArtifactType("artifact-low", "低等级", cap=3, rarity=0)
    request = SolveRequest(
        3, 3,
        (
            ArtifactInstance("harmony", harmony.id, base_level=1, special_priority=True),
            ArtifactInstance("high", high.id, base_level=4),
            ArtifactInstance("low", low.id, base_level=3),
        ),
        (), 3000,
    )
    result = solve(request, {item.id: item for item in (harmony, high, low)}, {})
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["specialObjective"] == 5000
    assert result["specialDetails"][0] == {
        "instanceId": "harmony", "typeId": harmony.id, "condition": "nearby_levels",
        "rawScore": 7, "maxScore": 7, "completion": 1000, "weight": 5,
        "weightedScore": 5000, "satisfied": True,
    }


def test_belt_priority_fills_top_row_and_keeps_belt_on_bottom():
    belt = ArtifactType(
        "artifact-belt", "多用途腰带", cap=0, rarity=0,
        criteria=("bottom",), special_condition="top_row_artifacts",
    )
    filler = ArtifactType("artifact-filler", "填充神器", cap=0, rarity=0)
    request = SolveRequest(
        2, 3,
        (ArtifactInstance("belt", belt.id, special_priority=True),) + tuple(
            ArtifactInstance(f"filler-{i}", filler.id) for i in range(3)
        ),
        (), 3000,
    )
    result = solve(request, {belt.id: belt, filler.id: filler}, {})
    placements = {item["instanceId"]: item["cell"] for item in result["placements"]}
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["specialObjective"] == 5000
    assert placements["belt"] >= 3
    assert all(placements[f"filler-{i}"] < 3 for i in range(3))


def test_telescope_priority_surrounds_it_with_planets():
    telescope = ArtifactType(
        "artifact-telescope", "巨型望远镜", cap=0, rarity=0,
        special_condition="nearby_planets",
    )
    planet = ArtifactType("artifact-yellow_planet", "黄色星球", cap=0, rarity=0, categories=("行星",))
    planet_support = ArtifactType("artifact-planet-log", "行星辅助物", cap=0, rarity=0, categories=("行星",))
    request = SolveRequest(
        3, 3,
        (ArtifactInstance("telescope", telescope.id, special_priority=True),) + tuple(
            ArtifactInstance(f"planet-{i}", planet.id) for i in range(3)
        ) + (ArtifactInstance("support", planet_support.id),),
        (), 3000,
    )
    result = solve(request, {item.id: item for item in (telescope, planet, planet_support)}, {})
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["specialObjective"] == 5000


def test_white_paper_priority_places_matching_categories_on_both_sides():
    paper = ArtifactType(
        "artifact-paper", "白纸", cap=0, rarity=0,
        special_condition="matching_side_categories",
    )
    matching = ArtifactType("artifact-matching", "同组合", cap=0, rarity=0, categories=("冰川",))
    other = ArtifactType("artifact-other", "其他组合", cap=0, rarity=0, categories=("余烬",))
    request = SolveRequest(
        1, 4,
        (
            ArtifactInstance("paper", paper.id, special_priority=True),
            ArtifactInstance("left", matching.id), ArtifactInstance("right", matching.id),
            ArtifactInstance("other", other.id),
        ),
        (), 3000,
    )
    result = solve(request, {item.id: item for item in (paper, matching, other)}, {})
    placements = {item["instanceId"]: item["cell"] for item in result["placements"]}
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["specialObjective"] == 5000
    assert {placements["left"], placements["right"]} == {
        placements["paper"] - 1, placements["paper"] + 1,
    }


def test_gold_needle_priority_uses_manually_selected_target():
    needle = ArtifactType(
        "artifact-needle", "北向的金色针", cap=0, rarity=0, special_condition="target_above",
    )
    target = ArtifactType("artifact-target", "指定目标", cap=0, rarity=0)
    other = ArtifactType("artifact-other", "其他神器", cap=0, rarity=0)
    request = SolveRequest(
        2, 2,
        (
            ArtifactInstance(
                "needle", needle.id, special_priority=True,
                special_target_instance_id="target",
            ),
            ArtifactInstance("target", target.id), ArtifactInstance("other", other.id),
        ),
        (), 3000,
    )
    result = solve(request, {item.id: item for item in (needle, target, other)}, {})
    placements = {item["instanceId"]: item["cell"] for item in result["placements"]}
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["specialObjective"] == 5000
    assert placements["target"] == placements["needle"] - request.cols


def test_special_priority_uses_artifact_weight_when_conditions_conflict():
    needle = ArtifactType(
        "artifact-needle", "北向的金色针", cap=0, rarity=0, special_condition="target_above",
    )
    target = ArtifactType("artifact-target", "共同目标", cap=0, rarity=0)
    request = SolveRequest(
        2, 2,
        (
            ArtifactInstance(
                "high", needle.id, weight=10, special_priority=True,
                special_target_instance_id="target",
            ),
            ArtifactInstance(
                "low", needle.id, weight=1, special_priority=True,
                special_target_instance_id="target",
            ),
            ArtifactInstance("target", target.id),
        ),
        (), 3000,
    )
    result = solve(request, {needle.id: needle, target.id: target}, {})
    placements = {item["instanceId"]: item["cell"] for item in result["placements"]}
    details = {item["instanceId"]: item for item in result["specialDetails"]}
    assert result["solutionStatus"] == "OPTIMAL"
    assert placements["target"] == placements["high"] - request.cols
    assert placements["target"] != placements["low"] - request.cols
    assert result["specialObjective"] == 10_000
    assert details["high"]["weightedScore"] == 10_000
    assert details["low"]["weightedScore"] == 0


def test_special_completion_is_normalized_before_weighting():
    harmony = ArtifactType(
        "artifact-harmony", "和谐之晶", cap=2, rarity=0, special_condition="nearby_levels",
    )
    neighbor = ArtifactType("artifact-neighbor", "邻近神器", cap=4, rarity=0)
    request = SolveRequest(
        3, 3,
        (
            ArtifactInstance("harmony", harmony.id, weight=3, fixed_cell=0, special_priority=True),
            ArtifactInstance("near", neighbor.id, base_level=4, fixed_cell=1),
            ArtifactInstance("far", neighbor.id, base_level=4, fixed_cell=8),
        ),
        (), 3000,
    )
    result = solve(request, {harmony.id: harmony, neighbor.id: neighbor}, {})
    detail = result["specialDetails"][0]
    assert result["solutionStatus"] == "OPTIMAL"
    assert (detail["rawScore"], detail["maxScore"]) == (4, 8)
    assert detail["completion"] == 500
    assert detail["weightedScore"] == result["specialObjective"] == 1500


def test_special_priority_takes_precedence_over_level_objectives():
    # The target earns +1 from the tablet only on cell 3, but the needle's
    # enabled special effect comes first: it must keep the target directly
    # above the needle even though that forfeits the level points.
    needle = ArtifactType(
        "artifact-needle", "北向的金色针", cap=0, rarity=0, special_condition="target_above",
    )
    target = ArtifactType("artifact-target", "等级目标", cap=1, rarity=0)
    tablet = TabletType(
        "tablet-fixed", "固定加成", "common", False, None, None,
        candidates={"2x2": ((0, 0, ((3, 1),), ()),)},
    )
    request = SolveRequest(
        2, 2,
        (
            ArtifactInstance(
                "needle", needle.id, weight=10, special_priority=True,
                special_target_instance_id="target",
            ),
            ArtifactInstance("target", target.id),
        ),
        (TabletInstance("tablet", tablet.id, fixed_cell=0),), 3000,
    )
    result = solve(request, {needle.id: needle, target.id: target}, {tablet.id: tablet})
    placements = {item["instanceId"]: item["cell"] for item in result["placements"]}
    details = {item["instanceId"]: item for item in result["artifacts"]}
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["specialObjective"] == 10_000
    assert placements["target"] == placements["needle"] - request.cols
    assert result["primaryObjective"] == 0
    assert result["secondaryObjective"] == 0
    assert details["target"]["level"] == 0
    assert validate_result(request, {needle.id: needle, target.id: target}, {tablet.id: tablet}, result) == []


def test_pre_stopped_search_never_claims_optimality():
    controller = StopController()
    controller.stop()
    request = SolveRequest(1, 1, (ArtifactInstance("a1", ARTIFACT.id),), (), 1000)
    result = solve(request, {ARTIFACT.id: ARTIFACT}, {}, controller)
    assert result["solutionStatus"] == "STOPPED"
    if result["placements"]:
        assert validate_result(request, {ARTIFACT.id: ARTIFACT}, {}, result) == []
