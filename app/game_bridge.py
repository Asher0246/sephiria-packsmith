from __future__ import annotations

import ctypes
import gzip
import hashlib
import json
import os
import struct
import unicodedata
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
from typing import Any


PIPE_PATH = r"\\.\pipe\SephiriaInventoryBridge.v1"
APPLY_PIPE_PATH = r"\\.\pipe\SephiriaInventoryBridge.apply.v1"
MAX_SNAPSHOT_BYTES = 8_000_000
MAX_COMMAND_BYTES = 1_000_000
ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"


class GameBridgeError(RuntimeError):
    pass


class GameApplyError(GameBridgeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


@lru_cache(maxsize=1)
def _catalog_mappings() -> tuple[
    dict[int, str], dict[str, str], dict[int, str], dict[str, str],
]:
    wiki = json.loads((ASSETS / "wiki_artifacts.json").read_text(encoding="utf-8"))
    localized = json.loads((ASSETS / "wiki_zh_cn.json").read_text(encoding="utf-8"))
    with gzip.open(ASSETS / "wiki_tablets.json.gz", "rt", encoding="utf-8") as handle:
        tablets = json.load(handle)["tablets"]

    artifact_by_entity: dict[int, str] = {}
    artifact_by_name: dict[str, str] = {}
    for row in wiki["artifacts"]:
        type_id = f"artifact-{row['value']}"
        artifact_by_entity[int(row["id"])] = type_id
        names = (row.get("value"), row.get("label_kor"), row.get("label_eng"),
                 localized["artifacts"][str(row["value"])]["name"])
        for name in names:
            if _normalized(name):
                artifact_by_name[_normalized(name)] = type_id

    artifact_by_entity[1304] = "artifact-heart_burden"
    artifact_by_entity[1033] = "artifact-sword_earring"
    artifact_by_name[_normalized("心之重担")] = "artifact-heart_burden"

    tablet_by_name: dict[str, str] = {}
    for row in tablets:
        value = str(row["value"])
        type_id = f"tablet-{value}"
        tablet_by_name[_normalized(localized["tablets"][value])] = type_id
    tablet_by_name[_normalized(localized["tablets"]["curse"])] = "tablet-curse"
    tablet_by_entity = {
        2038: "tablet-triceps",
        2053: "tablet-development",
        2060: "tablet-defender",
        12000: "tablet-curse",
    }
    return artifact_by_entity, artifact_by_name, tablet_by_entity, tablet_by_name


def _resolve_type(kind: str, item: dict) -> str | None:
    artifact_by_entity, artifact_by_name, tablet_by_entity, tablet_by_name = _catalog_mappings()
    if kind == "artifact":
        try:
            type_id = artifact_by_entity.get(int(item.get("entityId")))
        except (TypeError, ValueError):
            type_id = None
        if type_id:
            return type_id
        return artifact_by_name.get(_normalized(item.get("name")))

    type_id = tablet_by_entity.get(_integer(item.get("entityId"), -1))
    if type_id:
        return type_id
    return tablet_by_name.get(_normalized(item.get("name")))


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _metadata_pairs(raw: Any, cell_count: int, label: str) -> list[tuple[int, str]]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} 不是数组")
    result = []
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"{label} 条目格式无效")
        cell = _integer(pair[0], -1)
        if not 0 <= cell < cell_count:
            raise ValueError(f"{label} 包含越界格子")
        result.append((cell, str(pair[1] or "")))
    return result


def _translate_custom_tablet(
    raw: dict, cell_count: int, base_type_id: str | None = None,
) -> dict:
    query_rotations = raw.get("queryRotations")
    condition_rotations = raw.get("conditionRotations")
    if not isinstance(query_rotations, list) or len(query_rotations) not in (1, 4):
        raise ValueError("缺少效果旋转规则")
    if not isinstance(condition_rotations, list) or len(condition_rotations) not in (1, 4):
        raise ValueError("缺少条件旋转规则")
    rotatable = bool(raw.get("rotatable"))
    raw_candidates = raw.get("customCandidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("缺少自定义石板候选")
    candidates = []
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, list) or len(raw_candidate) != 4:
            raise ValueError("自定义石板候选格式无效")
        cell = _integer(raw_candidate[0], -1)
        rotation = _integer(raw_candidate[1], -1)
        if not 0 <= cell < cell_count or not 0 <= rotation <= 3:
            raise ValueError("自定义石板候选位置或旋转无效")
        effects: dict[int, int] = {}
        unlocks: set[int] = set()
        disables: set[int] = set()
        multipliers: dict[int, int] = {}
        for target, value in _metadata_pairs(raw_candidate[2], cell_count, "效果范围"):
            try:
                numeric = int(value)
            except ValueError:
                numeric = None
            if numeric is not None:
                effects[target] = effects.get(target, 0) + numeric
            elif value == "X":
                disables.add(target)
            elif value == "IGNORECRITERIA":
                unlocks.add(target)
            elif value.startswith("MUL/"):
                multiplier = int(value.split("/", 1)[1])
                if not 1 <= multiplier <= 20:
                    raise ValueError("石板倍率超出支持范围")
                multipliers[target] = multipliers.get(target, 0) + multiplier
            else:
                raise ValueError(f"未知石板效果: {value}")
        effects = {target: value for target, value in effects.items() if value}
        conditions = []
        for target, value in _metadata_pairs(raw_candidate[3], cell_count, "限定范围"):
            if value not in {"ITEM", "CHARM", "PLACED"}:
                raise ValueError(f"未知石板限定条件: {value}")
            conditions.append([target, value])
        candidates.append([
            cell, rotation, sorted([target, value] for target, value in effects.items()),
            sorted(unlocks), sorted(disables),
            sorted([target, value] for target, value in multipliers.items()), conditions,
        ])
    signature = json.dumps(
        [base_type_id, rotatable, query_rotations, condition_rotations, candidates],
        ensure_ascii=False, separators=(",", ":"),
    )
    type_id = "custom-tablet-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
    raw_name = str(raw.get("name") or "").strip()
    name = raw_name or "自定义石板"
    result = {
        "id": type_id,
        "name": name[:40],
        "tier": "custom",
        "rotatable": rotatable,
        "constraint": None,
        "image": None,
        "custom": True,
        "cellCount": cell_count,
        "queryRotations": [str(value) for value in query_rotations],
        "conditionRotations": [str(value) for value in condition_rotations],
        "candidates": candidates,
    }
    if base_type_id:
        from .catalog import tablet_types
        base = next((item for item in tablet_types() if item.id == base_type_id), None)
        if base is not None:
            result.update({
                "name": base.name, "tier": base.tier, "image": base.image,
                "baseTypeId": base.id,
            })
    return result
def translate_snapshot(snapshot: Any) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("version") not in (1, 2):
        raise GameBridgeError("游戏背包桥接数据版本不受支持")
    if not snapshot.get("ready"):
        raise GameBridgeError(str(snapshot.get("error") or "尚未找到本地玩家背包"))
    width = _integer(snapshot.get("width"))
    cell_count = _integer(snapshot.get("cellCount"))
    if width != 6 or not 1 <= cell_count <= 60:
        raise GameBridgeError("游戏返回的背包尺寸无效")

    result = {
        "grid": {"cellCount": cell_count},
        "artifacts": [],
        "tablets": [],
        "customTabletTypes": [],
        "unmapped": [],
        "source": {
            "assemblySha256": str(snapshot.get("assemblySha256") or ""),
            "capturedAt": str(snapshot.get("capturedAt") or ""),
            "fingerprint": str(snapshot.get("inventoryFingerprint") or ""),
            "cellCount": cell_count,
            "items": [],
        },
    }
    seen_instances: set[str] = set()
    for kind, source_key, target_key, prefix in (
        ("artifact", "artifacts", "artifacts", "game-a"),
        ("tablet", "tablets", "tablets", "game-t"),
    ):
        raw_items = snapshot.get(source_key, [])
        if not isinstance(raw_items, list):
            raise GameBridgeError("游戏背包桥接数据格式无效")
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            custom_type = None
            resolved_type_id = _resolve_type(kind, raw)
            if kind == "tablet" and raw.get("customCandidates") is not None:
                try:
                    custom_type = _translate_custom_tablet(
                        raw, cell_count,
                        None if raw.get("isCustom") else resolved_type_id,
                    )
                except (TypeError, ValueError) as exc:
                    if raw.get("isCustom") or not resolved_type_id:
                        result["unmapped"].append({
                            "kind": kind, "entityId": raw.get("entityId"),
                            "name": raw.get("name") or "", "reason": str(exc),
                        })
                        continue
                if custom_type is not None:
                    type_id = custom_type["id"]
                    if all(item["id"] != type_id for item in result["customTabletTypes"]):
                        result["customTabletTypes"].append(custom_type)
                else:
                    type_id = resolved_type_id
            else:
                type_id = resolved_type_id
                if not type_id and kind == "tablet":
                    try:
                        custom_type = _translate_custom_tablet(raw, cell_count)
                    except (TypeError, ValueError):
                        custom_type = None
                    if custom_type is not None:
                        type_id = custom_type["id"]
                        if all(item["id"] != type_id for item in result["customTabletTypes"]):
                            result["customTabletTypes"].append(custom_type)
            if not type_id:
                result["unmapped"].append({
                    "kind": kind, "entityId": raw.get("entityId"), "name": raw.get("name") or "",
                })
                continue
            raw_instance = _integer(raw.get("instanceId"), index + 1)
            instance_id = f"{prefix}-{raw_instance}"
            suffix = 2
            while instance_id in seen_instances:
                instance_id = f"{prefix}-{raw_instance}-{suffix}"
                suffix += 1
            seen_instances.add(instance_id)
            source_item = {
                "solverInstanceId": instance_id,
                "instanceId": raw_instance,
                "kind": kind,
                "entityId": _integer(raw.get("entityId")),
                "cell": _integer(raw.get("y"), -1) * width + _integer(raw.get("x"), -1),
                "rotation": _integer(raw.get("rotation"), 0) if kind == "tablet" else -1,
            }
            result["source"]["items"].append(source_item)
            if kind == "artifact":
                base_level = max(
                    0, _integer(raw.get("enchantLevel"), _integer(raw.get("temporaryLevel"))),
                )
                result[target_key].append({
                    "instanceId": instance_id, "typeId": type_id, "weight": 5,
                    "baseLevel": base_level, "minLevel": None, "exactLevel": None,
                    "fixedCell": None, "specialPriority": False, "specialTargetInstanceId": None,
                })
            else:
                translated = {
                    "instanceId": instance_id, "typeId": type_id,
                    "fixedCell": None, "fixedRotation": None,
                    "preferredRotation": source_item["rotation"],
                }
                query_rotations = raw.get("queryRotations")
                condition_rotations = raw.get("conditionRotations")
                if (isinstance(query_rotations, list) and isinstance(condition_rotations, list)
                        and len(query_rotations) in (1, 4) and len(condition_rotations) in (1, 4)):
                    translated["runtimeRule"] = {
                        "queryRotations": [str(value) for value in query_rotations],
                        "conditionRotations": [str(value) for value in condition_rotations],
                    }
                result[target_key].append(translated)
    result["source"]["complete"] = (
        bool(result["source"]["fingerprint"])
        and not result["unmapped"]
        and all(0 <= item["cell"] < cell_count for item in result["source"]["items"])
    )
    return result


def prepare_apply_command(source: Any, result: Any) -> dict:
    if not isinstance(source, dict) or not isinstance(result, dict):
        raise GameApplyError("NO_GAME_SNAPSHOT", "该求解结果不是从游戏背包读取的数据，无法自动应用")
    fingerprint = str(source.get("fingerprint") or "")
    assembly_hash = str(source.get("assemblySha256") or "")
    cell_count = _integer(source.get("cellCount"), 0)
    source_items = source.get("items")
    placements = result.get("placements")
    if not fingerprint or not assembly_hash or not 1 <= cell_count <= 60:
        raise GameApplyError("PLUGIN_UPDATE_REQUIRED", "游戏插件版本过旧，请更新插件并重新读取背包")
    if not isinstance(source_items, list) or not isinstance(placements, list) or not placements:
        raise GameApplyError("INVALID_APPLY_PLAN", "求解结果缺少可应用的物品位置")

    source_by_solver_id: dict[str, dict] = {}
    game_ids: set[int] = set()
    source_cells: set[int] = set()
    for item in source_items:
        if not isinstance(item, dict):
            raise GameApplyError("INVALID_GAME_SNAPSHOT", "游戏背包快照中的物品格式无效")
        solver_id = str(item.get("solverInstanceId") or "")
        game_id = _integer(item.get("instanceId"), 0)
        kind = item.get("kind")
        cell = _integer(item.get("cell"), -1)
        if (not solver_id or game_id <= 0 or kind not in ("artifact", "tablet")
                or not 0 <= cell < cell_count or solver_id in source_by_solver_id
                or game_id in game_ids or cell in source_cells):
            raise GameApplyError("INVALID_GAME_SNAPSHOT", "游戏背包快照中的实例或位置无效")
        source_by_solver_id[solver_id] = item
        game_ids.add(game_id)
        source_cells.add(cell)

    command_placements = []
    target_cells: set[int] = set()
    placed_ids: set[str] = set()
    for placement in placements:
        if not isinstance(placement, dict):
            raise GameApplyError("INVALID_APPLY_PLAN", "求解结果中的位置格式无效")
        solver_id = str(placement.get("instanceId") or "")
        source_item = source_by_solver_id.get(solver_id)
        cell = _integer(placement.get("cell"), -1)
        if (source_item is None or solver_id in placed_ids or not 0 <= cell < cell_count
                or cell in target_cells or placement.get("kind") != source_item["kind"]):
            raise GameApplyError("INVALID_APPLY_PLAN", "求解结果与读取到的游戏物品不一致")
        rotation = -1
        if source_item["kind"] == "tablet":
            rotation = _integer(placement.get("rotation"), -1)
            if not 0 <= rotation <= 3:
                raise GameApplyError("INVALID_APPLY_PLAN", "求解结果中的石板旋转无效")
        command_placements.append({
            "instanceId": _integer(source_item["instanceId"]),
            "kind": source_item["kind"],
            "cell": cell,
            "rotation": rotation,
        })
        placed_ids.add(solver_id)
        target_cells.add(cell)
    if placed_ids != set(source_by_solver_id):
        raise GameApplyError("INVALID_APPLY_PLAN", "求解结果没有包含读取到的全部游戏物品")
    return {
        "version": 1,
        "operation": "apply",
        "assemblySha256": assembly_hash,
        "cellCount": cell_count,
        "placements": command_placements,
    }


def _read_pipe_windows(timeout_ms: int, pipe_path: str = PIPE_PATH) -> bytes:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wait_named_pipe = kernel32.WaitNamedPipeW
    wait_named_pipe.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    wait_named_pipe.restype = wintypes.BOOL
    if not wait_named_pipe(pipe_path, timeout_ms):
        error = ctypes.get_last_error()
        if error in (2, 121):
            raise GameBridgeError("未连接到游戏插件；请启动游戏并确认背包桥接插件已安装")
        raise GameBridgeError(f"等待游戏背包插件失败（Windows 错误 {error}）")

    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(pipe_path, 0x80000000, 0, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise GameBridgeError(f"连接游戏背包插件失败（Windows 错误 {ctypes.get_last_error()}）")
    try:
        read_file = kernel32.ReadFile
        read_file.argtypes = (
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        read_file.restype = wintypes.BOOL
        chunks: list[bytes] = []
        total = 0
        while True:
            buffer = ctypes.create_string_buffer(64 * 1024)
            read = wintypes.DWORD()
            ok = read_file(handle, buffer, len(buffer), ctypes.byref(read), None)
            if read.value:
                chunks.append(buffer.raw[:read.value])
                total += read.value
                if total > MAX_SNAPSHOT_BYTES:
                    raise GameBridgeError("游戏背包桥接数据过大")
            if not ok:
                error = ctypes.get_last_error()
                if error in (109, 233):  # Writer sent one complete snapshot and closed/disconnected.
                    break
                if error == 234:  # ERROR_MORE_DATA
                    continue
                raise GameBridgeError(f"读取游戏背包插件失败（Windows 错误 {error}）")
            if read.value == 0:
                break
        return b"".join(chunks)
    finally:
        kernel32.CloseHandle(handle)


def read_game_inventory(timeout_ms: int = 1500, pipe_path: str = PIPE_PATH) -> dict:
    if os.name != "nt":
        raise GameBridgeError("游戏背包桥接仅支持 Windows")
    snapshot = None
    decode_error = None
    for _ in range(2):
        raw = _read_pipe_windows(timeout_ms, pipe_path)
        try:
            snapshot = json.loads(raw.decode("utf-8"))
            break
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            decode_error = exc
    if snapshot is None:
        raise GameBridgeError("游戏背包插件返回了无效数据") from decode_error
    return translate_snapshot(snapshot)


def _exchange_pipe_windows(payload: bytes, timeout_ms: int, pipe_path: str = APPLY_PIPE_PATH) -> bytes:
    if len(payload) > MAX_COMMAND_BYTES:
        raise GameApplyError("COMMAND_TOO_LARGE", "应用方案数据过大")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wait_named_pipe = kernel32.WaitNamedPipeW
    wait_named_pipe.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    wait_named_pipe.restype = wintypes.BOOL
    if not wait_named_pipe(pipe_path, timeout_ms):
        error = ctypes.get_last_error()
        if error in (2, 121):
            raise GameApplyError("PLUGIN_UPDATE_REQUIRED", "未连接到支持自动排布的游戏插件，请更新插件并重启游戏")
        raise GameApplyError("PIPE_UNAVAILABLE", f"等待游戏应用插件失败（Windows 错误 {error}）")

    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(pipe_path, 0xC0000000, 0, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise GameApplyError("PIPE_UNAVAILABLE", f"连接游戏应用插件失败（Windows 错误 {ctypes.get_last_error()}）")

    write_file = kernel32.WriteFile
    write_file.argtypes = (
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    )
    write_file.restype = wintypes.BOOL
    read_file = kernel32.ReadFile
    read_file.argtypes = (
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    )
    read_file.restype = wintypes.BOOL

    def write_all(data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = wintypes.DWORD()
            buffer = ctypes.create_string_buffer(data[offset:])
            if not write_file(handle, buffer, len(buffer.raw) - 1, ctypes.byref(written), None):
                raise GameApplyError("PIPE_WRITE_FAILED", f"发送应用方案失败（Windows 错误 {ctypes.get_last_error()}）")
            if written.value == 0:
                raise GameApplyError("PIPE_WRITE_FAILED", "发送应用方案时连接已关闭")
            offset += written.value

    def read_exact(length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            buffer = ctypes.create_string_buffer(min(64 * 1024, remaining))
            read = wintypes.DWORD()
            ok = read_file(handle, buffer, len(buffer), ctypes.byref(read), None)
            if read.value:
                chunks.append(buffer.raw[:read.value])
                remaining -= read.value
            if not ok:
                raise GameApplyError("PIPE_READ_FAILED", f"读取应用结果失败（Windows 错误 {ctypes.get_last_error()}）")
            if read.value == 0:
                raise GameApplyError("PIPE_READ_FAILED", "读取应用结果时连接已关闭")
        return b"".join(chunks)

    try:
        write_all(struct.pack("<I", len(payload)) + payload)
        response_length = struct.unpack("<I", read_exact(4))[0]
        if response_length <= 0 or response_length > MAX_COMMAND_BYTES:
            raise GameApplyError("INVALID_PLUGIN_RESPONSE", "游戏插件返回的数据长度无效")
        return read_exact(response_length)
    finally:
        kernel32.CloseHandle(handle)


def apply_game_arrangement(command: dict, timeout_ms: int = 20_000,
                           pipe_path: str = APPLY_PIPE_PATH) -> dict:
    if os.name != "nt":
        raise GameApplyError("UNSUPPORTED_PLATFORM", "自动应用背包排布仅支持 Windows")
    payload = json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raw = _exchange_pipe_windows(payload, timeout_ms, pipe_path)
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GameApplyError("INVALID_PLUGIN_RESPONSE", "游戏插件返回了无效的应用结果") from exc
    if not isinstance(response, dict):
        raise GameApplyError("INVALID_PLUGIN_RESPONSE", "游戏插件返回的应用结果格式无效")
    if not response.get("ok"):
        raise GameApplyError(
            str(response.get("code") or "APPLY_FAILED"),
            str(response.get("message") or "游戏未能应用背包排布"),
        )
    return response
