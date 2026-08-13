from __future__ import annotations

from .models import ArtifactType, SolveRequest, TabletType
from .solver import _rotation_invariant, build_candidates


def _tablet_applies(candidate, occupied: set[int], artifact_cells: set[int]) -> bool:
    placed_cells = []
    for cell, kind in candidate.conditions:
        if kind == "ITEM" and cell not in occupied:
            return False
        if kind == "CHARM" and cell not in artifact_cells:
            return False
        if kind == "PLACED":
            placed_cells.append(cell)
    return not placed_cells or candidate.cell in placed_cells


def _artifact_criteria_met(
    artifact: ArtifactType,
    cell: int,
    request: SolveRequest,
    occupied: set[int],
    artifact_cells: set[int],
) -> bool:
    y, x = divmod(cell, request.cols)
    for criterion in artifact.criteria:
        if criterion == "edge" and not (x in (0, request.cols - 1) or y in (0, request.rows - 1)):
            return False
        if criterion == "inner" and not (0 < x < request.cols - 1 and 0 < y < request.rows - 1):
            return False
        if criterion == "top" and y != 0:
            return False
        if criterion == "bottom" and y != request.rows - 1:
            return False
        if criterion == "side_end" and not (x in (0, request.cols - 1) or cell == request.cell_count - 1):
            return False
        if criterion == "side_free":
            neighbors = [neighbor for neighbor in (cell - 1, cell + 1)
                         if 0 <= neighbor < request.cell_count
                         and neighbor // request.cols == y]
            if any(neighbor in occupied for neighbor in neighbors):
                return False
        if criterion == "both_side_artifacts":
            if x in (0, request.cols - 1) or cell + 1 >= request.cell_count:
                return False
            if cell - 1 not in artifact_cells or cell + 1 not in artifact_cells:
                return False
    return True


def validate_result(
    request: SolveRequest,
    artifacts_by_id: dict[str, ArtifactType],
    tablets_by_id: dict[str, TabletType],
    result: dict,
) -> list[str]:
    problems: list[str] = []
    if result.get("solutionStatus") not in ("OPTIMAL", "FEASIBLE", "STOPPED"):
        return problems
    placements = result.get("placements", [])
    expected_ids = {item.instance_id for item in (*request.artifacts, *request.tablets)}
    actual_ids = [item.get("instanceId") for item in placements]
    if set(actual_ids) != expected_ids or len(actual_ids) != len(expected_ids):
        problems.append("返回物品实例与请求不一致")
    cells = [item.get("cell") for item in placements]
    if any(not isinstance(cell, int) or not 0 <= cell < request.cell_count for cell in cells):
        problems.append("存在越界位置")
    if len(cells) != len(set(cells)):
        problems.append("存在物品重叠")

    by_instance = {item.get("instanceId"): item for item in placements}
    occupied = {item["cell"] for item in placements if isinstance(item.get("cell"), int)}
    artifact_cells = {
        by_instance[item.instance_id]["cell"] for item in request.artifacts
        if item.instance_id in by_instance and isinstance(by_instance[item.instance_id].get("cell"), int)
    }
    effects = [0] * request.cell_count
    multipliers = [0] * request.cell_count
    tablet_effects = [[] for _ in range(request.cell_count)]
    unlocks: set[int] = set()
    disabled: set[int] = set()
    for instance in request.tablets:
        placed = by_instance.get(instance.instance_id)
        if not placed:
            continue
        tablet = tablets_by_id[instance.type_id]
        matching = [candidate for candidate in build_candidates(
                        tablet, request.rows, request.cols, request.cell_count)
                    if candidate.cell == placed.get("cell")
                    and (candidate.rotation == placed.get("rotation")
                         or (tablet.rotatable and _rotation_invariant(tablet)))]
        if len(matching) != 1:
            problems.append(f"石板 {instance.instance_id} 的位置或旋转非法")
            continue
        candidate = matching[0]
        applied = _tablet_applies(candidate, occupied, artifact_cells)
        if placed.get("applied", True) != applied:
            problems.append(f"石板 {instance.instance_id} 的限定条件状态错误")
        if applied:
            for cell, value in candidate.effects.items():
                effects[cell] += value
            for cell, value in candidate.multipliers.items():
                multipliers[cell] += value
            unlocks.update(candidate.unlocks)
            disabled.update(candidate.disables)
            for cell in set(candidate.effects) | set(candidate.multipliers):
                additive = candidate.effects.get(cell, 0)
                multiplier = candidate.multipliers.get(cell, 0)
                if additive or multiplier:
                    tablet_effects[cell].append({
                        "instanceId": instance.instance_id,
                        "typeId": instance.type_id,
                        "name": tablet.name,
                        "cell": candidate.cell,
                        "additive": additive,
                        "multiplier": multiplier,
                    })
        if instance.fixed_cell is not None and candidate.cell != instance.fixed_cell:
            problems.append(f"石板 {instance.instance_id} 未遵守固定位置")
        if instance.fixed_rotation is not None and placed.get("rotation") != instance.fixed_rotation:
            problems.append(f"石板 {instance.instance_id} 未遵守固定旋转")

    detail_by_instance = {item.get("instanceId"): item for item in result.get("artifacts", [])}
    for instance in request.artifacts:
        placed = by_instance.get(instance.instance_id)
        detail = detail_by_instance.get(instance.instance_id)
        if not placed or not detail:
            problems.append(f"神器 {instance.instance_id} 缺少明细")
            continue
        artifact = artifacts_by_id[instance.type_id]
        cell = placed["cell"]
        multiplier = max(1, multipliers[cell])
        expected_level = min(artifact.cap, (instance.base_level + effects[cell]) * multiplier)
        criteria_met = _artifact_criteria_met(
            artifact, cell, request, occupied, artifact_cells,
        )
        expected_active = (
            expected_level >= 0 and cell not in disabled
            and (criteria_met or cell in unlocks)
        )
        if (detail.get("rawBonus") != effects[cell]
                or detail.get("multiplier") != multiplier
                or detail.get("disabled") != (cell in disabled)
                or detail.get("level") != expected_level):
            problems.append(f"神器 {instance.instance_id} 等级计算错误")
        if detail.get("baseLevel") != instance.base_level:
            problems.append(f"神器 {instance.instance_id} 基础等级错误")
        if detail.get("active") != expected_active:
            problems.append(f"神器 {instance.instance_id} 生效状态错误")
        if detail.get("tabletEffects") != tablet_effects[cell]:
            problems.append(f"神器 {instance.instance_id} 石板效果来源错误")
        if instance.fixed_cell is not None and cell != instance.fixed_cell:
            problems.append(f"神器 {instance.instance_id} 未遵守固定位置")
        if instance.min_level is not None and expected_level < instance.min_level:
            problems.append(f"神器 {instance.instance_id} 未达到最低等级")
        if instance.exact_level is not None and expected_level != instance.exact_level:
            problems.append(f"神器 {instance.instance_id} 未达到固定等级")
    if result.get("cellEffects") != effects:
        problems.append("格子加成汇总错误")
    if result.get("cellMultipliers") != multipliers:
        problems.append("格子倍率汇总错误")
    if sorted(result.get("disabledCells", [])) != sorted(disabled):
        problems.append("禁用格汇总错误")
    if sorted(result.get("unlockedCells", [])) != sorted(unlocks):
        problems.append("解锁格汇总错误")
    tertiary = sum(
        (-min(0, effects[cell]) + int(cell in disabled)
         if cell in artifact_cells else max(0, effects[cell]) + multipliers[cell])
        for cell in range(request.cell_count)
    )
    if result.get("tertiaryObjective") != tertiary:
        problems.append("第三级目标汇总错误")
    empty_cells = set(range(request.cell_count)) - occupied
    empty_cell_score = sum(effects[cell] for cell in empty_cells)
    if result.get("emptyCellObjective") != empty_cell_score:
        problems.append("空闲格等级汇总错误")
    return problems
