"""GPU tests are optional; ordinary installations do not need CuPy."""
from dataclasses import replace
import pytest

from app.models import ArtifactInstance, ArtifactType, SolveRequest, TabletInstance, TabletType
from app.result_cache import ResultCache
from app.server import AppState
from app.solver import solve
from app.validation import validate_result
from tools.gpu_search import GpuSearch


@pytest.fixture(scope="module")
def gpu():
    cp = pytest.importorskip("cupy")
    try:
        if cp.cuda.runtime.getDeviceCount() == 0:
            pytest.skip("No CUDA device")
    except cp.cuda.runtime.CUDARuntimeError:
        pytest.skip("CUDA driver unavailable")
    return GpuSearch()


def example():
    artifact = ArtifactType("a", "Artifact", cap=5, rarity=0)
    tablet = TabletType("t", "Mixed", "test", True, None, None, candidates={
        "1x4": (
            (0, 0, ((1, -3), (2, 2)), (), (3,), ((2, 2),)),
            (0, 1, ((2, 1), (3, 2)), (), (1,), ((3, 3),)),
            (3, 0, ((0, 3), (1, -2)), (), (2,), ((0, 2),)),
        )
    })
    request = SolveRequest(1, 4, (
        ArtifactInstance("a1", "a", weight=3, base_level=1),
        ArtifactInstance("a2", "a", weight=7),
    ), (TabletInstance("t1", "t"),), 1000, double_level_cells=frozenset({2}))
    return request, {"a": artifact}, {"t": tablet}


def test_gpu_score_matches_cpu_and_seed_is_accepted(gpu):
    request, artifacts, tablets = example()
    # Each search independently verifies its exact integer score against the
    # existing CPU rules, including negative levels, caps, disable and multiplier.
    for seed in range(1, 9):
        initial = gpu.search(request, artifacts, tablets, count=128, steps=0, seed=seed)
        improved = gpu.search(request, artifacts, tablets, count=128, steps=64, seed=seed)
        assert improved["score"] >= initial["score"]
        assert validate_result(request, artifacts, tablets, improved["result"]) == []
    result = solve(request, artifacts, tablets, initial_placements=improved["placements"])
    assert result["solutionStatus"] == "OPTIMAL"
    assert result["primaryObjective"] >= improved["score"][0]
    assert validate_result(request, artifacts, tablets, result) == []


def test_gpu_rejects_unimplemented_rules(gpu):
    request, artifacts, tablets = example()
    with pytest.raises(ValueError, match="ordinary artifacts"):
        gpu.search(replace(request, artifacts=(replace(request.artifacts[0], special_priority=True),)),
                   artifacts, tablets)
    with pytest.raises(ValueError, match="conditional tablets"):
        conditional = replace(tablets["t"], candidates={"1x4": ((0, 0, (), (), (), (), ((1, "ITEM"),)),)})
        gpu.search(request, artifacts, {"t": conditional})


def test_server_hybrid_path_runs_real_gpu(gpu, tmp_path):
    request, artifacts, tablets = example()
    request = replace(request, gpu_acceleration=True)
    state = AppState("test", ResultCache(tmp_path / "cache"))
    state.gpu_search = gpu
    result = state._solve(request, artifacts, tablets, state.jobs.get("unused"))
    assert result["diagnostics"]["gpuRequested"] is True
    assert result["diagnostics"]["gpuUsed"] is True
    assert result["diagnostics"]["gpuEvaluations"] > 0
    assert validate_result(request, artifacts, tablets, result) == []
