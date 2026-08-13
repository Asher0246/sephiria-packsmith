from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .models import RequestError, TabletType
from .solver import Candidate, _rotation_invariant, build_candidates


CUSTOM_ID = re.compile(r"^custom-tablet-[a-f0-9]{16,64}$")
CONDITION_KINDS = {"ITEM", "CHARM", "PLACED"}


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RequestError(f"{name} 必须是 {low} 到 {high} 之间的整数")
    return value


def _text(value: Any, name: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{name} 必须是字符串")
    value = value.strip()
    if (not value and not allow_empty) or len(value) > maximum:
        raise RequestError(f"{name} 长度无效")
    return value


def _rotations(raw: Any, name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) not in (1, 4):
        raise RequestError(f"{name} 必须包含 1 或 4 个旋转规则")
    return tuple(_text(value, f"{name}[]", 20_000, True).replace("\r\n", "\n") for value in raw)


def _decode_candidate(raw: Any, index: int, cell_count: int) -> tuple:
    if not isinstance(raw, list) or not 4 <= len(raw) <= 7:
        raise RequestError(f"customTabletTypes.candidates[{index}] 格式无效")
    cell = _integer(raw[0], "candidate.cell", 0, cell_count - 1)
    rotation = _integer(raw[1], "candidate.rotation", 0, 3)

    def pairs(source: Any, label: str, value_low: int, value_high: int) -> tuple[tuple[int, int], ...]:
        if not isinstance(source, list):
            raise RequestError(f"{label} 必须是数组")
        result = []
        for pair in source:
            if not isinstance(pair, list) or len(pair) != 2:
                raise RequestError(f"{label} 条目格式无效")
            target = _integer(pair[0], f"{label}.cell", 0, cell_count - 1)
            value = _integer(pair[1], f"{label}.value", value_low, value_high)
            result.append((target, value))
        return tuple(result)

    def cells(source: Any, label: str) -> tuple[int, ...]:
        if not isinstance(source, list):
            raise RequestError(f"{label} 必须是数组")
        return tuple(_integer(value, f"{label}.cell", 0, cell_count - 1) for value in source)

    effects = pairs(raw[2], "candidate.effects", -99, 99)
    unlocks = cells(raw[3], "candidate.unlocks")
    disables = cells(raw[4] if len(raw) > 4 else [], "candidate.disables")
    multipliers = pairs(raw[5] if len(raw) > 5 else [], "candidate.multipliers", 1, 20)
    raw_conditions = raw[6] if len(raw) > 6 else []
    if not isinstance(raw_conditions, list):
        raise RequestError("candidate.conditions 必须是数组")
    conditions = []
    for pair in raw_conditions:
        if not isinstance(pair, list) or len(pair) != 2:
            raise RequestError("candidate.conditions 条目格式无效")
        target = _integer(pair[0], "candidate.conditions.cell", 0, cell_count - 1)
        kind = _text(pair[1], "candidate.conditions.kind", 12)
        if kind not in CONDITION_KINDS:
            raise RequestError(f"未知石板限定条件: {kind}")
        conditions.append((target, kind))
    return cell, rotation, effects, unlocks, disables, multipliers, tuple(conditions)


def parse_custom_tablet_types(payload: Any) -> dict[str, TabletType]:
    if not isinstance(payload, dict):
        return {}
    raw_types = payload.get("customTabletTypes", [])
    if not isinstance(raw_types, list) or len(raw_types) > 60:
        raise RequestError("customTabletTypes 必须是最多包含 60 项的数组")
    result: dict[str, TabletType] = {}
    for type_index, raw in enumerate(raw_types):
        if not isinstance(raw, dict):
            raise RequestError(f"customTabletTypes[{type_index}] 必须是对象")
        type_id = _text(raw.get("id"), f"customTabletTypes[{type_index}].id", 80)
        if not CUSTOM_ID.fullmatch(type_id) or type_id in result:
            raise RequestError(f"自定义石板 ID 无效或重复: {type_id}")
        name = _text(raw.get("name"), f"customTabletTypes[{type_index}].name", 40)
        rotatable = raw.get("rotatable")
        if not isinstance(rotatable, bool):
            raise RequestError("customTabletTypes.rotatable 必须是布尔值")
        cell_count = _integer(raw.get("cellCount"), "customTabletTypes.cellCount", 1, 60)
        raw_candidates = raw.get("candidates")
        if not isinstance(raw_candidates, list) or not 1 <= len(raw_candidates) <= 240:
            raise RequestError("customTabletTypes.candidates 数量无效")
        candidates = tuple(
            _decode_candidate(candidate, index, cell_count)
            for index, candidate in enumerate(raw_candidates)
        )
        rows = (cell_count + 5) // 6
        result[type_id] = TabletType(
            id=type_id, name=name, tier="custom", rotatable=rotatable,
            constraint=None, image=None, candidates={f"{rows}x6": candidates},
            custom=True, cell_count=cell_count,
            query_rotations=_rotations(raw.get("queryRotations"), "queryRotations"),
            condition_rotations=_rotations(raw.get("conditionRotations"), "conditionRotations"),
        )
    return result


def _candidate_raw(candidate: Candidate) -> list:
    return [
        candidate.cell,
        candidate.rotation,
        [[cell, value] for cell, value in sorted(candidate.effects.items())],
        sorted(candidate.unlocks),
        sorted(candidate.disables),
        [[cell, value] for cell, value in sorted(candidate.multipliers.items())],
        [[cell, kind] for cell, kind in candidate.conditions],
    ]


def custom_tablet_public(tablet: TabletType) -> dict:
    if not tablet.custom or tablet.cell_count is None or tablet.candidates is None:
        raise ValueError("Expected a custom tablet type")
    key = f"{(tablet.cell_count + 5) // 6}x6"
    return {
        "id": tablet.id,
        "name": tablet.name,
        "tier": "custom",
        "rotatable": tablet.rotatable,
        "constraint": None,
        "image": None,
        "custom": True,
        "cellCount": tablet.cell_count,
        "queryRotations": list(tablet.query_rotations),
        "conditionRotations": list(tablet.condition_rotations),
        "candidates": [_candidate_raw(candidate) for candidate in tablet.candidates[key]],
    }


def _runtime_rotations(source: dict, key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    runtime = source.get("runtimeRule")
    if isinstance(runtime, dict) and key in runtime:
        return _rotations(runtime.get(key), f"source.{key}")
    return fallback


def _rotation_value(values: tuple[str, ...], rotation: int) -> str:
    if not values:
        return ""
    return values[rotation % len(values)]


def compose_custom_tablet(payload: Any, base_types: dict[str, TabletType]) -> dict:
    if not isinstance(payload, dict):
        raise RequestError("请求体必须是对象")
    cell_count = _integer(payload.get("cellCount"), "cellCount", 1, 60)
    rows = (cell_count + 5) // 6
    custom_types = parse_custom_tablet_types(payload)
    all_types = {**base_types, **custom_types}
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) != 2:
        raise RequestError("合成必须选择两块石板")

    resolved = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise RequestError(f"sources[{index}] 必须是对象")
        type_id = _text(source.get("typeId"), f"sources[{index}].typeId", 80)
        tablet = all_types.get(type_id)
        if tablet is None:
            raise RequestError(f"未知石板: {type_id}")
        if tablet.custom and tablet.cell_count != cell_count:
            raise RequestError(f"自定义石板 {tablet.name} 仅适用于原背包格数")
        rotation = _integer(source.get("rotation", 0), f"sources[{index}].rotation", 0, 3)
        if rotation and not tablet.rotatable:
            raise RequestError(f"石板 {tablet.name} 不可旋转")
        possible = build_candidates(tablet, rows, 6, cell_count)
        by_position = {(candidate.cell, candidate.rotation): candidate for candidate in possible}
        if tablet.rotatable and _rotation_invariant(tablet):
            by_position = {
                (candidate.cell, rotation): candidate
                for candidate in possible
                for rotation in range(4)
            }
        query_rotations = _runtime_rotations(source, "queryRotations", tablet.query_rotations)
        condition_rotations = _runtime_rotations(
            source, "conditionRotations", tablet.condition_rotations,
        )
        resolved.append((tablet, rotation, by_position, query_rotations, condition_rotations))

    first, second = resolved
    base_conditions = [
        _rotation_value(first[4], first[1]),
        _rotation_value(second[4], second[1]),
    ]
    if all(base_conditions) and base_conditions[0] != base_conditions[1]:
        raise RequestError("两块石板旋转后的限定条件不同，游戏中无法合成")

    result_rotatable = first[0].rotatable and second[0].rotatable
    rotation_count = 4 if result_rotatable else 1
    combined: list[Candidate] = []
    query_rotations = []
    condition_rotations = []
    for final_rotation in range(rotation_count):
        queries = [
            _rotation_value(first[3], first[1] + final_rotation),
            _rotation_value(second[3], second[1] + final_rotation),
        ]
        query_rotations.append("\n".join(value for value in queries if value))
        conditions = [
            _rotation_value(first[4], first[1] + final_rotation),
            _rotation_value(second[4], second[1] + final_rotation),
        ]
        nonempty_conditions = [value for value in conditions if value]
        if len(set(nonempty_conditions)) > 1:
            raise RequestError("两块石板在旋转后产生了不同的限定条件")
        condition_rotations.append(nonempty_conditions[0] if nonempty_conditions else "")

        for cell in range(cell_count):
            source_candidates = []
            for tablet, source_rotation, by_position, _, _ in resolved:
                effective_rotation = (source_rotation + final_rotation) % 4 if tablet.rotatable else 0
                candidate = by_position.get((cell, effective_rotation))
                if candidate is None:
                    raise RequestError(f"石板 {tablet.name} 缺少合成候选")
                source_candidates.append(candidate)
            a, b = source_candidates
            effects = dict(a.effects)
            for target, value in b.effects.items():
                effects[target] = effects.get(target, 0) + value
                if effects[target] == 0:
                    del effects[target]
            multipliers = dict(a.multipliers)
            for target, value in b.multipliers.items():
                multipliers[target] = multipliers.get(target, 0) + value
            if conditions[0] and conditions[1]:
                candidate_conditions = a.conditions
            elif conditions[0]:
                candidate_conditions = a.conditions
            elif conditions[1]:
                candidate_conditions = b.conditions
            else:
                candidate_conditions = ()
            combined.append(Candidate(
                cell, final_rotation, effects, a.unlocks | b.unlocks,
                a.disables | b.disables, multipliers, candidate_conditions,
            ))

    name = _text(payload.get("name") or f"{first[0].name} + {second[0].name}", "name", 40)
    signature = json.dumps(
        [result_rotatable, query_rotations, condition_rotations,
         [_candidate_raw(candidate) for candidate in combined]],
        ensure_ascii=False, separators=(",", ":"),
    )
    type_id = "custom-tablet-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
    tablet = TabletType(
        type_id, name, "custom", result_rotatable, None, None,
        candidates={f"{rows}x6": tuple(combined)}, custom=True, cell_count=cell_count,
        query_rotations=tuple(query_rotations), condition_rotations=tuple(condition_rotations),
    )
    return custom_tablet_public(tablet)
