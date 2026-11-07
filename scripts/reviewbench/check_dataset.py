#!/usr/bin/env python3
"""Validate an exported ReviewBench Harbor dataset before a run.

Checks the local-dataset contract evolve freezes at `evolve init`:

  * dataset.toml exists, parses, and lists at least one task;
  * every task has a task.toml and the judge model registry;
  * the verifier receives RB_HARBOR_OPENAI_API_KEY (directly or through a
    ${...} template);
  * the agent/verifier keep an allowlist network mode (warning only);
  * the agent allowlist carries no baked-in deployment host (warning only),
    because the recipe injects the model host per run.

Exit status is 0 when the dataset is usable, 2 when an error was found.
Warnings are printed but do not fail.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError as error:  # Python < 3.11, i.e. not the project env
    raise SystemExit(
        "check_dataset.py requires Python 3.11+ (tomllib); run it through "
        "scripts/reviewbench/check_dataset.sh or `uv run --frozen python`."
    ) from error

JUDGE_REGISTRY = Path("tests/judge/.reviewbench-models/models.json")
KEY_VAR = "RB_HARBOR_OPENAI_API_KEY"


def task_directory(root: Path, name: str) -> Path:
    return root / name.rsplit("/", 1)[-1]


def check_models(path: Path, errors: list[str]) -> None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"{path}: cannot read judge registry: {error}")
        return
    providers = payload.get("providers")
    if not isinstance(providers, dict) or not providers:
        errors.append(f"{path}: expected a non-empty 'providers' object")
        return
    for provider, config in providers.items():
        models = config.get("models") if isinstance(config, dict) else None
        if not isinstance(models, list) or not models:
            errors.append(f"{path}: provider {provider!r} has no models")


def check_task(root: Path, name: str, errors: list[str], warnings: list[str]) -> None:
    directory = task_directory(root, name)
    manifest = directory / "task.toml"
    if not manifest.is_file():
        errors.append(f"{name}: missing {manifest.relative_to(root)}")
        return
    try:
        task = tomllib.loads(manifest.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"{manifest}: cannot parse task.toml: {error}")
        return

    verifier = task.get("verifier") if isinstance(task.get("verifier"), dict) else {}
    env = verifier.get("env") if isinstance(verifier.get("env"), dict) else {}
    injected = KEY_VAR in env or any(KEY_VAR in str(value) for value in env.values())
    if not injected:
        errors.append(f"{name}: verifier.env does not provide {KEY_VAR}")
    if not env.get("PI_CODING_AGENT_DIR"):
        warnings.append(f"{name}: verifier.env has no PI_CODING_AGENT_DIR for the judge registry")

    for role in ("agent", "verifier"):
        section = task.get(role)
        mode = section.get("network_mode") if isinstance(section, dict) else None
        if mode != "allowlist":
            warnings.append(f"{name}: {role}.network_mode is {mode!r}, expected 'allowlist'")
    agent = task.get("agent") if isinstance(task.get("agent"), dict) else {}
    if agent.get("allowed_hosts"):
        warnings.append(
            f"{name}: agent.allowed_hosts is not empty; the model host should be "
            "injected per run by evaluator.agent_allowed_hosts"
        )

    registry = directory / JUDGE_REGISTRY
    if not registry.is_file():
        errors.append(f"{name}: missing {JUDGE_REGISTRY}")
    else:
        check_models(registry, errors)


def check_dataset(root: Path, min_tasks: int) -> tuple[list[str], list[str], int]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest = root / "dataset.toml"
    if not manifest.is_file():
        return [f"not a local dataset (missing dataset.toml): {root}"], warnings, 0
    try:
        data = tomllib.loads(manifest.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        return [f"{manifest}: cannot parse dataset.toml: {error}"], warnings, 0

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return [f"{manifest}: no [[tasks]] entries"], warnings, 0
    names: list[str] = []
    seen: set[str] = set()
    for entry in tasks:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            errors.append(f"{manifest}: invalid task entry {entry!r}")
            continue
        if name in seen:
            errors.append(f"{manifest}: duplicate task name {name!r}")
            continue
        seen.add(name)
        names.append(name)
        check_task(root, name, errors, warnings)

    if len(names) < min_tasks:
        errors.append(f"{manifest}: found {len(names)} tasks, need at least {min_tasks}")
    return errors, warnings, len(names)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="local dataset directory containing dataset.toml")
    parser.add_argument("--min-tasks", type=int, default=1, help="minimum task count (default: 1)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args()

    root = args.dataset.expanduser().resolve()
    errors, warnings, count = check_dataset(root, max(1, args.min_tasks))
    if args.json:
        print(json.dumps({"dataset": str(root), "tasks": count, "errors": errors, "warnings": warnings}, indent=2))
    else:
        print(f"dataset: {root}")
        print(f"tasks:   {count}")
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        if not errors:
            print("ok: dataset contract looks usable")
    raise SystemExit(2 if errors else 0)


if __name__ == "__main__":
    main()
