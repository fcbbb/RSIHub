from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evolve.evaluation.legacy import effective_task_set_identity
from evolve.evaluation.run_plan import EvaluationRunPlan


def _plan() -> EvaluationRunPlan:
    return EvaluationRunPlan(
        schema_version=1,
        experiment_id="experiment",
        generation="3",
        candidate_commit="a" * 40,
        purpose="candidate",
        canonical=True,
        tasks=("task-a", "task-b"),
        attempts_per_task=2,
        expected_trials=4,
        concurrency=2,
        evaluator_fingerprint="b" * 40,
        task_set_hash="c" * 64,
        runtime_fingerprint="d" * 64,
        execution_runtime_fingerprint="e" * 64,
    )


def test_run_plan_round_trips_as_authoritative_evaluation_input(tmp_path: Path) -> None:
    path = _plan().write(tmp_path / "run-plan.json")

    assert EvaluationRunPlan.read(path) == _plan()
    assert json.loads(path.read_text())["tasks"] == ["task-a", "task-b"]


def test_run_plan_rejects_trial_count_drift(tmp_path: Path) -> None:
    path = replace(_plan(), expected_trials=3).write(tmp_path / "run-plan.json")

    with pytest.raises(ValueError, match="does not match"):
        EvaluationRunPlan.read(path)


def test_task_limit_changes_both_members_and_identity(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    (evaluator / "splits.json").write_text(
        json.dumps(
            {
                "version": 2,
                "resolved": True,
                "identity_status": "verified",
                "dataset_identity": {
                    "source": "local",
                    "digest": "d" * 64,
                    "resolved_reference": "sha256:" + "d" * 64,
                },
                "tasks": {"train": [], "gate": ["task-c", "task-a", "task-b"], "sealed": []},
                "task_digests": {"task-a": "a" * 64, "task-b": "b" * 64, "task-c": "c" * 64},
            }
        )
    )
    config = {"dataset": "local", "k": 2, "evaluation_split": "gate"}

    full = effective_task_set_identity(tmp_path, config)
    limited = effective_task_set_identity(tmp_path, config, task_limit=1)

    assert full.members == ("task-a", "task-b", "task-c")
    assert limited.members == ("task-c",)
    assert limited.digest != full.digest


def test_gate_identity_uses_the_frozen_per_round_limit(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    (evaluator / "splits.json").write_text(
        json.dumps(
            {
                "version": 2,
                "resolved": True,
                "identity_status": "verified",
                "dataset_identity": {
                    "source": "local",
                    "digest": "d" * 64,
                    "resolved_reference": "sha256:" + "d" * 64,
                },
                "sampling": "static",
                "gate_tasks_per_round": 2,
                "tasks": {"train": [], "gate": ["task-c", "task-a", "task-b"], "sealed": []},
                "task_digests": {"task-a": "a" * 64, "task-b": "b" * 64, "task-c": "c" * 64},
            }
        )
    )

    identity = effective_task_set_identity(
        tmp_path,
        {"dataset": "local", "k": 1, "evaluation_split": "gate"},
    )

    assert identity.members == ("task-a", "task-c")


def _plan_for(attempts: int = 1, tasks: tuple[str, ...] = ("task-a", "task-b")) -> EvaluationRunPlan:
    return EvaluationRunPlan(
        schema_version=1,
        experiment_id="experiment",
        generation="3",
        candidate_commit="a" * 40,
        purpose="candidate",
        canonical=True,
        tasks=tasks,
        attempts_per_task=attempts,
        expected_trials=len(tasks) * attempts,
        concurrency=2,
        evaluator_fingerprint="b" * 40,
        task_set_hash="c" * 64,
        runtime_fingerprint="d" * 64,
        execution_runtime_fingerprint="e" * 64,
    )


def _write_attempt(
    workspace: Path,
    plan: EvaluationRunPlan,
    *,
    number: int,
    status: str | None,
    results: tuple[tuple[str, dict[str, object]], ...],
    prior: EvaluationRunPlan | None = None,
) -> Path:
    from evolve.runtime import attempt_parent

    attempt = (
        attempt_parent(
            workspace,
            purpose=plan.purpose,
            generation=plan.generation,
            candidate_commit=plan.candidate_commit,
        )
        / f"attempt-{number}"
    )
    attempt.mkdir(parents=True)
    (prior or plan).write(attempt / "run-plan.json")
    if status is not None:
        (attempt / "status").write_text(status + "\n")
    for name, payload in results:
        trial = attempt / "jobs" / "job" / name
        trial.mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps(payload))
    return attempt


def test_resume_reuses_only_incomplete_matching_attempts(tmp_path: Path) -> None:
    from evolve.evaluation.execution import _resumable_attempt_roots

    plan = _plan_for()
    first = _write_attempt(
        tmp_path,
        plan,
        number=1,
        status="infrastructure_failed",
        results=(("task-a", {"task_name": "task-a", "trial_name": "a"}),),
    )
    _write_attempt(
        tmp_path,
        plan,
        number=2,
        status="complete",
        results=(
            ("task-a", {"task_name": "task-a", "trial_name": "a", "verifier_result": {"rewards": {"reward": 1}}}),
        ),
    )
    _write_attempt(
        tmp_path,
        plan,
        number=3,
        status=None,
        results=(("task-a", {"task_name": "task-a", "trial_name": "a"}),),
        prior=_plan_for(tasks=("task-a",)),
    )

    assert _resumable_attempt_roots(tmp_path, plan) == ((1, first / "jobs"),)


def test_resume_skips_attempts_without_job_evidence(tmp_path: Path) -> None:
    from evolve.evaluation.execution import _resumable_attempt_roots

    plan = _plan_for()
    _write_attempt(tmp_path, plan, number=1, status=None, results=())

    assert _resumable_attempt_roots(tmp_path, plan) == ()
