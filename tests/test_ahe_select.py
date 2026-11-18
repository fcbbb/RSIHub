import importlib.util
import random
from pathlib import Path
from types import SimpleNamespace

from evolve.frozen.interfaces import OperatorContext

ROOT = Path(__file__).resolve().parents[1]


def test_ahe_latest_selects_newest_valid_parent_even_when_score_is_lower(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("ahe_latest_test", ROOT / "library/select/ahe_latest.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = SimpleNamespace(valid_parents=lambda: [{"genid": "1", "score": 0.9}, {"genid": "2", "score": 0.1}])
    ctx = OperatorContext(tmp_path, tmp_path, tmp_path, "3", "2", None, 1, {}, random.Random(0))
    assert module.AheLatestSelect().pick(archive, ctx).parents == ["2"]


def _ahe_parent(genid: str, rewards: list[float], *, task_hash: str = "tasks") -> dict:
    tasks = {
        f"task-{index}": {"trials": [{"status": "benchmark_complete", "owner": "benchmark", "reward": reward}]}
        for index, reward in enumerate(rewards)
    }
    return {
        "genid": genid,
        "score": sum(rewards) / len(rewards),
        "task_set_hash": task_hash,
        "evaluator_fingerprint": "eval-v1",
        "selection_eligible": True,
        "task_vector": {"schema_version": 1, "tasks": tasks},
    }


def _ahe_champion_module():
    spec = importlib.util.spec_from_file_location("ahe_champion_test", ROOT / "library/select/ahe_champion.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ahe_champion_keeps_incumbent_when_paired_interval_includes_zero(tmp_path: Path) -> None:
    module = _ahe_champion_module()
    parents = [_ahe_parent("0", [1.0] * 5 + [0.0] * 5), _ahe_parent("1", [1.0] * 5 + [1.0] + [0.0] * 4)]
    archive = SimpleNamespace(valid_parents=lambda: parents)
    ctx = OperatorContext(tmp_path, tmp_path, tmp_path, "2", "1", None, 2, {}, random.Random(0))

    result = module.AheChampionSelect().pick(archive, ctx)

    assert result.parents == ["0"]
    comparison = result.decision["comparisons"][0]
    assert comparison["transitions"] == {"P2P": 5, "F2P": 1, "P2F": 0, "F2F": 4}
    assert comparison["confidence_interval_95"][0] <= 0


def test_ahe_champion_promotes_clear_paired_improvement_and_records_decision(tmp_path: Path) -> None:
    module = _ahe_champion_module()
    parents = [_ahe_parent("0", [1.0] * 4 + [0.0] * 8), _ahe_parent("1-0", [1.0] * 12)]
    archive = SimpleNamespace(valid_parents=lambda: parents)
    ctx = OperatorContext(tmp_path, tmp_path, tmp_path, "2", "1-0", None, 2, {}, random.Random(0))

    result = module.AheChampionSelect().pick(archive, ctx)

    assert result.parents == ["1-0"]
    assert result.decision["champion"] == "1-0"
    comparison = result.decision["comparisons"][0]
    assert comparison["transitions"] == {"P2P": 4, "F2P": 8, "P2F": 0, "F2F": 0}
    assert comparison["confidence_interval_95"][0] > 0


def test_ahe_champion_does_not_compare_different_task_manifests(tmp_path: Path) -> None:
    module = _ahe_champion_module()
    parents = [_ahe_parent("0", [0.0] * 8), _ahe_parent("1", [1.0] * 8, task_hash="other")]
    archive = SimpleNamespace(valid_parents=lambda: parents)
    ctx = OperatorContext(tmp_path, tmp_path, tmp_path, "2", "1", None, 1, {}, random.Random(0))

    result = module.AheChampionSelect().pick(archive, ctx)

    assert result.parents == ["0"]
    assert result.decision["comparisons"][0]["reason"] == "task evidence is incomplete or incomparable"


def test_ahe_champion_uses_equal_weighted_multibench_paired_deltas(
    tmp_path: Path,
) -> None:
    module = _ahe_champion_module()
    rewards = {
        "deep_swe__task-a": (0.0, 1.0),
        "deep_swe__task-b": (0.0, 1.0),
        "reviewbench__repo-pr": (0.0, 0.0),
        "swe_atlas__task-c": (0.0, 0.0),
    }

    def parent(genid: str, reward_index: int) -> dict:
        values = {task: pair[reward_index] for task, pair in rewards.items()}
        return {
            "genid": genid,
            "score": sum(values.values()) / len(values),
            "task_set_hash": "multibench",
            "evaluator_fingerprint": "eval-v1",
            "selection_eligible": True,
            "score_components": {"deep_swe": 0.0, "reviewbench": 0.0, "swe_atlas": 0.0},
            "task_vector": {
                "schema_version": 1,
                "tasks": {
                    task: {"trials": [{"status": "benchmark_complete", "owner": "benchmark", "reward": reward}]}
                    for task, reward in values.items()
                },
            },
        }

    archive = SimpleNamespace(valid_parents=lambda: [parent("0", 0), parent("1", 1)])
    ctx = OperatorContext(tmp_path, tmp_path, tmp_path, "2", "1", None, 2, {}, random.Random(0))

    result = module.AheChampionSelect().pick(archive, ctx)

    assert result.parents == ["1"]
    comparison = result.decision["comparisons"][0]
    assert comparison["score_aggregation"] == "equal_source_macro"
    assert comparison["task_count"] == 4
    assert abs(comparison["delta"] - (1 / 3)) < 1e-9
    assert comparison["transitions"] == {"P2P": 0, "F2P": 2, "P2F": 0, "F2F": 2}
