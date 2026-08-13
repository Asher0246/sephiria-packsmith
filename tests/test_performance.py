from app.catalog import artifact_types, tablet_types
from app.models import ArtifactInstance, SolveRequest, TabletInstance
from app.solver import solve
from app.validation import validate_result


def test_medium_real_catalog_case_returns_valid_layout_within_limit():
    artifacts = {item.id: item for item in artifact_types()}
    tablets = {item.id: item for item in tablet_types()}
    chosen_artifacts = [item for item in artifacts.values() if item.cap >= 3 and not item.criteria][:10]
    selected_ids = {"tablet-dry", "tablet-approximation", "tablet-fate", "tablet-hope", "tablet-beating", "tablet-advance"}
    chosen_tablets = [item for item in tablets.values() if item.id in selected_ids]
    request = SolveRequest(4, 4,
        tuple(ArtifactInstance(f"a-{index}", item.id, weight=1 + index % 3) for index, item in enumerate(chosen_artifacts)),
        tuple(TabletInstance(f"t-{index}", item.id) for index, item in enumerate(chosen_tablets)),
        3_000,
    )
    result = solve(request, artifacts, tablets)
    assert result["solutionStatus"] in ("OPTIMAL", "FEASIBLE")
    assert result["solveMs"] < 4_500
    assert validate_result(request, artifacts, tablets, result) == []
