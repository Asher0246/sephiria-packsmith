import pytest

from app.models import RequestError, parse_request


ARTIFACT_IDS = {"artifact-a"}
TABLET_IDS = {"tablet-t"}


def valid_payload():
    return {
        "grid": {"cellCount": 11},
        "artifacts": [{"instanceId": "a1", "typeId": "artifact-a", "weight": 5, "baseLevel": 1, "minLevel": 2, "exactLevel": 3, "fixedCell": 1}],
        "tablets": [{"instanceId": "t1", "typeId": "tablet-t", "fixedCell": 4, "fixedRotation": 2}],
        "options": {"timeLimitMs": 3000, "workerCount": 16},
    }


def test_parse_full_constraint_request():
    request = parse_request(valid_payload(), ARTIFACT_IDS, TABLET_IDS)
    assert (request.rows, request.cols, request.cell_count, request.time_limit_ms) == (2, 6, 11, 3000)
    assert request.worker_count == 16
    assert request.artifacts[0].fixed_cell == 1
    assert request.artifacts[0].min_level == 2
    assert request.artifacts[0].exact_level == 3
    assert request.artifacts[0].weight == 5
    assert request.artifacts[0].base_level == 1
    assert request.tablets[0].fixed_rotation == 2


def test_artifact_weight_defaults_to_five():
    payload = valid_payload()
    payload["artifacts"][0].pop("weight")
    request = parse_request(payload, ARTIFACT_IDS, TABLET_IDS)
    assert request.artifacts[0].weight == 5


def test_parse_legacy_rectangular_grid():
    payload = valid_payload()
    payload["grid"] = {"rows": 2, "cols": 3}
    request = parse_request(payload, ARTIFACT_IDS, TABLET_IDS)
    assert (request.rows, request.cols, request.cell_count) == (2, 3, 6)


def test_parse_gold_needle_special_target():
    payload = valid_payload()
    payload["artifacts"] = [
        {
            "instanceId": "needle",
            "typeId": "artifact-unalloyed_gold_needle",
            "specialPriority": True,
            "specialTargetInstanceId": "target",
        },
        {"instanceId": "target", "typeId": "artifact-a"},
    ]
    request = parse_request(
        payload, {"artifact-a", "artifact-unalloyed_gold_needle"}, TABLET_IDS,
    )
    needle = request.artifacts[0]
    assert needle.special_priority is True
    assert needle.special_target_instance_id == "target"


@pytest.mark.parametrize("target", [None, "needle", "t1", "missing"])
def test_gold_needle_special_target_must_be_another_artifact(target):
    payload = valid_payload()
    needle = {
        "instanceId": "needle",
        "typeId": "artifact-unalloyed_gold_needle",
        "specialPriority": True,
    }
    if target is not None:
        needle["specialTargetInstanceId"] = target
    payload["artifacts"] = [needle, {"instanceId": "target", "typeId": "artifact-a"}]
    with pytest.raises(RequestError):
        parse_request(payload, {"artifact-a", "artifact-unalloyed_gold_needle"}, TABLET_IDS)


@pytest.mark.parametrize("mutator", [
    lambda p: p["grid"].update(cellCount=True),
    lambda p: p["options"].update(workerCount=65),
    lambda p: p["options"].update(workerCount=True),
    lambda p: p["artifacts"][0].update(weight=1.5),
    lambda p: p["artifacts"][0].update(baseLevel=-1),
    lambda p: p["artifacts"][0].update(specialPriority="yes"),
    lambda p: p["artifacts"][0].update(specialPriority=True),
    lambda p: p["artifacts"][0].update(fixedCell=99),
    lambda p: p["tablets"][0].update(typeId="missing"),
    lambda p: p["tablets"][0].update(instanceId="a1"),
])
def test_request_validation_is_strict(mutator):
    payload = valid_payload()
    mutator(payload)
    with pytest.raises(RequestError):
        parse_request(payload, ARTIFACT_IDS, TABLET_IDS)
