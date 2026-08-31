import json
import threading
import urllib.error
import urllib.request

import pytest

from app.catalog import artifact_types, tablet_types
from app.game_bridge import GameBridgeError, inventory_to_solve_payload
from app.server import create_server, runtime_info_path, write_runtime_info


def _sample_inventory(*, complete: bool = True, unmapped=None):
    artifact = next(item for item in artifact_types() if item.cap >= 1)
    tablet = next(item for item in tablet_types() if item.id == "tablet-fate")
    inventory = {
        "grid": {"cellCount": 2, "doubleLevelCells": []},
        "artifacts": [{
            "instanceId": "game-a-11",
            "typeId": artifact.id,
            "weight": 5,
            "baseLevel": 0,
            "minLevel": None,
            "exactLevel": None,
        }],
        "tablets": [{
            "instanceId": "game-t-22",
            "typeId": tablet.id,
            "fixedCell": None,
            "fixedRotation": None,
            "preferredRotation": 0,
        }],
        "customTabletTypes": [],
        "unmapped": unmapped if unmapped is not None else [],
        "source": {
            "fingerprint": "f" * 64,
            "assemblySha256": "abc",
            "cellCount": 2,
            "complete": complete,
            "items": [
                {"solverInstanceId": "game-a-11", "instanceId": 11, "kind": "artifact", "cell": 0},
                {"solverInstanceId": "game-t-22", "instanceId": 22, "kind": "tablet", "cell": 1, "rotation": 0},
            ],
        },
    }
    return inventory, artifact, tablet


def test_inventory_to_solve_payload_builds_game_source_request():
    inventory, artifact, tablet = _sample_inventory()
    payload = inventory_to_solve_payload(inventory, fast_mode=True, time_limit_ms=15_000)
    assert payload["grid"] == {"cellCount": 2, "doubleLevelCells": []}
    assert payload["artifacts"][0]["typeId"] == artifact.id
    assert payload["tablets"][0]["typeId"] == tablet.id
    assert payload["options"] == {"timeLimitMs": 15_000, "workerCount": 0, "fastMode": True}
    assert payload["gameSource"]["fingerprint"] == "f" * 64


def test_inventory_to_solve_payload_rejects_unmapped_items():
    inventory, _, _ = _sample_inventory(unmapped=[{"name": "未知物品", "entityId": 999}])
    with pytest.raises(GameBridgeError, match="无法识别"):
        inventory_to_solve_payload(inventory)


@pytest.fixture
def live_server():
    server, token = create_server(0, "test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_address[1]}", token
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _request(url, token=None, data=None, method=None):
    headers = {}
    if token:
        headers["X-Sephiria-Token"] = token
    if data is not None:
        data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers, method=method),
        timeout=30,
    )


def test_auto_organize_endpoint_runs_pipeline(live_server, monkeypatch):
    server, base, token = live_server
    inventory, _, _ = _sample_inventory()
    applied = []

    monkeypatch.setattr("app.server.read_game_inventory", lambda: inventory)
    monkeypatch.setattr("app.server.apply_game_arrangement", lambda command: applied.append(command) or {
        "ok": True,
        "code": "APPLIED",
        "message": "done",
        "inventoryFingerprint": "e" * 64,
        "moves": 2,
        "rotations": 1,
        "rolledBack": False,
    })

    response = json.load(_request(base + "/api/auto-organize", token, {}))
    assert response["ok"] is True
    assert response["solutionStatus"] == "OPTIMAL"
    assert response["moves"] == 2
    assert len(applied) == 1
    assert applied[0]["assemblySha256"] == "abc"


def test_auto_organize_endpoint_requires_auth(live_server):
    _, base, _token = live_server
    with pytest.raises(urllib.error.HTTPError) as denied:
        _request(base + "/api/auto-organize", None, {})
    assert denied.value.code == 403


def test_write_runtime_info_uses_cache_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("SEPHIRIA_CACHE_DIR", str(tmp_path))
    write_runtime_info(8765, "secret-token")
    payload = json.loads(runtime_info_path().read_text(encoding="utf-8"))
    assert payload == {
        "port": 8765,
        "token": "secret-token",
        "url": "http://127.0.0.1:8765",
    }
