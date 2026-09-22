import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from app.catalog import artifact_types, tablet_types
from app.game_bridge import GameApplyError
from app.server import AppState, create_server


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("SEPHIRIA_CACHE_DIR", str(tmp_path))
    server, token = create_server(0, "test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", token
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def request(url, token=None, data=None, method=None):
    headers = {}
    if token:
        headers["X-Sephiria-Token"] = token
    if data is not None:
        data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=10)


def test_static_catalog_auth_and_async_solve(live_server, tmp_path):
    base, token = live_server
    assert json.load(request(base + "/api/sharing", token))["needsConsent"] is True
    with pytest.raises(urllib.error.HTTPError) as denied_sharing:
        request(base + "/api/sharing", data={"enabled": True})
    assert denied_sharing.value.code == 403
    assert json.load(request(base + "/api/sharing", token, {"enabled": True}))["enabled"] is True
    assert request(base + "/").status == 200
    game_read_script = request(base + "/game_read_state.js")
    assert game_read_script.status == 200
    assert b"SAME_RUN_THRESHOLD" in game_read_script.read()
    with pytest.raises(urllib.error.HTTPError) as denied:
        request(base + "/api/catalog")
    assert denied.value.code == 403
    catalog = json.load(request(base + "/api/catalog", token))
    assert len(catalog["artifacts"]) == 281
    assert len(catalog["tablets"]) == 63

    artifact = next(item for item in artifact_types() if item.cap >= 1)
    tablet = next(item for item in tablet_types() if item.id == "tablet-fate")
    payload = {
        "grid": {"cellCount": 2},
        "artifacts": [{"instanceId": "a1", "typeId": artifact.id, "fixedCell": 1}],
        "tablets": [{"instanceId": "t1", "typeId": tablet.id, "fixedCell": 0}],
        "options": {"timeLimitMs": 3000},
    }
    started = json.load(request(base + "/api/solve", token, payload))
    for _ in range(50):
        status = json.load(request(base + f"/api/solve/{started['solveId']}", token))
        if status["jobStatus"] in ("FINISHED", "FAILED"):
            break
        time.sleep(0.05)
    assert status["jobStatus"] == "FINISHED"
    assert status["result"]["solutionStatus"] == "OPTIMAL"
    assert len(status["result"]["placements"]) == 2
    assert status["recordedBuild"] is True
    record = json.loads((tmp_path / "recorded_builds.jsonl").read_text(encoding="utf-8"))
    assert record["schemaVersion"] == 2
    assert record["event"] == "solve"
    assert record["gameSourceAtSolve"] is None
    assert record["request"]["artifacts"][0]["instanceId"] == "a1"
    assert record["solution"]["solutionStatus"] == "OPTIMAL"
    assert json.load(request(base + "/api/sharing", token))["pending"] == 1
    disabled = json.load(request(base + "/api/sharing", token, {"enabled": False}))
    assert disabled["enabled"] is False and disabled["pending"] == 0
    assert json.loads((tmp_path / "sharing.json").read_text())["enabled"] is False


def test_game_inventory_bridge_endpoint(live_server, monkeypatch):
    base, token = live_server
    inventory = {
        "grid": {"cellCount": 12}, "artifacts": [], "tablets": [],
        "unmapped": [], "source": {"assemblySha256": "abc", "capturedAt": "now"},
    }
    monkeypatch.setattr("app.server.read_game_inventory", lambda: inventory)
    assert json.load(request(base + "/api/game-inventory", token)) == inventory


def test_gpu_option_uses_seed_and_falls_back_to_cpu(live_server, monkeypatch):
    base, token = live_server
    artifact = next(item for item in artifact_types() if item.cap >= 1 and not item.criteria)

    def fake_seed(_state, solve_request, artifacts, tablets):
        from tools.gpu_search import result_from_placements
        placements = [{"instanceId": "a1", "kind": "artifact", "cell": 0}]
        return {
            "placements": placements,
            "result": result_from_placements(solve_request, artifacts, tablets, placements),
            "elapsedMs": 2.5, "evaluations": 123,
        }

    monkeypatch.setattr(AppState, "_gpu_seed", fake_seed)
    payload = {
        "grid": {"cellCount": 2},
        "artifacts": [{"instanceId": "a1", "typeId": artifact.id}],
        "tablets": [],
        "options": {"timeLimitMs": 1000, "gpuAcceleration": True},
    }
    started = json.load(request(base + "/api/solve", token, payload))
    for _ in range(50):
        status = json.load(request(base + f"/api/solve/{started['solveId']}", token))
        if status["jobStatus"] in ("FINISHED", "FAILED"):
            break
        time.sleep(0.05)
    assert status["jobStatus"] == "FINISHED"
    assert status["result"]["diagnostics"]["gpuUsed"] is True
    assert status["result"]["diagnostics"]["gpuEvaluations"] == 123

    def unavailable(*_args):
        raise RuntimeError("CUDA unavailable")

    monkeypatch.setattr(AppState, "_gpu_seed", unavailable)
    payload["grid"]["cellCount"] = 3
    started = json.load(request(base + "/api/solve", token, payload))
    for _ in range(50):
        status = json.load(request(base + f"/api/solve/{started['solveId']}", token))
        if status["jobStatus"] in ("FINISHED", "FAILED"):
            break
        time.sleep(0.05)
    diagnostics = status["result"]["diagnostics"]
    assert status["jobStatus"] == "FINISHED"
    assert diagnostics["gpuRequested"] is True
    assert diagnostics["gpuUsed"] is False
    assert diagnostics["gpuFallbackReason"] == "CUDA unavailable"


def test_local_recording_follows_sharing_consent(live_server, tmp_path):
    """Without sharing consent the tool records nothing locally either."""
    base, token = live_server
    artifact = next(item for item in artifact_types() if item.cap >= 1)
    payload = {
        "grid": {"cellCount": 1},
        "artifacts": [{"instanceId": "a1", "typeId": artifact.id}],
        "tablets": [],
        "options": {"timeLimitMs": 3000},
    }

    def solve_once():
        started = json.load(request(base + "/api/solve", token, payload))
        for _ in range(50):
            status = json.load(request(base + f"/api/solve/{started['solveId']}", token))
            if status["jobStatus"] in ("FINISHED", "FAILED"):
                return status
            time.sleep(0.05)
        raise AssertionError("solve did not finish")

    assert json.load(request(base + "/api/sharing", token))["needsConsent"] is True
    assert solve_once()["recordedBuild"] is None
    assert not (tmp_path / "recorded_builds.jsonl").exists()

    assert json.load(request(base + "/api/sharing", token, {"enabled": True}))["enabled"] is True
    assert solve_once()["recordedBuild"] is True
    record = json.loads((tmp_path / "recorded_builds.jsonl").read_text(encoding="utf-8"))
    assert record["event"] == "solve"
    assert record["request"]["artifacts"][0]["instanceId"] == "a1"


def test_apply_arrangement_uses_finished_game_solve(live_server, monkeypatch, tmp_path):
    base, token = live_server
    # Enabling auto-share is also what turns on local build recording.
    assert json.load(request(base + "/api/sharing", token, {"enabled": True}))["enabled"] is True
    artifact = next(item for item in artifact_types() if item.cap >= 1)
    tablet = next(item for item in tablet_types() if item.id == "tablet-fate")
    applied = []

    def fake_apply(command):
        applied.append(command)
        return {
            "ok": True, "code": "APPLIED", "message": "done",
            "inventoryFingerprint": "e" * 64, "moves": 1,
            "rotations": 0, "rolledBack": False,
        }

    monkeypatch.setattr("app.server.apply_game_arrangement", fake_apply)
    current_inventory = {
        "grid": {"cellCount": 2},
        "artifacts": [{"instanceId": "game-a-11", "typeId": artifact.id}],
        "tablets": [{"instanceId": "game-t-22", "typeId": tablet.id}],
        "source": {"fingerprint": "c" * 64, "capturedAt": "just-before-apply"},
    }
    monkeypatch.setattr("app.server.read_game_inventory", lambda: current_inventory)
    payload = {
        "grid": {"cellCount": 2},
        "artifacts": [{"instanceId": "game-a-11", "typeId": artifact.id}],
        "tablets": [{"instanceId": "game-t-22", "typeId": tablet.id}],
        "gameSource": {
            "fingerprint": "f" * 64, "assemblySha256": "abc", "cellCount": 2,
            "items": [
                {"solverInstanceId": "game-a-11", "instanceId": 11, "kind": "artifact", "cell": 0},
                {"solverInstanceId": "game-t-22", "instanceId": 22, "kind": "tablet", "cell": 1},
            ],
        },
        "options": {"timeLimitMs": 3000},
    }
    started = json.load(request(base + "/api/solve", token, payload))
    for _ in range(50):
        status = json.load(request(base + f"/api/solve/{started['solveId']}", token))
        if status["jobStatus"] in ("FINISHED", "FAILED"):
            break
        time.sleep(0.05)
    response = json.load(request(base + "/api/apply-arrangement", token, {
        "solveId": started["solveId"],
    }))
    assert response["code"] == "APPLIED"
    assert response["recordedBuild"] is True
    assert len(applied) == 1
    assert applied[0]["assemblySha256"] == "abc"
    assert {item["instanceId"] for item in applied[0]["placements"]} == {11, 22}
    record_path = tmp_path / "recorded_builds.jsonl"
    records = [json.loads(line) for line in record_path.read_text(encoding="utf-8").splitlines()]
    # Consenting to auto-share records the solve as well as the application.
    assert [record["event"] for record in records] == ["solve", "apply"]
    record = records[1]
    assert record["schemaVersion"] == 2
    assert record["solveId"] == started["solveId"]
    assert record["request"]["grid"] == {"cellCount": 2}
    assert record["gameSourceAtSolve"]["fingerprint"] == "f" * 64
    assert record["preApplyInventory"] == current_inventory
    assert record["solution"]["placements"]
    assert record["applyCommand"]["placements"]
    assert record["outcome"]["ok"] is True


def test_apply_failure_is_recorded(live_server, monkeypatch, tmp_path):
    base, token = live_server
    assert json.load(request(base + "/api/sharing", token, {"enabled": True}))["enabled"] is True
    artifact = next(item for item in artifact_types() if item.cap >= 1)

    def fail_apply(_command):
        raise GameApplyError("INVENTORY_CHANGED", "游戏背包已变化")

    monkeypatch.setattr("app.server.apply_game_arrangement", fail_apply)
    monkeypatch.setattr("app.server.read_game_inventory", lambda: {
        "grid": {"cellCount": 1},
        "source": {"fingerprint": "c" * 64, "capturedAt": "just-before-apply"},
        "artifacts": [], "tablets": [],
    })
    started = json.load(request(base + "/api/solve", token, {
        "grid": {"cellCount": 1},
        "artifacts": [{"instanceId": "game-a-11", "typeId": artifact.id}],
        "tablets": [],
        "gameSource": {
            "fingerprint": "f" * 64, "assemblySha256": "abc", "cellCount": 1,
            "items": [
                {"solverInstanceId": "game-a-11", "instanceId": 11, "kind": "artifact", "cell": 0},
            ],
        },
        "options": {"timeLimitMs": 3000},
    }))
    for _ in range(50):
        status = json.load(request(base + "/api/solve/" + started["solveId"], token))
        if status["jobStatus"] in ("FINISHED", "FAILED"):
            break
        time.sleep(0.05)

    with pytest.raises(urllib.error.HTTPError) as rejected:
        request(base + "/api/apply-arrangement", token, {
            "solveId": started["solveId"],
        })
    assert rejected.value.code == 409
    response = json.load(rejected.value)
    assert response["error"]["code"] == "INVENTORY_CHANGED"
    assert response["recordedBuild"] is True
    record = json.loads(
        (tmp_path / "recorded_builds.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert record["event"] == "apply"
    assert record["outcome"] == {
        "ok": False,
        "error": {"code": "INVENTORY_CHANGED", "message": "游戏背包已变化"},
    }


def test_apply_arrangement_rejects_non_game_solve(live_server):
    base, token = live_server
    artifact = next(item for item in artifact_types() if item.cap >= 1)
    started = json.load(request(base + "/api/solve", token, {
        "grid": {"cellCount": 1},
        "artifacts": [{"instanceId": "a1", "typeId": artifact.id}],
        "tablets": [],
        "options": {"timeLimitMs": 3000},
    }))
    for _ in range(50):
        status = json.load(request(base + f"/api/solve/{started['solveId']}", token))
        if status["jobStatus"] in ("FINISHED", "FAILED"):
            break
        time.sleep(0.05)
    with pytest.raises(urllib.error.HTTPError) as rejected:
        request(base + "/api/apply-arrangement", token, {"solveId": started["solveId"]})
    assert rejected.value.code == 409
    assert json.load(rejected.value)["error"]["code"] == "NO_GAME_SNAPSHOT"


def test_custom_tablet_compose_and_solve_endpoints(live_server):
    base, token = live_server
    source = next(item for item in tablet_types() if item.id == "tablet-fate")
    composed = json.load(request(base + "/api/custom-tablet/compose", token, {
        "cellCount": 2,
        "name": "测试合成石板",
        "sources": [
            {"typeId": source.id, "rotation": 0},
            {"typeId": source.id, "rotation": 0},
        ],
        "customTabletTypes": [],
    }))
    assert composed["custom"] is True
    assert composed["id"].startswith("custom-tablet-")

    artifact = next(item for item in artifact_types() if item.cap >= 1)
    payload = {
        "grid": {"cellCount": 2},
        "artifacts": [{"instanceId": "a1", "typeId": artifact.id}],
        "tablets": [{"instanceId": "t1", "typeId": composed["id"]}],
        "customTabletTypes": [composed],
        "options": {"timeLimitMs": 3000},
    }
    started = json.load(request(base + "/api/solve", token, payload))
    for _ in range(50):
        status = json.load(request(base + f"/api/solve/{started['solveId']}", token))
        if status["jobStatus"] in ("FINISHED", "FAILED"):
            break
        time.sleep(0.05)
    assert status["jobStatus"] == "FINISHED"
    assert status["result"]["solutionStatus"] == "OPTIMAL"
