#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from glob import escape
from pathlib import Path

from harbor_artifacts import FINAL_TRIAL_STATUSES, _load_task_trials, write_harbor_artifacts


def _load_eval_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def _write_outputs(run_dir: Path, *, status: str, metrics: dict[str, object], score: float | None = None) -> None:
    # Compatibility outputs only: the host derives its EvaluationRecord from task_vector.json.
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status").write_text(f"{status}\n")
    if score is not None:
        (run_dir / "score").write_text(f"{score}\n")
    (run_dir / "metrics.json").write_text(json.dumps({"dimensions": metrics}, indent=2, sort_keys=True) + "\n")


def _attempts(env_values: dict[str, str]) -> int:
    return max(1, int(env_values.get("EVOLVE_HARBOR_ATTEMPTS", "1")))


def _expected_trials(run_dir: Path, env_values: dict[str, str]) -> int:
    """Resolve how many trials this run owed.

    Prefer the host run plan and the limited-run env override over task-split.json.
    A smoke/limited evaluation may leave the full train split recorded while only
    running N planned trials; treating the full split as expected_trials would
    mark a successful limited run as infra_failed.
    """
    attempts = _attempts(env_values)
    plan_path = run_dir / "run-plan.json"
    if plan_path.exists():
        payload = json.loads(plan_path.read_text())
        if isinstance(payload, dict):
            planned = payload.get("expected_trials")
            if isinstance(planned, int) and not isinstance(planned, bool) and planned >= 1:
                return planned
            tasks = payload.get("tasks")
            if isinstance(tasks, list) and tasks:
                return max(1, len(tasks) * attempts)
    for raw in (
        os.environ.get("EVOLVE_HARBOR_EXPECTED_TRIALS"),
        env_values.get("EVOLVE_HARBOR_EXPECTED_TRIALS"),
        env_values.get("EVOLVE_HARBOR_N"),
    ):
        if raw is not None and str(raw).strip():
            return max(1, int(raw))
    selection = run_dir / "task-split.json"
    if selection.exists():
        payload = json.loads(selection.read_text())
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if isinstance(tasks, list) and tasks:
            return max(1, len(tasks) * attempts)
    return 1


def _resume_roots(primary: Path) -> list[Path]:
    """Prior-attempt job roots followed by this invocation's jobs directory."""
    roots = [
        Path(entry)
        for entry in os.environ.get("EVOLVE_RESUME_JOBS_ROOTS", "").split(os.pathsep)
        if entry.strip()
    ]
    if primary not in roots:
        roots.append(primary)
    return roots


def _task_identity(task_name: str, expected: set[str]) -> str:
    """Map a Harbor-qualified task name back to its frozen dataset identity."""
    if task_name in expected:
        return task_name
    matches = [
        name for name in expected if task_name.endswith(f"__{name}") or task_name.endswith(f"/{name}")
    ]
    return matches[0] if len(matches) == 1 else task_name


def _missing_tasks(argv: list[str]) -> int:
    if len(argv) != 4:
        raise SystemExit("usage: parse_score.py --missing-tasks <planned_tasks> <output>")
    planned_path = Path(argv[2])
    output_path = Path(argv[3])
    planned = [
        line.strip()
        for line in planned_path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    env_values = _load_eval_env(Path("evaluator") / "eval.env")
    attempts = max(1, int(env_values.get("EVOLVE_HARBOR_ATTEMPTS", "1")))
    reward_schema = env_values.get("EVOLVE_REWARD_SCHEMA", "scalar")
    roots = [
        Path(entry)
        for entry in os.environ.get("EVOLVE_RESUME_JOBS_ROOTS", "").split(os.pathsep)
        if entry.strip()
    ]
    expected = set(planned)
    counts: dict[str, int] = {}
    for trial in _load_task_trials(roots, reward_schema=reward_schema):
        if trial["status"] not in FINAL_TRIAL_STATUSES:
            continue
        identity = _task_identity(str(trial["task_name"]), expected)
        counts[identity] = counts.get(identity, 0) + 1
    missing = [task for task in planned if counts.get(task, 0) < attempts]
    output_path.write_text("".join(f"{escape(task)}\n" for task in missing))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--missing-tasks":
        return _missing_tasks(argv)
    if len(argv) != 4:
        raise SystemExit("usage: parse_score.py <jobs_dir> <run_dir> <harbor_rc>")
    jobs_dir = Path(argv[1])
    run_dir = Path(argv[2])
    harbor_rc = int(argv[3])
    env_values = _load_eval_env(Path("evaluator") / "eval.env")
    reward_schema = env_values.get("EVOLVE_REWARD_SCHEMA", "scalar")
    expected_trials = _expected_trials(run_dir, env_values)
    jobs_roots = _resume_roots(jobs_dir)
    rewards = write_harbor_artifacts(jobs_roots, run_dir, reward_schema=reward_schema)
    if not rewards and reward_schema == "scalar":
        for jobs_root in jobs_roots:
            for reward_path in sorted(jobs_root.rglob("verifier/reward.txt")) if jobs_root.exists() else []:
                try:
                    rewards.append(float(reward_path.read_text().strip()))
                except (OSError, ValueError):
                    continue
    completed_trials = len(rewards)
    missing_trials = max(expected_trials - completed_trials, 0)
    if completed_trials == 0:
        _write_outputs(
            run_dir,
            status="infra_failed",
            metrics={
                "completed_trials": 0,
                "expected_trials": expected_trials,
                "missing_trials": expected_trials,
                "pass_rate": 0.0,
                "harbor_rc": harbor_rc,
            },
        )
        return 3

    score = sum(rewards) / completed_trials
    metrics = {
        "completed_trials": completed_trials,
        "expected_trials": expected_trials,
        "missing_trials": missing_trials,
        "pass_rate": score,
        "harbor_rc": harbor_rc,
    }
    if completed_trials < expected_trials:
        _write_outputs(run_dir, status="infra_failed", metrics=metrics)
        return 3

    _write_outputs(run_dir, status="complete", metrics=metrics, score=score)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
