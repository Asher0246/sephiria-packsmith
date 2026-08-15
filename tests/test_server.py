import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from app.catalog import artifact_types, tablet_types
from app.server import create_server


@pytest.fixture
def live_server():
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


def test_static_catalog_auth_and_async_solve(live_server):
    base, token = live_server
    assert request(base + "/").status == 200
    game_read_script = request(base + "/game_read_state.js")
    assert game_read_script.status == 200
    assert b"SAME_RUN_THRESHOLD" in game_read_script.read()
    with pytest.raises(urllib.error.HTTPError) as denied:
        request(base + "/api/catalog")
    assert denied.value.code == 403
    catalog = json.load(request(base + "/api/catalog", token))
    assert len(catalog["artifacts"]) == 268
    assert len(catalog["tablets"]) == 61

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


def test_game_inventory_bridge_endpoint(live_server, monkeypatch):
    base, token = live_server
    inventory = {
        "grid": {"cellCount": 12}, "artifacts": [], "tablets": [],
        "unmapped": [], "source": {"assemblySha256": "abc", "capturedAt": "now"},
    }
    monkeypatch.setattr("app.server.read_game_inventory", lambda: inventory)
    assert json.load(request(base + "/api/game-inventory", token)) == inventory


def test_apply_arrangement_uses_finished_game_solve(live_server, monkeypatch):
    base, token = live_server
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
    assert len(applied) == 1
    assert applied[0]["assemblySha256"] == "abc"
    assert {item["instanceId"] for item in applied[0]["placements"]} == {11, 22}


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
