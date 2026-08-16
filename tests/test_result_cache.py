import json
import time

from app.catalog import artifact_types, tablet_types
from app.models import ArtifactInstance, SolveRequest, TabletInstance
from app.result_cache import ResultCache
from app.server import AppState
from app.solver import solve
from app.validation import validate_result


def maps():
    artifacts = {item.id: item for item in artifact_types()}
    tablets = {item.id: item for item in tablet_types()}
    return artifacts, tablets


def plain_type():
    return next(item for item in artifact_types()
                if item.cap >= 2 and not item.criteria and not item.special_condition)


def build_request(prefix="a", order=(0, 1, 2), time_limit=5000, weight=3, min_level=None):
    kind = plain_type()
    artifacts = [
        ArtifactInstance(f"{prefix}{index}", kind.id, weight=weight, min_level=min_level)
        for index in order
    ]
    return SolveRequest(
        2, 4, tuple(artifacts),
        (TabletInstance(f"{prefix}-t", "tablet-dry"),),
        time_limit,
    )


def test_key_ignores_instance_ids_order_and_time_limit():
    artifacts, tablets = maps()
    first = ResultCache("Z:/no-write")
    reference = build_request()
    key_a, _, _ = first.key(reference, {})
    key_b, ids_b, _ = first.key(build_request(prefix="b", order=(2, 1, 0), time_limit=9000), {})
    assert key_a == key_b
    assert ids_b == ["b0", "b1", "b2"]


def test_key_changes_with_settings_grid_and_custom_tablets():
    cache = ResultCache("Z:/no-write")
    assert cache.key(build_request(), {}) != cache.key(build_request(weight=4), {})
    assert cache.key(build_request(), {}) != cache.key(build_request(min_level=1), {})
    assert cache.key(build_request(), {}) != cache.key(build_request(order=(0, 1)), {})
    assert cache.key(build_request(), {}) != cache.key(
        build_request(), {"customTabletTypes": [{"id": "custom-x"}]})


def test_roundtrip_remaps_instance_ids_and_validates(tmp_path):
    artifacts, tablets = maps()
    cache = ResultCache(tmp_path)
    original = build_request()
    result = solve(original, artifacts, tablets)
    assert result["solutionStatus"] == "OPTIMAL"
    key, artifact_ids, tablet_ids = cache.key(original, {})
    cache.store(key, artifact_ids, tablet_ids, result)

    renamed = build_request(prefix="z", order=(2, 1, 0))
    new_key, new_artifact_ids, new_tablet_ids = cache.key(renamed, {})
    assert new_key == key

    cached = cache.lookup(new_key, renamed, artifacts, tablets, new_artifact_ids, new_tablet_ids)
    assert cached is not None
    assert cached["fromCache"] is True
    assert cached["primaryObjective"] == result["primaryObjective"]
    placed = {item["instanceId"] for item in cached["placements"]}
    assert placed == {item.instance_id for item in (*renamed.artifacts, *renamed.tablets)}
    assert validate_result(renamed, artifacts, tablets, cached) == []


def test_infeasible_is_cached_but_feasible_is_not(tmp_path):
    artifacts, tablets = maps()
    cache = ResultCache(tmp_path)
    impossible = build_request(min_level=9)
    result = solve(impossible, artifacts, tablets)
    assert result["solutionStatus"] == "INFEASIBLE"
    key, artifact_ids, tablet_ids = cache.key(impossible, {})
    cache.store(key, artifact_ids, tablet_ids, result)
    assert cache.lookup(key, impossible, artifacts, tablets, artifact_ids, tablet_ids) is not None

    cache.store("feasible-key", artifact_ids, tablet_ids,
                {"solutionStatus": "FEASIBLE", "placements": []})
    assert cache.lookup("feasible-key", impossible, artifacts, tablets,
                        artifact_ids, tablet_ids) is None


def test_invalid_cached_layout_is_evicted(tmp_path):
    artifacts, tablets = maps()
    cache = ResultCache(tmp_path)
    original = build_request()
    result = solve(original, artifacts, tablets)
    key, artifact_ids, tablet_ids = cache.key(original, {})
    cache.store(key, artifact_ids, tablet_ids, result)

    stored = json.loads(cache.path.read_text(encoding="utf-8"))
    stored["entries"][key]["result"]["placements"][0]["cell"] = \
        stored["entries"][key]["result"]["placements"][1]["cell"]
    cache.path.write_text(json.dumps(stored), encoding="utf-8")

    reloaded = ResultCache(tmp_path)
    assert reloaded.lookup(key, original, artifacts, tablets, artifact_ids, tablet_ids) is None
    assert key not in json.loads(reloaded.path.read_text(encoding="utf-8"))["entries"]


def test_entries_are_trimmed_to_the_limit(tmp_path):
    cache = ResultCache(tmp_path, max_entries=2)
    for index in range(3):
        cache.store(f"key-{index}", [f"a{index}"], [], {"solutionStatus": "INFEASIBLE"})
        time.sleep(0.01)
    stored = json.loads(cache.path.read_text(encoding="utf-8"))["entries"]
    assert set(stored) == {"key-1", "key-2"}


def test_app_state_returns_cached_result_without_solving(tmp_path):
    state = AppState("token", ResultCache(tmp_path))
    kind = plain_type()
    payload = {
        "grid": {"cellCount": 8},
        "artifacts": [
            {"instanceId": "a1", "typeId": kind.id, "weight": 3},
            {"instanceId": "a2", "typeId": kind.id, "weight": 3},
        ],
        "tablets": [{"instanceId": "t1", "typeId": "tablet-dry"}],
        "options": {"timeLimitMs": 5000},
    }
    first = state.create_job(json.loads(json.dumps(payload)))
    for _ in range(200):
        if first.status in ("FINISHED", "FAILED"):
            break
        time.sleep(0.05)
    assert first.status == "FINISHED"
    assert first.result["solutionStatus"] == "OPTIMAL"
    assert first.result.get("fromCache") is None

    payload["options"]["timeLimitMs"] = 30000
    payload["options"]["fastMode"] = True
    payload["artifacts"] = [
        {"instanceId": "b2", "typeId": kind.id, "weight": 3},
        {"instanceId": "b1", "typeId": kind.id, "weight": 3},
    ]
    second = state.create_job(payload)
    assert second.status == "FINISHED"
    assert second.result["fromCache"] is True
    assert {item["instanceId"] for item in second.result["placements"]} == {"b1", "b2", "t1"}
