from app.custom_tablets import compose_custom_tablet, parse_custom_tablet_types
from app.models import ArtifactInstance, ArtifactType, SolveRequest, TabletInstance, TabletType
from app.solver import solve
from app.validation import validate_result


def custom_payload(candidates, *, rotatable=False):
    return {
        "customTabletTypes": [{
            "id": "custom-tablet-0123456789abcdef",
            "name": "自定义测试石板",
            "rotatable": rotatable,
            "cellCount": 3,
            "queryRotations": ["RIGHT 1"],
            "conditionRotations": ["LEFT ITEM"],
            "candidates": candidates,
        }],
    }


def test_parse_custom_tablet_candidate_extensions():
    payload = custom_payload([[0, 0, [[2, 1]], [1], [0], [[2, 2]], [[1, "ITEM"]]]])
    tablet = parse_custom_tablet_types(payload)["custom-tablet-0123456789abcdef"]
    candidate = tablet.candidates["1x6"][0]
    assert candidate == (0, 0, ((2, 1),), (1,), (0,), ((2, 2),), ((1, "ITEM"),))


def test_custom_condition_requires_item_before_effect_applies():
    tablet = parse_custom_tablet_types(custom_payload([
        [0, 0, [[2, 2]], [], [], [], [[1, "ITEM"]]],
    ]))["custom-tablet-0123456789abcdef"]
    artifact = ArtifactType("artifact-a", "目标", 5, 0)
    filler = ArtifactType("artifact-b", "条件物品", 0, 0)
    request = SolveRequest(
        1, 6,
        (ArtifactInstance("target", artifact.id, fixed_cell=2),
         ArtifactInstance("filler", filler.id, fixed_cell=1)),
        (TabletInstance("tablet", tablet.id, fixed_cell=0),), 3000,
        actual_cell_count=3,
    )
    result = solve(request, {artifact.id: artifact, filler.id: filler}, {tablet.id: tablet})
    details = {item["instanceId"]: item for item in result["artifacts"]}
    assert result["solutionStatus"] == "OPTIMAL"
    assert details["target"]["level"] == 2
    placement = next(item for item in result["placements"] if item["instanceId"] == "tablet")
    assert placement["applied"]
    assert placement["rangeCells"] == [1, 2]


def test_custom_disable_and_multiplier_match_game_order():
    tablet = parse_custom_tablet_types(custom_payload([
        [0, 0, [[2, 1]], [], [1], [[2, 2]], []],
    ]))["custom-tablet-0123456789abcdef"]
    artifact = ArtifactType("artifact-a", "目标", 6, 0)
    disabled = ArtifactType("artifact-b", "禁用目标", 6, 0)
    request = SolveRequest(
        1, 6,
        (ArtifactInstance("target", artifact.id, base_level=1, fixed_cell=2),
         ArtifactInstance("disabled", disabled.id, base_level=6, fixed_cell=1)),
        (TabletInstance("tablet", tablet.id, fixed_cell=0),), 3000,
        actual_cell_count=3,
    )
    result = solve(request, {artifact.id: artifact, disabled.id: disabled}, {tablet.id: tablet})
    details = {item["instanceId"]: item for item in result["artifacts"]}
    assert details["target"]["level"] == 4  # (base 1 + additive 1) * 2
    assert details["target"]["multiplier"] == 2
    assert not details["disabled"]["active"]
    assert details["disabled"]["disabled"]
    assert validate_result(
        request,
        {artifact.id: artifact, disabled.id: disabled},
        {tablet.id: tablet},
        result,
    ) == []


def test_candidate_cache_distinguishes_different_rules_with_the_same_tablet_id():
    first = parse_custom_tablet_types(custom_payload([
        [0, 0, [[2, 2]], [], [], [], []],
    ]))["custom-tablet-0123456789abcdef"]
    second = parse_custom_tablet_types(custom_payload([
        [0, 0, [[2, 5]], [], [], [], []],
    ]))["custom-tablet-0123456789abcdef"]
    artifact = ArtifactType("artifact-cache-target", "缓存目标", 10, 0)
    request = SolveRequest(
        1, 6,
        (ArtifactInstance("target", artifact.id, fixed_cell=2),),
        (TabletInstance("tablet", first.id, fixed_cell=0),), 3000,
        actual_cell_count=3,
    )

    first_result = solve(request, {artifact.id: artifact}, {first.id: first})
    second_result = solve(request, {artifact.id: artifact}, {second.id: second})

    assert first_result["artifacts"][0]["level"] == 2
    assert second_result["artifacts"][0]["level"] == 5


def test_compose_combines_source_ranges_and_consumes_source_rotations():
    left = TabletType(
        "tablet-left", "左", "common", True, None, None,
        candidates={"1x6": tuple(
            (cell, rotation, ((cell + 1, 1),) if cell + 1 < 3 else (), ())
            for cell in range(3) for rotation in range(4)
        )},
    )
    right = TabletType(
        "tablet-right", "右", "common", True, None, None,
        candidates={"1x6": tuple(
            (cell, rotation, ((cell + 2, 2),) if cell + 2 < 3 else (), ())
            for cell in range(3) for rotation in range(4)
        )},
    )
    result = compose_custom_tablet({
        "cellCount": 3,
        "name": "合成结果",
        "sources": [{"typeId": left.id, "rotation": 0}, {"typeId": right.id, "rotation": 0}],
        "customTabletTypes": [],
    }, {left.id: left, right.id: right})
    first = next(candidate for candidate in result["candidates"]
                 if candidate[0] == 0 and candidate[1] == 0)
    assert result["rotatable"] is True
    assert first[2] == [[1, 1], [2, 2]]


def test_compose_accepts_any_rotation_for_rotation_invariant_curse():
    curse = TabletType(
        "tablet-curse", "诅咒", "special", True, None, None,
        (("CHECKERBOARD2", 1), ("CHECKERBOARD", -1)),
    )
    right = TabletType(
        "tablet-right", "右侧 +2", "common", True, None, None, ((1, 2),),
    )
    result = compose_custom_tablet({
        "cellCount": 6,
        "name": "诅咒合成",
        "sources": [
            {"typeId": curse.id, "rotation": 3},
            {"typeId": right.id, "rotation": 0},
        ],
        "customTabletTypes": [],
    }, {curse.id: curse, right.id: right})
    first = next(candidate for candidate in result["candidates"]
                 if candidate[0] == 0 and candidate[1] == 0)
    assert result["rotatable"] is True
    assert first[2] == [[1, 3], [2, -1], [3, 1], [4, -1], [5, 1]]
