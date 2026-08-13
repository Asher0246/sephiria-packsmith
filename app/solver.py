from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from ortools.sat.python import cp_model

from .models import ArtifactType, SolveRequest, TabletType


PLANET_ARTIFACT_IDS = frozenset({
    "artifact-yellow_planet",
    "artifact-red_planet",
    "artifact-blue_planet",
    "artifact-sky_blue_planet",
    "artifact-white_planet",
    "artifact-black_planet",
    "artifact-ashen_planet",
})
SPECIAL_COMPLETION_SCALE = 1000


@dataclass(frozen=True)
class Candidate:
    cell: int
    rotation: int
    effects: dict[int, int]
    unlocks: frozenset[int]
    disables: frozenset[int] = frozenset()
    multipliers: dict[int, int] = field(default_factory=dict)
    conditions: tuple[tuple[int, str], ...] = ()


class StopController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._solver: cp_model.CpSolver | None = None
        self._stopped = False

    def attach(self, solver: cp_model.CpSolver) -> None:
        with self._lock:
            self._solver = solver
            if self._stopped:
                solver.stop_search()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._solver is not None:
                self._solver.stop_search()

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped


def _linear_offset(value: int) -> tuple[int, int]:
    dy = math.trunc(value / 6)
    return value - 6 * dy, dy


def _rotate(dx: int, dy: int, rotation: int) -> tuple[int, int]:
    for _ in range(rotation % 4):
        dx, dy = -dy, dx
    return dx, dy


def _base_cells(
    token: int | str, anchor_x: int, anchor_y: int,
    rows: int, cols: int, cell_count: int,
) -> list[tuple[int, int]]:
    if isinstance(token, int):
        return [_linear_offset(token)]
    offsets = {
        "UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0),
        "DIAUPLEFT": (-1, -1), "DIAUPRIGHT": (1, -1),
        "DIADOWNLEFT": (-1, 1), "DIADOWNRIGHT": (1, 1),
    }
    if token in offsets:
        return [offsets[token]]
    if token == "ROW":
        return [(x - anchor_x, 0) for x in range(cols)]
    if token == "COL":
        return [(0, y - anchor_y) for y in range(rows)]
    if token == "TOP":
        return [(x - anchor_x, -anchor_y) for x in range(cols)]
    if token == "BOTTOM":
        return [
            (cell % cols - anchor_x, cell // cols - anchor_y)
            for cell in range(max(0, cell_count - cols), cell_count)
        ]
    if token == "SLASH":
        delta = anchor_y - anchor_x
        return [(x - anchor_x, y - anchor_y) for y in range(rows) for x in range(cols) if y - x == delta]
    if token in {"CHECKERBOARD", "CHECKERBOARD2"}:
        parity = 0 if token == "CHECKERBOARD" else 1
        return [
            (x - anchor_x, y - anchor_y)
            for y in range(rows)
            for x in range(cols)
            if (x != anchor_x or y != anchor_y)
            and (x + y + anchor_x + anchor_y) % 2 == parity
        ]
    raise ValueError(f"未知石板方向标记: {token}")


def _anchor_allowed(constraint: str | None, x: int, y: int, rows: int, cols: int) -> bool:
    if constraint == "last_row":
        return y == rows - 1
    if constraint == "first_row":
        return y == 0
    if constraint == "first_or_last_col":
        return x in (0, cols - 1)
    return True


def _checkerboard_effect_values(tablet: TabletType) -> tuple[int, int] | None:
    """Return (same-parity, opposite-parity) effects for pure checkerboards."""
    if tablet.candidates is not None or not tablet.directions:
        return None
    values = {"CHECKERBOARD": 0, "CHECKERBOARD2": 0}
    for token, value in tablet.directions:
        if token not in values or not isinstance(value, int):
            return None
        values[token] += value
    return values["CHECKERBOARD"], values["CHECKERBOARD2"]


def _rotation_invariant(tablet: TabletType) -> bool:
    return _checkerboard_effect_values(tablet) is not None


def build_candidates(
    tablet: TabletType, rows: int, cols: int, cell_count: int | None = None,
) -> tuple[Candidate, ...]:
    cell_count = rows * cols if cell_count is None else cell_count
    if tablet.candidates is not None:
        raw_candidates = tablet.candidates.get(f"{rows}x{cols}")
        if raw_candidates is None:
            raise ValueError(f"石板 {tablet.name} 不支持 {rows}x{cols} 背包")
        return tuple(Candidate(
            int(raw[0]), int(raw[1]),
            {int(cell): int(value) for cell, value in raw[2] if int(cell) < cell_count},
            frozenset(int(cell) for cell in raw[3] if int(cell) < cell_count),
            frozenset(int(cell) for cell in (raw[4] if len(raw) > 4 else ()) if int(cell) < cell_count),
            {int(cell): int(value) for cell, value in (raw[5] if len(raw) > 5 else ()) if int(cell) < cell_count},
            tuple((int(cell), str(kind)) for cell, kind in (raw[6] if len(raw) > 6 else ())
                  if int(cell) < cell_count),
        ) for raw in raw_candidates if int(raw[0]) < cell_count)
    rotations = range(1) if _rotation_invariant(tablet) else (
        range(4) if tablet.rotatable else range(1)
    )
    result: list[Candidate] = []
    for cell in range(cell_count):
        anchor_y, anchor_x = divmod(cell, cols)
        if not _anchor_allowed(tablet.constraint, anchor_x, anchor_y, rows, cols):
            continue
        for rotation in rotations:
            effects: dict[int, int] = {}
            unlocks: set[int] = set()
            for token, value in tablet.directions:
                for dx, dy in _base_cells(token, anchor_x, anchor_y, rows, cols, cell_count):
                    if token not in {"CHECKERBOARD", "CHECKERBOARD2"}:
                        dx, dy = _rotate(dx, dy, rotation)
                    x, y = anchor_x + dx, anchor_y + dy
                    if not (0 <= x < cols and 0 <= y < rows):
                        continue
                    target = y * cols + x
                    if target >= cell_count:
                        continue
                    if value == "UNLOCK":
                        unlocks.add(target)
                    else:
                        effects[target] = effects.get(target, 0) + int(value)
            effects = {key: value for key, value in effects.items() if value}
            result.append(Candidate(cell, rotation, effects, frozenset(unlocks)))
    return tuple(result)


_candidates_cache: dict[
    tuple[str, int, int, int], tuple[TabletType, tuple[Candidate, ...]]
] = {}
_candidates_cache_lock = threading.Lock()


def build_candidates_cached(
    tablet: TabletType, rows: int, cols: int, cell_count: int | None = None,
) -> tuple[Candidate, ...]:
    cell_count = rows * cols if cell_count is None else cell_count
    key = (tablet.id, rows, cols, cell_count)
    with _candidates_cache_lock:
        cached = _candidates_cache.get(key)
        if cached is not None and cached[0] == tablet:
            return cached[1]
    built = build_candidates(tablet, rows, cols, cell_count)
    with _candidates_cache_lock:
        _candidates_cache[key] = (tablet, built)
    return built


def _candidate_behavior(candidate: Candidate) -> tuple:
    return (
        candidate.cell,
        tuple(sorted(candidate.effects.items())),
        tuple(sorted(candidate.unlocks)),
        tuple(sorted(candidate.disables)),
        tuple(sorted(candidate.multipliers.items())),
        tuple(sorted(candidate.conditions)),
    )


def _deduplicate_candidates(
    candidates: tuple[Candidate, ...], preferred_rotation: int | None = None,
) -> tuple[Candidate, ...]:
    unique: dict[tuple, Candidate] = {}
    for candidate in candidates:
        key = _candidate_behavior(candidate)
        current = unique.get(key)
        if current is None or (
            preferred_rotation is not None
            and candidate.rotation == preferred_rotation
            and current.rotation != preferred_rotation
        ):
            unique[key] = candidate
    return tuple(unique.values())


def _bool_or(model: cp_model.CpModel, values: list, name: str):
    if not values:
        return model.new_constant(0)
    if len(values) == 1:
        return values[0]
    out = model.new_bool_var(name)
    model.add_max_equality(out, values)
    return out


def _bool_and(model: cp_model.CpModel, values: list, name: str):
    if not values:
        return model.new_constant(1)
    if len(values) == 1:
        return values[0]
    out = model.new_bool_var(name)
    model.add_min_equality(out, values)
    return out


def _static_criterion(kind: str, cell: int, rows: int, cols: int, cell_count: int) -> bool | None:
    y, x = divmod(cell, cols)
    if kind == "edge":
        return x in (0, cols - 1) or y in (0, rows - 1)
    if kind == "inner":
        return 0 < x < cols - 1 and 0 < y < rows - 1
    if kind == "top":
        return y == 0
    if kind == "bottom":
        return y == rows - 1
    if kind == "side_end":
        return x in (0, cols - 1) or cell == cell_count - 1
    return None


def _neighbor_cells(cell: int, rows: int, cols: int, cell_count: int) -> tuple[int, ...]:
    y, x = divmod(cell, cols)
    return tuple(
        ny * cols + nx
        for ny in range(max(0, y - 1), min(rows, y + 2))
        for nx in range(max(0, x - 1), min(cols, x + 2))
        if (nx != x or ny != y) and ny * cols + nx < cell_count
    )


def _special_objective(
    model: cp_model.CpModel,
    request: SolveRequest,
    artifact_types: list[ArtifactType],
    x: dict,
    artifact_occupied: list,
    score_levels: list,
    active: list,
) -> tuple[list, list[tuple[int, str, object, int, object, object]]]:
    enabled = [
        (index, artifact.special_condition)
        for index, (instance, artifact) in enumerate(zip(request.artifacts, artifact_types))
        if instance.special_priority and artifact.special_condition
    ]
    if not enabled:
        return [], []

    cells = range(request.cell_count)
    rewards = []
    details: list[tuple[int, str, object, int, object, object]] = []
    max_cap = max((artifact.cap for artifact in artifact_types), default=0)
    max_neighbor_count = max((len(_neighbor_cells(
        c, request.rows, request.cols, request.cell_count,
    )) for c in cells), default=0)

    cell_levels = None
    if any(kind == "nearby_levels" for _, kind in enabled):
        cell_levels = []
        for c in cells:
            placed_levels = []
            for a, artifact in enumerate(artifact_types):
                placed = model.new_int_var(0, artifact.cap, f"special_level_{a}_{c}")
                model.add_multiplication_equality(placed, [score_levels[a], x[a, c]])
                placed_levels.append(placed)
            total = model.new_int_var(0, max_cap, f"special_cell_level_{c}")
            model.add(total == sum(placed_levels))
            cell_levels.append(total)

    planet_occupied = None
    if any(kind == "nearby_planets" for _, kind in enabled):
        planet_indexes = [a for a, artifact in enumerate(artifact_types) if artifact.id in PLANET_ARTIFACT_IDS]
        planet_occupied = []
        for c in cells:
            value = model.new_bool_var(f"planet_occupied_{c}")
            model.add(value == sum(x[a, c] for a in planet_indexes))
            planet_occupied.append(value)

    shared_side_category = None
    if any(kind == "matching_side_categories" for _, kind in enabled):
        categories = sorted({category for artifact in artifact_types for category in artifact.categories})
        category_occupied = {}
        for category in categories:
            indexes = [a for a, artifact in enumerate(artifact_types) if category in artifact.categories]
            for c in cells:
                value = model.new_bool_var(f"category_{len(category_occupied)}_{c}")
                model.add(value == sum(x[a, c] for a in indexes))
                category_occupied[category, c] = value
        shared_side_category = []
        for c in cells:
            _, cx = divmod(c, request.cols)
            if cx == 0 or cx == request.cols - 1 or c + 1 >= request.cell_count:
                shared_side_category.append(model.new_constant(0))
                continue
            matches = [
                _bool_and(
                    model,
                    [category_occupied[category, c - 1], category_occupied[category, c + 1]],
                    f"matching_category_{c}_{index}",
                )
                for index, category in enumerate(categories)
            ]
            shared_side_category.append(_bool_or(model, matches, f"shared_side_category_{c}"))

    instance_indexes = {instance.instance_id: index for index, instance in enumerate(request.artifacts)}
    top_cells = range(min(request.cols, request.cell_count))
    top_artifact_count = model.new_int_var(0, len(top_cells), "top_artifact_count")
    model.add(top_artifact_count == sum(artifact_occupied[c] for c in top_cells))

    for a, kind in enabled:
        instance = request.artifacts[a]
        if kind == "nearby_levels":
            upper = min(8, max(0, request.cell_count - 1)) * max_cap
            max_score = sum(sorted(
                (artifact.cap for index, artifact in enumerate(artifact_types) if index != a),
                reverse=True,
            )[:max_neighbor_count])
            at_cells = []
            for c in cells:
                located_active = _bool_and(model, [x[a, c], active[a]], f"special_active_{a}_{c}")
                neighbors = _neighbor_cells(c, request.rows, request.cols, request.cell_count)
                nearby_total = model.new_int_var(0, len(neighbors) * max_cap, f"nearby_level_total_{a}_{c}")
                model.add(nearby_total == sum(cell_levels[n] for n in neighbors))
                value = model.new_int_var(0, upper, f"nearby_levels_{a}_{c}")
                model.add_multiplication_equality(value, [nearby_total, located_active])
                at_cells.append(value)
            raw_score = model.new_int_var(0, upper, f"special_raw_{a}")
            model.add(raw_score == sum(at_cells))
        elif kind == "top_row_artifacts":
            if request.rows == 1:
                max_score = min(len(top_cells), len(request.artifacts))
            else:
                max_score = min(len(top_cells), max(0, len(request.artifacts) - 1))
            raw_score = model.new_int_var(0, len(top_cells), f"special_raw_{a}")
            model.add_multiplication_equality(raw_score, [top_artifact_count, active[a]])
        elif kind == "nearby_planets":
            max_score = min(
                max_neighbor_count,
                sum(artifact.id in PLANET_ARTIFACT_IDS for artifact in artifact_types),
            )
            at_cells = []
            for c in cells:
                located_active = _bool_and(model, [x[a, c], active[a]], f"special_active_{a}_{c}")
                neighbors = _neighbor_cells(c, request.rows, request.cols, request.cell_count)
                nearby_total = model.new_int_var(0, len(neighbors), f"nearby_planet_total_{a}_{c}")
                model.add(nearby_total == sum(planet_occupied[n] for n in neighbors))
                value = model.new_int_var(0, 8, f"nearby_planets_{a}_{c}")
                model.add_multiplication_equality(value, [nearby_total, located_active])
                at_cells.append(value)
            raw_score = model.new_int_var(0, 8, f"special_raw_{a}")
            model.add(raw_score == sum(at_cells))
        elif kind == "matching_side_categories":
            max_score = 1
            satisfied_at = [
                _bool_and(model, [x[a, c], active[a], shared_side_category[c]], f"matching_sides_{a}_{c}")
                for c in cells
            ]
            raw_score = _bool_or(model, satisfied_at, f"special_raw_{a}")
        elif kind == "target_above":
            max_score = 1
            target = instance_indexes[instance.special_target_instance_id]
            satisfied_at = [
                _bool_and(model, [x[a, c], x[target, c - request.cols], active[a]], f"target_above_{a}_{c}")
                for c in range(request.cols, request.cell_count)
            ]
            raw_score = _bool_or(model, satisfied_at, f"special_raw_{a}")
        else:
            continue
        completion = model.new_int_var(0, SPECIAL_COMPLETION_SCALE, f"special_completion_{a}")
        if max_score > 0:
            scaled_raw = model.new_int_var(
                0, max_score * SPECIAL_COMPLETION_SCALE, f"special_scaled_raw_{a}",
            )
            model.add(scaled_raw == raw_score * SPECIAL_COMPLETION_SCALE)
            model.add_division_equality(completion, scaled_raw, max_score)
        else:
            model.add(completion == 0)
        weighted = model.new_int_var(
            0, instance.weight * SPECIAL_COMPLETION_SCALE, f"special_weighted_{a}",
        )
        model.add(weighted == completion * instance.weight)
        rewards.append(weighted)
        details.append((a, kind, raw_score, max_score, completion, weighted))
    return rewards, details


def solve(
    request: SolveRequest,
    artifacts_by_id: dict[str, ArtifactType],
    tablets_by_id: dict[str, TabletType],
    controller: StopController | None = None,
    on_solver: Callable[[cp_model.CpSolver], None] | None = None,
) -> dict:
    started = time.perf_counter()
    controller = controller or StopController()
    model = cp_model.CpModel()
    cells = range(request.cell_count)

    artifact_types = [artifacts_by_id[item.type_id] for item in request.artifacts]
    tablet_types = [tablets_by_id[item.type_id] for item in request.tablets]
    candidates: list[tuple[Candidate, ...]] = []
    raw_candidate_count = 0
    for instance, tablet in zip(request.tablets, tablet_types):
        possible = build_candidates_cached(tablet, request.rows, request.cols, request.cell_count)
        possible = tuple(candidate for candidate in possible
                         if instance.fixed_cell is None or candidate.cell == instance.fixed_cell)
        if instance.fixed_rotation is not None:
            if tablet.rotatable and _rotation_invariant(tablet):
                possible = tuple(replace(candidate, rotation=instance.fixed_rotation)
                                 for candidate in possible)
            else:
                possible = tuple(candidate for candidate in possible
                                 if candidate.rotation == instance.fixed_rotation)
        raw_candidate_count += len(possible)
        if instance.fixed_rotation is None:
            possible = _deduplicate_candidates(possible, instance.preferred_rotation)
        if not possible:
            return _empty_result("INFEASIBLE", "石板固定约束没有合法位置", started)
        candidates.append(possible)

    x = {(a, c): model.new_bool_var(f"a_{a}_{c}")
         for a in range(len(request.artifacts)) for c in cells}
    y = {(t, k): model.new_bool_var(f"t_{t}_{k}")
         for t, possible in enumerate(candidates) for k in range(len(possible))}
    for a, item in enumerate(request.artifacts):
        model.add(sum(x[a, c] for c in cells) == 1)
        if item.fixed_cell is not None:
            model.add(x[a, item.fixed_cell] == 1)

    # Symmetry breaking: interchangeable artifacts (same type, weight, base
    # level, constraints and no fixed cell) are ordered by cell index so the
    # solver does not explore equivalent permutations.
    artifact_groups: dict[tuple, list[int]] = {}
    for a, item in enumerate(request.artifacts):
        if item.fixed_cell is not None:
            continue
        key = (
            item.type_id, item.weight, item.base_level, item.min_level,
            item.exact_level, item.special_priority,
            item.special_target_instance_id,
        )
        artifact_groups.setdefault(key, []).append(a)
    for indices in artifact_groups.values():
        for left, right in zip(indices, indices[1:]):
            model.add(
                sum(c * x[left, c] for c in cells)
                <= sum(c * x[right, c] for c in cells)
            )

    for t, possible in enumerate(candidates):
        model.add(sum(y[t, k] for k in range(len(possible))) == 1)

    tablet_groups: dict[tuple[str, int | None], list[int]] = {}
    for t, item in enumerate(request.tablets):
        if item.fixed_cell is None and item.fixed_rotation is None:
            tablet_groups.setdefault((item.type_id, item.preferred_rotation), []).append(t)
    for indices in tablet_groups.values():
        for left, right in zip(indices, indices[1:]):
            model.add(
                sum(candidate.cell * y[left, k]
                    for k, candidate in enumerate(candidates[left]))
                <= sum(candidate.cell * y[right, k]
                       for k, candidate in enumerate(candidates[right]))
            )

    occupied = []
    artifact_occupied = []
    tablet_anchors = [[] for _ in cells]
    for t, possible in enumerate(candidates):
        for k, candidate in enumerate(possible):
            tablet_anchors[candidate.cell].append(y[t, k])
    for c in cells:
        artifact_sum = sum(x[a, c] for a in range(len(request.artifacts)))
        tablet_sum = sum(tablet_anchors[c])
        model.add(artifact_sum + tablet_sum <= 1)
        a_occ = model.new_bool_var(f"artifact_occ_{c}")
        model.add(a_occ == artifact_sum)
        artifact_occupied.append(a_occ)
        occ = model.new_bool_var(f"occupied_{c}")
        model.add(occ == artifact_sum + tablet_sum)
        occupied.append(occ)

    candidate_applied = {}
    for t, possible in enumerate(candidates):
        for k, candidate in enumerate(possible):
            required = []
            placed_cells = []
            for condition_cell, condition_kind in candidate.conditions:
                if condition_kind == "ITEM":
                    required.append(occupied[condition_cell])
                elif condition_kind == "CHARM":
                    required.append(artifact_occupied[condition_cell])
                elif condition_kind == "PLACED":
                    placed_cells.append(condition_cell)
                else:
                    raise ValueError(f"未知石板限定条件: {condition_kind}")
            if placed_cells:
                required.append(model.new_constant(int(candidate.cell in placed_cells)))
            if not required:
                candidate_applied[t, k] = y[t, k]
            else:
                condition = _bool_and(model, required, f"tablet_condition_{t}_{k}")
                candidate_applied[t, k] = _bool_and(
                    model, [y[t, k], condition], f"tablet_applied_{t}_{k}",
                )

    checkerboard_aggregates = {}
    for t, (tablet, possible) in enumerate(zip(tablet_types, candidates)):
        effect_values = _checkerboard_effect_values(tablet)
        if effect_values is None:
            continue
        even_applied = model.new_bool_var(f"checkerboard_even_{t}")
        odd_applied = model.new_bool_var(f"checkerboard_odd_{t}")
        model.add(even_applied == sum(
            candidate_applied[t, k]
            for k, candidate in enumerate(possible)
            if (candidate.cell // request.cols + candidate.cell % request.cols) % 2 == 0
        ))
        model.add(odd_applied == sum(
            candidate_applied[t, k]
            for k, candidate in enumerate(possible)
            if (candidate.cell // request.cols + candidate.cell % request.cols) % 2 == 1
        ))
        checkerboard_aggregates[t] = (
            *effect_values,
            even_applied,
            odd_applied,
            {candidate.cell: candidate_applied[t, k]
             for k, candidate in enumerate(possible)},
        )

    effect_terms = [[] for _ in cells]
    effect_bounds = [[0, 0] for _ in cells]
    unlock_terms_by_cell = [[] for _ in cells]
    disable_terms_by_cell = [[] for _ in cells]
    multiplier_terms_by_cell = [[] for _ in cells]
    multiplier_highs = [0 for _ in cells]
    for t, possible in enumerate(candidates):
        per_cell_values: dict[int, list[int]] = {}
        per_cell_multipliers: dict[int, list[int]] = {}
        for k, candidate in enumerate(possible):
            applied = candidate_applied[t, k]
            for cell, value in candidate.effects.items():
                per_cell_values.setdefault(cell, []).append(value)
                if t not in checkerboard_aggregates:
                    effect_terms[cell].append(value * applied)
            for cell in candidate.unlocks:
                unlock_terms_by_cell[cell].append(applied)
            for cell in candidate.disables:
                disable_terms_by_cell[cell].append(applied)
            for cell, value in candidate.multipliers.items():
                per_cell_multipliers.setdefault(cell, []).append(value)
                multiplier_terms_by_cell[cell].append(value * applied)
        for cell, values in per_cell_values.items():
            effect_bounds[cell][0] += min(0, *values)
            effect_bounds[cell][1] += max(0, *values)
        for cell, values in per_cell_multipliers.items():
            multiplier_highs[cell] += max(values)

    raw_bounds: list[tuple[int, int]] = []
    raw_bonus = []
    unlocked = []
    disabled = []
    multiplier_sums = []
    multiplier_factors = []
    for c in cells:
        terms = effect_terms[c]
        low, high = effect_bounds[c]
        for aggregate in checkerboard_aggregates.values():
            (same_value, opposite_value, even_applied,
             odd_applied, applied_by_cell) = aggregate
            target_even = (c // request.cols + c % request.cols) % 2 == 0
            same_applied = even_applied if target_even else odd_applied
            opposite_applied = odd_applied if target_even else even_applied
            terms.append(same_value * same_applied)
            terms.append(opposite_value * opposite_applied)
            if c in applied_by_cell:
                terms.append(-same_value * applied_by_cell[c])
        raw = model.new_int_var(low, high, f"raw_{c}")
        model.add(raw == sum(terms))
        raw_bonus.append(raw)
        raw_bounds.append((low, high))
        unlocked.append(_bool_or(model, unlock_terms_by_cell[c], f"unlocked_{c}"))
        disabled.append(_bool_or(model, disable_terms_by_cell[c], f"disabled_{c}"))
        multiplier_sum = model.new_int_var(0, multiplier_highs[c], f"multiplier_sum_{c}")
        model.add(multiplier_sum == sum(multiplier_terms_by_cell[c]))
        multiplier_sums.append(multiplier_sum)
        multiplier_factor = model.new_int_var(1, max(1, multiplier_highs[c]), f"multiplier_factor_{c}")
        model.add_max_equality(multiplier_factor, [multiplier_sum, 1])
        multiplier_factors.append(multiplier_factor)

    # The tertiary objective measures effects that do not help an artifact:
    # negative net effects on artifact cells and positive net effects on all
    # other cells.  It is deliberately kept separate from level objectives so
    # it can only break ties after the first two objectives are fixed.
    positive_effects = []
    negative_effects = []
    for c, (low, high) in enumerate(raw_bounds):
        positive = model.new_int_var(0, max(0, high), f"positive_effect_{c}")
        model.add_max_equality(positive, [raw_bonus[c], 0])
        negative = model.new_int_var(0, max(0, -low), f"negative_effect_{c}")
        model.add_max_equality(negative, [-raw_bonus[c], 0])
        positive_effects.append(positive)
        negative_effects.append(negative)

    negative_on_artifact = []
    positive_on_artifact = []
    disabled_on_artifact = []
    multiplier_on_artifact = []
    for c in cells:
        negative_used = model.new_int_var(0, max(0, -raw_bounds[c][0]), f"negative_used_{c}")
        model.add_multiplication_equality(negative_used, [negative_effects[c], artifact_occupied[c]])
        positive_used = model.new_int_var(0, max(0, raw_bounds[c][1]), f"positive_used_{c}")
        model.add_multiplication_equality(positive_used, [positive_effects[c], artifact_occupied[c]])
        negative_on_artifact.append(negative_used)
        positive_on_artifact.append(positive_used)
        disabled_used = model.new_bool_var(f"disabled_used_{c}")
        model.add_multiplication_equality(disabled_used, [disabled[c], artifact_occupied[c]])
        disabled_on_artifact.append(disabled_used)
        multiplier_used = model.new_int_var(0, multiplier_sums[c].Proto().domain[-1], f"multiplier_used_{c}")
        model.add_multiplication_equality(multiplier_used, [multiplier_sums[c], artifact_occupied[c]])
        multiplier_on_artifact.append(multiplier_used)

    levels = []
    active = []
    score_levels = []
    weighted_contributions = []
    level_transforms: dict[tuple[int, int], tuple[list, int]] = {}
    for item, artifact in zip(request.artifacts, artifact_types):
        key = (item.base_level, artifact.cap)
        if key in level_transforms:
            continue
        cell_levels = []
        cell_level_bounds = []
        for c in cells:
            low, high = raw_bounds[c]
            factor_high = multiplier_factors[c].Proto().domain[-1]
            pre_low, pre_high = item.base_level + low, item.base_level + high
            product_low = min(pre_low, pre_low * factor_high, pre_high, pre_high * factor_high)
            product_high = max(pre_low, pre_low * factor_high, pre_high, pre_high * factor_high)
            pre_level = model.new_int_var(pre_low, pre_high, f"pre_level_{len(level_transforms)}_{c}")
            model.add(pre_level == raw_bonus[c] + item.base_level)
            multiplied = model.new_int_var(
                product_low, product_high, f"multiplied_level_{len(level_transforms)}_{c}",
            )
            model.add_multiplication_equality(multiplied, [pre_level, multiplier_factors[c]])
            capped_low = min(product_low, artifact.cap)
            capped = model.new_int_var(capped_low, artifact.cap, f"capped_{len(level_transforms)}_{c}")
            model.add_min_equality(capped, [multiplied, artifact.cap])
            cell_levels.append(capped)
            cell_level_bounds.append(capped_low)
        level_transforms[key] = (cell_levels, min(cell_level_bounds))

    for a, (item, artifact) in enumerate(zip(request.artifacts, artifact_types)):
        cell_levels, level_low = level_transforms[item.base_level, artifact.cap]
        level = model.new_int_var(level_low, artifact.cap, f"level_{a}")
        for c in cells:
            model.add(level == cell_levels[c]).only_enforce_if(x[a, c])
        if item.min_level is not None:
            model.add(level >= item.min_level)
        if item.exact_level is not None:
            model.add(level == item.exact_level)
        levels.append(level)

        level_active = model.new_bool_var(f"level_active_{a}")
        model.add(level >= 0).only_enforce_if(level_active)
        model.add(level <= -1).only_enforce_if(level_active.Not())

        active_at = []
        for c in cells:
            cy, cx = divmod(c, request.cols)
            criterion_values = []
            for criterion in artifact.criteria:
                static = _static_criterion(criterion, c, request.rows, request.cols, request.cell_count)
                if static is not None:
                    criterion_values.append(model.new_constant(int(static)))
                elif criterion == "side_free":
                    neighbors = []
                    if cx > 0:
                        left_empty = model.new_bool_var(f"left_empty_{a}_{c}")
                        model.add(left_empty + occupied[c - 1] == 1)
                        neighbors.append(left_empty)
                    if cx < request.cols - 1 and c + 1 < request.cell_count:
                        right_empty = model.new_bool_var(f"right_empty_{a}_{c}")
                        model.add(right_empty + occupied[c + 1] == 1)
                        neighbors.append(right_empty)
                    criterion_values.append(_bool_and(model, neighbors, f"side_free_{a}_{c}"))
                elif criterion == "both_side_artifacts":
                    if 0 < cx < request.cols - 1 and c + 1 < request.cell_count:
                        criterion_values.append(_bool_and(
                            model, [artifact_occupied[c - 1], artifact_occupied[c + 1]], f"both_artifacts_{a}_{c}"))
                    else:
                        criterion_values.append(model.new_constant(0))
            condition = _bool_and(model, criterion_values, f"condition_{a}_{c}")
            eligible = _bool_or(model, [condition, unlocked[c]], f"eligible_{a}_{c}")
            active_at.append(_bool_and(
                model, [x[a, c], eligible, level_active, disabled[c].Not()], f"active_at_{a}_{c}",
            ))
        is_active = model.new_bool_var(f"active_{a}")
        model.add(is_active == sum(active_at))
        active.append(is_active)

        nonnegative_level = model.new_int_var(0, artifact.cap, f"positive_level_{a}")
        model.add_max_equality(nonnegative_level, [level, 0])
        scoring = model.new_int_var(0, artifact.cap, f"score_level_{a}")
        model.add_multiplication_equality(scoring, [nonnegative_level, is_active])
        score_levels.append(scoring)
        weighted_contributions.append(scoring * item.weight)

    special_rewards, special_variables = _special_objective(
        model, request, artifact_types, x, artifact_occupied, score_levels, active,
    )
    primary = sum(weighted_contributions)
    secondary = sum(score_levels)
    special = sum(special_rewards)
    tertiary = sum(
        negative_on_artifact[c] + positive_effects[c] - positive_on_artifact[c]
        + disabled_on_artifact[c] + multiplier_sums[c] - multiplier_on_artifact[c]
        for c in cells
    )
    empty_cell_levels = []
    for c, (low, high) in enumerate(raw_bounds):
        empty_level = model.new_int_var(min(0, low), max(0, high), f"empty_level_{c}")
        model.add(empty_level == raw_bonus[c]).only_enforce_if(occupied[c].Not())
        model.add(empty_level == 0).only_enforce_if(occupied[c])
        empty_cell_levels.append(empty_level)
    empty_cell_score = sum(empty_cell_levels)
    # Each scale exceeds the full range of every lower-priority objective, so
    # these weighted sums are exactly equivalent to the original lexicographic order.
    secondary_upper = sum(artifact.cap for artifact in artifact_types)
    level_scale = secondary_upper + 1
    level_objective = primary * level_scale + secondary

    tertiary_upper = sum(
        max(0, -low) + max(0, high) + 1 + multiplier_highs[c]
        for c, (low, high) in enumerate(raw_bounds)
    )
    empty_lower = sum(min(0, low) for low, _ in raw_bounds)
    empty_upper = sum(max(0, high) for _, high in raw_bounds)
    empty_span = empty_upper - empty_lower
    empty_scale = empty_span + 1
    special_scale = (tertiary_upper + 1) * empty_scale
    refinement_objective = (
        special * special_scale
        + (tertiary_upper - tertiary) * empty_scale
        + (empty_cell_score - empty_lower)
    )

    model.maximize(level_objective)
    built = time.perf_counter()
    deadline = built + request.time_limit_ms / 1000
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.001, deadline - time.perf_counter())
    if request.worker_count:
        solver.parameters.num_search_workers = request.worker_count
    solver.parameters.random_seed = 1
    controller.attach(solver)
    if on_solver:
        on_solver(solver)
    phase1_status = solver.solve(model)
    status_name = solver.status_name(phase1_status)
    if phase1_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _empty_result("STOPPED" if controller.stopped else status_name,
                             "求解已停止" if controller.stopped else "没有满足全部约束的排布", started, built)

    primary_value = solver.value(primary)
    combined_bound = math.ceil(solver.best_objective_bound - 1e-6)
    primary_bound = combined_bound // level_scale
    best_solver = solver
    secondary_status = "OPTIMAL" if phase1_status == cp_model.OPTIMAL else "NOT_RUN"
    special_status = "NOT_RUN" if special_rewards else "DISABLED"
    tertiary_status = "NOT_RUN"
    empty_cell_status = "NOT_RUN"

    placement_vars = list(x.values()) + list(y.values())
    optimization_phases = 1

    def run_refinement(hint_solver) -> tuple[int | None, cp_model.CpSolver | None]:
        nonlocal optimization_phases
        remaining = deadline - time.perf_counter()
        if remaining <= 0 or controller.stopped:
            return None, None
        optimization_phases += 1
        model.maximize(refinement_objective)
        next_solver = cp_model.CpSolver()
        next_solver.parameters.max_time_in_seconds = remaining
        if request.worker_count:
            next_solver.parameters.num_search_workers = request.worker_count
        next_solver.parameters.random_seed = 1
        model.clear_hints()
        for var in placement_vars:
            value = hint_solver.value(var)
            if value is not None:
                model.add_hint(var, int(round(value)))
        controller.attach(next_solver)
        if on_solver:
            on_solver(next_solver)
        return next_solver.solve(model), next_solver

    if phase1_status == cp_model.OPTIMAL and not controller.stopped:
        model.add(level_objective == solver.value(level_objective))
        refinement_status, refinement_solver = run_refinement(best_solver)
        if refinement_status is not None and refinement_solver is not None:
            refinement_name = refinement_solver.status_name(refinement_status)
            if special_rewards:
                special_status = refinement_name
            tertiary_status = refinement_name
            empty_cell_status = refinement_name
            if refinement_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                best_solver = refinement_solver

    selected_tablets: dict[int, Candidate] = {}
    selected_tablets_applied: dict[int, bool] = {}
    for t, possible in enumerate(candidates):
        selected_index = next(k for k in range(len(possible)) if best_solver.boolean_value(y[t, k]))
        selected_tablets[t] = possible[selected_index]
        selected_tablets_applied[t] = best_solver.boolean_value(candidate_applied[t, selected_index])
    effects = [0] * request.cell_count
    multipliers = [0] * request.cell_count
    unlock_cells: set[int] = set()
    disabled_cells: set[int] = set()
    for t, candidate in selected_tablets.items():
        if not selected_tablets_applied[t]:
            continue
        for cell, value in candidate.effects.items():
            effects[cell] += value
        unlock_cells.update(candidate.unlocks)
        disabled_cells.update(candidate.disables)
        for cell, value in candidate.multipliers.items():
            multipliers[cell] += value

    placements = []
    artifact_details = []
    for a, (item, artifact) in enumerate(zip(request.artifacts, artifact_types)):
        cell = next(c for c in cells if best_solver.boolean_value(x[a, c]))
        value = best_solver.value(levels[a])
        is_active = best_solver.boolean_value(active[a])
        tablet_effects = []
        for t, (tablet_item, tablet) in enumerate(zip(request.tablets, tablet_types)):
            candidate = selected_tablets[t]
            if not selected_tablets_applied[t]:
                continue
            additive = candidate.effects.get(cell, 0)
            multiplier = candidate.multipliers.get(cell, 0)
            if not additive and not multiplier:
                continue
            tablet_effects.append({
                "instanceId": tablet_item.instance_id,
                "typeId": tablet_item.type_id,
                "name": tablet.name,
                "cell": candidate.cell,
                "additive": additive,
                "multiplier": multiplier,
            })
        placements.append({"kind": "artifact", "instanceId": item.instance_id, "typeId": item.type_id, "cell": cell})
        artifact_details.append({
            "instanceId": item.instance_id, "typeId": item.type_id, "name": artifact.name, "cell": cell,
            "baseLevel": item.base_level, "rawBonus": effects[cell],
            "multiplier": max(1, multipliers[cell]), "disabled": cell in disabled_cells, "level": value,
            "cap": artifact.cap, "active": is_active,
            "weight": item.weight, "contribution": max(0, value) * item.weight if is_active else 0,
            "tabletEffects": tablet_effects,
        })
    for t, (item, tablet) in enumerate(zip(request.tablets, tablet_types)):
        candidate = selected_tablets[t]
        range_cells = set(candidate.effects)
        range_cells.update(candidate.unlocks)
        range_cells.update(candidate.disables)
        range_cells.update(candidate.multipliers)
        range_cells.update(cell for cell, _ in candidate.conditions)
        placements.append({
            "kind": "tablet", "instanceId": item.instance_id, "typeId": item.type_id,
            "cell": candidate.cell, "rotation": candidate.rotation,
            "applied": selected_tablets_applied[t],
            "rangeCells": sorted(range_cells),
        })

    secondary_value = sum(detail["level"] for detail in artifact_details if detail["active"] and detail["level"] > 0)
    special_details = [{
        "instanceId": request.artifacts[a].instance_id,
        "typeId": request.artifacts[a].type_id,
        "condition": kind,
        "rawScore": best_solver.value(raw_score),
        "maxScore": max_score,
        "completion": best_solver.value(completion),
        "weight": request.artifacts[a].weight,
        "weightedScore": best_solver.value(weighted),
        "satisfied": best_solver.value(raw_score) > 0,
    } for a, kind, raw_score, max_score, completion, weighted in special_variables]
    special_value = sum(detail["weightedScore"] for detail in special_details)
    artifact_cells = {detail["cell"] for detail in artifact_details}
    tertiary_value = sum(
        (-min(0, effects[cell]) + int(cell in disabled_cells)
         if cell in artifact_cells else max(0, effects[cell]) + multipliers[cell])
        for cell in cells
    )
    occupied_cells = {item["cell"] for item in placements}
    empty_cell_value = sum(
        effects[cell] for cell in cells if cell not in occupied_cells
    )
    optimal = (phase1_status == cp_model.OPTIMAL
               and secondary_status == "OPTIMAL"
               and special_status in ("DISABLED", "OPTIMAL")
               and tertiary_status == "OPTIMAL"
               and empty_cell_status == "OPTIMAL")
    gap = 0.0 if phase1_status == cp_model.OPTIMAL else max(0.0, (primary_bound - primary_value) / max(1, abs(primary_value)))
    return {
        "solutionStatus": "OPTIMAL" if optimal else ("STOPPED" if controller.stopped else "FEASIBLE"),
        "secondaryStatus": secondary_status, "specialStatus": special_status,
        "tertiaryStatus": tertiary_status, "emptyCellStatus": empty_cell_status,
        "message": "已证明最优" if optimal else "已找到可行排布",
        "primaryObjective": primary_value, "secondaryObjective": secondary_value,
        "specialObjective": special_value, "specialDetails": special_details,
        "tertiaryObjective": tertiary_value, "emptyCellObjective": empty_cell_value,
        "primaryBestBound": primary_value if phase1_status == cp_model.OPTIMAL else primary_bound,
        "relativeGap": gap, "placements": placements, "artifacts": artifact_details,
        "cellEffects": effects, "cellMultipliers": multipliers,
        "disabledCells": sorted(disabled_cells), "unlockedCells": sorted(unlock_cells),
        "buildMs": round((built - started) * 1000), "solveMs": round((time.perf_counter() - built) * 1000),
        "diagnostics": {
            "tabletCandidates": sum(map(len, candidates)),
            "rawTabletCandidates": raw_candidate_count,
            "levelTransformGroups": len(level_transforms),
            "optimizationPhases": optimization_phases,
            "artifacts": len(request.artifacts),
            "tablets": len(request.tablets), "workerCount": request.worker_count,
        },
    }


def _empty_result(status: str, message: str, started: float, built: float | None = None) -> dict:
    built = built or time.perf_counter()
    return {
        "solutionStatus": status, "secondaryStatus": "NOT_RUN", "specialStatus": "NOT_RUN",
        "tertiaryStatus": "NOT_RUN", "emptyCellStatus": "NOT_RUN", "message": message,
        "primaryObjective": None, "secondaryObjective": None, "specialObjective": None,
        "specialDetails": [], "tertiaryObjective": None, "emptyCellObjective": None,
        "primaryBestBound": None,
        "relativeGap": None, "placements": [], "artifacts": [], "cellEffects": [],
        "cellMultipliers": [], "disabledCells": [], "unlockedCells": [],
        "buildMs": round((built - started) * 1000),
        "solveMs": round((time.perf_counter() - built) * 1000), "diagnostics": {},
    }
