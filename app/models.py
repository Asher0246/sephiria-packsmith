from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class RequestError(ValueError):
    pass


SPECIAL_CONDITION_BY_ID = {
    "artifact-crystal_of_harmony": "nearby_levels",
    "artifact-multi_use_belt": "top_row_artifacts",
    "artifact-giant_telescope": "nearby_planets",
    "artifact-white_paper": "matching_side_categories",
    "artifact-unalloyed_gold_needle": "target_above",
}


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(f"{name} 必须是整数")
    if not low <= value <= high:
        raise RequestError(f"{name} 必须在 {low} 到 {high} 之间")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise RequestError(f"{name} 必须是布尔值")
    return value


@dataclass(frozen=True)
class ArtifactType:
    id: str
    name: str
    cap: int
    rarity: int
    categories: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    allow_negative: bool = True
    base_level: int = 0
    image: str | None = None
    special_condition: str | None = None


@dataclass(frozen=True)
class TabletType:
    id: str
    name: str
    tier: str
    rotatable: bool
    constraint: str | None
    image: str | None
    directions: tuple[tuple[int | str, int | str], ...] = ()
    candidates: dict[str, tuple[tuple, ...]] | None = None
    custom: bool = False
    cell_count: int | None = None
    query_rotations: tuple[str, ...] = ()
    condition_rotations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactInstance:
    instance_id: str
    type_id: str
    weight: int = 5
    min_level: int | None = None
    exact_level: int | None = None
    fixed_cell: int | None = None
    base_level: int = 0
    special_priority: bool = False
    special_target_instance_id: str | None = None


@dataclass(frozen=True)
class TabletInstance:
    instance_id: str
    type_id: str
    fixed_cell: int | None = None
    fixed_rotation: int | None = None


@dataclass(frozen=True)
class SolveRequest:
    rows: int
    cols: int
    artifacts: tuple[ArtifactInstance, ...]
    tablets: tuple[TabletInstance, ...]
    time_limit_ms: int = 10_000
    actual_cell_count: int | None = None
    worker_count: int = 0

    @property
    def cell_count(self) -> int:
        return self.actual_cell_count if self.actual_cell_count is not None else self.rows * self.cols


def _instance_id(raw: Any, field_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 80:
        raise RequestError(f"{field_name} 必须是非空字符串")
    return raw.strip()


def _optional_cell(raw: Any, name: str, cell_count: int) -> int | None:
    if raw is None:
        return None
    return _integer(raw, name, 0, cell_count - 1)


def parse_request(payload: Any, artifact_ids: set[str], tablet_ids: set[str]) -> SolveRequest:
    if not isinstance(payload, dict):
        raise RequestError("请求体必须是 JSON 对象")
    grid = payload.get("grid")
    if not isinstance(grid, dict):
        raise RequestError("缺少 grid")
    if "cellCount" in grid:
        cell_count = _integer(grid.get("cellCount"), "grid.cellCount", 1, 60)
        cols = 6
        rows = (cell_count + cols - 1) // cols
    else:
        rows = _integer(grid.get("rows"), "grid.rows", 1, 10)
        cols = _integer(grid.get("cols"), "grid.cols", 1, 6)
        cell_count = rows * cols
    raw_artifacts = payload.get("artifacts", [])
    raw_tablets = payload.get("tablets", [])
    if not isinstance(raw_artifacts, list) or not isinstance(raw_tablets, list):
        raise RequestError("artifacts 和 tablets 必须是数组")
    if not raw_artifacts and not raw_tablets:
        raise RequestError("请至少添加一件物品")
    if len(raw_artifacts) + len(raw_tablets) > cell_count:
        raise RequestError("物品数量超过背包格数")

    seen: set[str] = set()
    artifacts: list[ArtifactInstance] = []
    for index, raw in enumerate(raw_artifacts):
        if not isinstance(raw, dict):
            raise RequestError(f"artifacts[{index}] 必须是对象")
        iid = _instance_id(raw.get("instanceId"), f"artifacts[{index}].instanceId")
        type_id = _instance_id(raw.get("typeId"), f"artifacts[{index}].typeId")
        if iid in seen:
            raise RequestError(f"实例 ID 重复: {iid}")
        if type_id not in artifact_ids:
            raise RequestError(f"未知神器: {type_id}")
        seen.add(iid)
        min_level = raw.get("minLevel")
        if min_level is not None:
            min_level = _integer(min_level, f"artifacts[{index}].minLevel", -99, 99)
        exact_level = raw.get("exactLevel")
        if exact_level is not None:
            exact_level = _integer(exact_level, f"artifacts[{index}].exactLevel", -99, 99)
        special_priority = _boolean(
            raw.get("specialPriority", False), f"artifacts[{index}].specialPriority",
        )
        special_target = raw.get("specialTargetInstanceId")
        if special_target is not None:
            special_target = _instance_id(
                special_target, f"artifacts[{index}].specialTargetInstanceId",
            )
        if special_priority and type_id not in SPECIAL_CONDITION_BY_ID:
            raise RequestError(f"神器 {type_id} 不支持特殊效果优先")
        artifacts.append(ArtifactInstance(
            instance_id=iid,
            type_id=type_id,
            weight=_integer(raw.get("weight", 5), f"artifacts[{index}].weight", 1, 10),
            min_level=min_level,
            exact_level=exact_level,
            fixed_cell=_optional_cell(raw.get("fixedCell"), f"artifacts[{index}].fixedCell", cell_count),
            base_level=_integer(raw.get("baseLevel", 0), f"artifacts[{index}].baseLevel", 0, 99),
            special_priority=special_priority,
            special_target_instance_id=special_target,
        ))

    artifact_instance_ids = {item.instance_id for item in artifacts}
    for item in artifacts:
        if not item.special_priority or SPECIAL_CONDITION_BY_ID.get(item.type_id) != "target_above":
            continue
        if item.special_target_instance_id is None:
            raise RequestError("北向的金色针开启特殊效果优先时必须指定上方目标神器")
        if item.special_target_instance_id == item.instance_id:
            raise RequestError("北向的金色针不能将自身设为上方目标")
        if item.special_target_instance_id not in artifact_instance_ids:
            raise RequestError("北向的金色针指定的上方目标必须是当前构筑中的神器")

    tablets: list[TabletInstance] = []
    for index, raw in enumerate(raw_tablets):
        if not isinstance(raw, dict):
            raise RequestError(f"tablets[{index}] 必须是对象")
        iid = _instance_id(raw.get("instanceId"), f"tablets[{index}].instanceId")
        type_id = _instance_id(raw.get("typeId"), f"tablets[{index}].typeId")
        if iid in seen:
            raise RequestError(f"实例 ID 重复: {iid}")
        if type_id not in tablet_ids:
            raise RequestError(f"未知石板: {type_id}")
        seen.add(iid)
        rotation = raw.get("fixedRotation")
        if rotation is not None:
            rotation = _integer(rotation, f"tablets[{index}].fixedRotation", 0, 3)
        tablets.append(TabletInstance(
            instance_id=iid,
            type_id=type_id,
            fixed_cell=_optional_cell(raw.get("fixedCell"), f"tablets[{index}].fixedCell", cell_count),
            fixed_rotation=rotation,
        ))

    options = payload.get("options", {})
    if not isinstance(options, dict):
        raise RequestError("options 必须是对象")
    return SolveRequest(
        rows, cols, tuple(artifacts), tuple(tablets),
        _integer(options.get("timeLimitMs", 10_000), "options.timeLimitMs", 500, 120_000),
        cell_count,
        _integer(options.get("workerCount", 0), "options.workerCount", 0, 64),
    )
