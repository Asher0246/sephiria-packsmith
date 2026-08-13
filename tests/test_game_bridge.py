import ctypes
import json
import os
import threading
import uuid
from ctypes import wintypes

import pytest

from app.game_bridge import (
    PIPE_PATH,
    GameApplyError,
    GameBridgeError,
    apply_game_arrangement,
    prepare_apply_command,
    read_game_inventory,
    translate_snapshot,
)


def snapshot():
    return {
        "version": 1,
        "ready": True,
        "width": 6,
        "cellCount": 17,
        "assemblySha256": "abc",
        "capturedAt": "2026-08-07T00:00:00Z",
        "artifacts": [{
            "entityId": 2, "instanceId": 101, "name": "雪花项链",
            "x": 1, "y": 0, "enchantLevel": 4, "temporaryLevel": 2, "displayedLevel": 3,
        }],
        "tablets": [{
            "entityId": 999, "instanceId": 202, "name": "骑士道",
            "objectName": "StoneTablet_chivalry", "rotation": 1,
        }],
    }


def test_translate_game_snapshot_to_solver_items():
    result = translate_snapshot(snapshot())
    assert result["grid"] == {"cellCount": 17}
    assert result["artifacts"] == [{
        "instanceId": "game-a-101", "typeId": "artifact-eye_crystal_necklace",
        "weight": 5, "baseLevel": 4, "minLevel": None, "exactLevel": None,
        "fixedCell": None, "specialPriority": False, "specialTargetInstanceId": None,
    }]
    assert result["tablets"] == [{
        "instanceId": "game-t-202", "typeId": "tablet-chivalry",
        "fixedCell": None, "fixedRotation": None,
    }]
    assert result["unmapped"] == []


def test_translate_snapshot_reports_unmapped_items_without_failing():
    value = snapshot()
    value["artifacts"][0] = {"entityId": 999999, "instanceId": 1, "name": "missing"}
    result = translate_snapshot(value)
    assert result["artifacts"] == []
    assert result["unmapped"] == [{"kind": "artifact", "entityId": 999999, "name": "missing"}]


def test_translate_hidden_heart_burden_from_game_entity_id():
    value = snapshot()
    value["artifacts"][0] = {
        "entityId": 1304, "instanceId": 303, "name": "心之重担", "temporaryLevel": 0,
    }
    result = translate_snapshot(value)
    assert result["artifacts"][0]["typeId"] == "artifact-heart_burden"
    assert result["unmapped"] == []


def test_translate_sword_earring_from_stable_game_entity_id():
    value = snapshot()
    value["artifacts"][0] = {
        "entityId": 1033, "instanceId": 304,
        "name": "uninitialized", "enchantLevel": 0,
    }
    result = translate_snapshot(value)
    assert result["artifacts"][0]["typeId"] == "artifact-sword_earring"
    assert result["unmapped"] == []


def test_translate_artifact_falls_back_to_temporary_level_for_v1_plugin():
    value = snapshot()
    value["artifacts"][0].pop("enchantLevel")
    result = translate_snapshot(value)
    assert result["artifacts"][0]["baseLevel"] == 2


def test_translate_progress_tablet_from_stable_entity_id():
    value = snapshot()
    value["tablets"][0] = {
        "entityId": 2053, "instanceId": 404, "name": "uninitialized",
        "objectName": "StoneTabletN-Progress(Clone)",
    }
    result = translate_snapshot(value)
    assert result["tablets"][0]["typeId"] == "tablet-development"
    assert result["unmapped"] == []


def test_translate_defensive_move_from_stable_entity_id():
    value = snapshot()
    value["tablets"][0] = {
        "entityId": 2060, "instanceId": 405, "name": "防御招式",
        "objectName": "StoneTablet-DefensiveMove(Clone)", "rotation": 2,
    }
    result = translate_snapshot(value)
    assert result["tablets"][0]["typeId"] == "tablet-defender"
    assert result["unmapped"] == []


@pytest.mark.parametrize(("entity_id", "name", "expected"), [
    (2006, "到来", "tablet-advent"),
    (2038, "三头", "tablet-triceps"),
])
def test_translate_tablets_from_game_names_or_stable_entity_ids(entity_id, name, expected):
    value = snapshot()
    value["tablets"][0] = {
        "entityId": entity_id, "instanceId": 406, "name": name,
        "rotation": 0,
    }
    result = translate_snapshot(value)
    assert result["tablets"][0]["typeId"] == expected
    assert result["unmapped"] == []


def test_translate_honor_tablet_from_current_game_name():
    value = snapshot()
    value["tablets"][0] = {
        "entityId": 2057, "instanceId": 408, "name": "荣誉", "rotation": 0,
    }
    result = translate_snapshot(value)
    assert result["tablets"][0]["typeId"] == "tablet-honor"
    assert result["unmapped"] == []


def test_translate_dedication_tablet_from_current_game_name():
    value = snapshot()
    value["tablets"][0] = {
        "entityId": 2056, "instanceId": 410, "name": "献礼", "rotation": 0,
    }
    result = translate_snapshot(value)
    assert result["tablets"][0]["typeId"] == "tablet-dedication"
    assert result["unmapped"] == []


def test_translate_last_stand_tablet_from_current_game_name():
    value = snapshot()
    value["tablets"][0] = {
        "entityId": 999998, "instanceId": 411,
        "name": "破釜沉舟", "rotation": 0,
    }
    result = translate_snapshot(value)
    assert result["tablets"][0]["typeId"] == "tablet-last_stand"
    assert result["unmapped"] == []


def test_translate_curse_tablet_from_current_game_entity_id():
    value = snapshot()
    value["tablets"][0] = {
        "entityId": 12000, "instanceId": 409, "name": "诅咒", "rotation": 0,
    }
    result = translate_snapshot(value)
    assert result["tablets"][0]["typeId"] == "tablet-curse"
    assert result["unmapped"] == []


def test_translate_tablet_does_not_infer_type_from_internal_object_name():
    value = snapshot()
    value["tablets"][0] = {
        "entityId": 999999, "instanceId": 407, "name": "missing",
        "objectName": "StoneTablet-Visit(Clone)", "rotation": 0,
    }
    result = translate_snapshot(value)
    assert result["tablets"] == []
    assert result["unmapped"] == [{
        "kind": "tablet", "entityId": 999999, "name": "missing",
    }]


def test_translate_custom_tablet_uses_compiled_game_rules():
    value = snapshot()
    value["version"] = 2
    value["tablets"][0] = {
        "entityId": -1,
        "instanceId": 505,
        "name": "……",
        "isCustom": True,
        "rotatable": False,
        "queryRotations": ["RIGHT 2\nLEFT X\nUP MUL/2"],
        "conditionRotations": ["DOWN CHARM"],
        "customCandidates": [[
            0, 0, [[1, "2"], [2, "X"], [3, "IGNORECRITERIA"], [4, "MUL/2"]],
            [[5, "CHARM"]],
        ]],
    }
    value["cellCount"] = 6
    result = translate_snapshot(value)
    custom = result["customTabletTypes"][0]
    assert custom["name"] == "……"
    assert custom["candidates"] == [[
        0, 0, [[1, 2]], [3], [2], [[4, 2]], [[5, "CHARM"]],
    ]]
    assert result["tablets"][0]["typeId"] == custom["id"]
    assert result["unmapped"] == []


@pytest.mark.parametrize("mutator", [
    lambda value: value.update(version=3),
    lambda value: value.update(ready=False, error="not ready"),
    lambda value: value.update(width=5),
    lambda value: value.update(cellCount=0),
])
def test_translate_snapshot_rejects_invalid_bridge_data(mutator):
    value = snapshot()
    mutator(value)
    with pytest.raises(GameBridgeError):
        translate_snapshot(value)


def test_translate_snapshot_accepts_bridge_protocol_v2():
    value = snapshot()
    value["version"] = 2
    assert translate_snapshot(value)["grid"] == {"cellCount": 17}


def test_translate_snapshot_preserves_apply_source_identity_and_positions():
    value = snapshot()
    value["version"] = 2
    value["inventoryFingerprint"] = "f" * 64
    value["tablets"][0].update(x=2, y=1)
    result = translate_snapshot(value)
    assert result["source"] == {
        "assemblySha256": "abc",
        "capturedAt": "2026-08-07T00:00:00Z",
        "fingerprint": "f" * 64,
        "cellCount": 17,
        "complete": True,
        "items": [
            {
                "solverInstanceId": "game-a-101", "instanceId": 101,
                "kind": "artifact", "entityId": 2, "cell": 1, "rotation": -1,
            },
            {
                "solverInstanceId": "game-t-202", "instanceId": 202,
                "kind": "tablet", "entityId": 999, "cell": 8, "rotation": 1,
            },
        ],
    }


def test_prepare_apply_command_maps_solver_ids_to_exact_game_instances():
    value = snapshot()
    value["version"] = 2
    value["inventoryFingerprint"] = "f" * 64
    value["tablets"][0].update(x=2, y=1)
    source = translate_snapshot(value)["source"]
    command = prepare_apply_command(source, {"placements": [
        {"kind": "artifact", "instanceId": "game-a-101", "cell": 8},
        {"kind": "tablet", "instanceId": "game-t-202", "cell": 1, "rotation": 3},
    ]})
    assert command == {
        "version": 1, "operation": "apply",
        "assemblySha256": "abc", "cellCount": 17,
        "placements": [
            {"instanceId": 101, "kind": "artifact", "cell": 8, "rotation": -1},
            {"instanceId": 202, "kind": "tablet", "cell": 1, "rotation": 3},
        ],
    }


def test_prepare_apply_command_rejects_incomplete_plan():
    source = {
        "fingerprint": "f" * 64, "assemblySha256": "abc", "cellCount": 2,
        "items": [
            {"solverInstanceId": "a", "instanceId": 1, "kind": "artifact", "cell": 0},
            {"solverInstanceId": "t", "instanceId": 2, "kind": "tablet", "cell": 1},
        ],
    }
    with pytest.raises(GameApplyError, match="全部"):
        prepare_apply_command(source, {
            "placements": [{"kind": "artifact", "instanceId": "a", "cell": 0}],
        })


def test_apply_game_arrangement_returns_verified_plugin_response(monkeypatch):
    response = {"ok": True, "code": "APPLIED", "moves": 2, "rotations": 1}
    monkeypatch.setattr("app.game_bridge.os.name", "nt")
    monkeypatch.setattr(
        "app.game_bridge._exchange_pipe_windows",
        lambda *_: json.dumps(response).encode("utf-8"),
    )
    assert apply_game_arrangement({"version": 1}) == response


def test_apply_game_arrangement_surfaces_plugin_error_code(monkeypatch):
    monkeypatch.setattr("app.game_bridge.os.name", "nt")
    monkeypatch.setattr(
        "app.game_bridge._exchange_pipe_windows",
        lambda *_: json.dumps({
            "ok": False, "code": "HOST_REQUIRED", "message": "host required",
        }).encode("utf-8"),
    )
    with pytest.raises(GameApplyError) as raised:
        apply_game_arrangement({"version": 1})
    assert raised.value.code == "HOST_REQUIRED"


def test_read_game_inventory_retries_one_invalid_startup_snapshot(monkeypatch):
    responses = iter([b"", json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")])
    monkeypatch.setattr("app.game_bridge.os.name", "nt")
    monkeypatch.setattr("app.game_bridge._read_pipe_windows", lambda *_: next(responses))
    result = read_game_inventory()
    assert result["grid"] == {"cellCount": 17}


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipe integration")
def test_reads_complete_snapshot_from_windows_named_pipe():
    payload = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
    pipe_path = rf"\\.\pipe\SephiriaInventoryBridge.test.{uuid.uuid4().hex}"
    ready = threading.Event()

    def serve():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateNamedPipeW
        create.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        )
        create.restype = wintypes.HANDLE
        handle = create(pipe_path, 0x00000002, 0, 1, 65536, 65536, 0, None)
        assert handle != wintypes.HANDLE(-1).value
        ready.set()
        try:
            kernel32.ConnectNamedPipe(handle, None)
            written = wintypes.DWORD()
            assert kernel32.WriteFile(handle, payload, len(payload), ctypes.byref(written), None)
            assert written.value == len(payload)
            kernel32.FlushFileBuffers(handle)
            kernel32.DisconnectNamedPipe(handle)
        finally:
            kernel32.CloseHandle(handle)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    result = read_game_inventory(2000, pipe_path)
    thread.join(timeout=2)
    assert result["grid"] == {"cellCount": 17}
    assert result["artifacts"][0]["typeId"] == "artifact-eye_crystal_necklace"
    assert result["tablets"][0]["typeId"] == "tablet-chivalry"
