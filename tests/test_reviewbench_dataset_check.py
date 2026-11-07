from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reviewbench" / "check_dataset.py"

TASK = """\
[agent]
network_mode = "allowlist"

[verifier]
network_mode = "allowlist"

[verifier.env]
OPENAI_API_KEY = "${RB_HARBOR_OPENAI_API_KEY}"
PI_CODING_AGENT_DIR = "/tests/judge/.reviewbench-models"
"""


def _dataset(tmp_path: Path, tasks: int = 1, **task_options: object) -> Path:
    root = tmp_path / "dataset"
    root.mkdir()
    entries = []
    for index in range(tasks):
        name = f"task-{index}"
        entries.append(f'[[tasks]]\nname = "{name}"\n')
        task = root / name
        task.mkdir()
        (task / "task.toml").write_text(TASK)
        if task_options.get("registry", True):
            registry = task / "tests" / "judge" / ".reviewbench-models"
            registry.mkdir(parents=True)
            (registry / "models.json").write_text('{"providers": {"openai": {"models": [{"id": "judge"}]}}}\n')
    (root / "dataset.toml").write_text('schema_version = "1.0"\n\n[dataset]\nname = "fixture"\n\n' + "\n".join(entries))
    return root


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_valid_dataset_passes(tmp_path: Path) -> None:
    result = _run(_dataset(tmp_path, tasks=2))

    assert result.returncode == 0, result.stderr
    assert "tasks:   2" in result.stdout
    assert "ok: dataset contract looks usable" in result.stdout


def test_missing_manifest_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()

    result = _run(tmp_path / "empty")

    assert result.returncode == 2
    assert "missing dataset.toml" in result.stderr


def test_missing_judge_registry_is_an_error(tmp_path: Path) -> None:
    result = _run(_dataset(tmp_path, registry=False))

    assert result.returncode == 2
    assert "missing tests/judge/.reviewbench-models/models.json" in result.stderr


def test_missing_verifier_key_is_an_error(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    task_toml = root / "task-0" / "task.toml"
    task_toml.write_text(task_toml.read_text().replace("${RB_HARBOR_OPENAI_API_KEY}", "static-key"))

    result = _run(root)

    assert result.returncode == 2
    assert "verifier.env does not provide RB_HARBOR_OPENAI_API_KEY" in result.stderr


def test_min_task_count_is_enforced(tmp_path: Path) -> None:
    result = _run(_dataset(tmp_path), "--min-tasks", "2")

    assert result.returncode == 2
    assert "found 1 tasks, need at least 2" in result.stderr


def test_non_allowlist_network_is_only_a_warning(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    task_toml = root / "task-0" / "task.toml"
    task_toml.write_text(task_toml.read_text().replace('"allowlist"', '"bridge"', 1))

    result = _run(root)

    assert result.returncode == 0, result.stderr
    assert "agent.network_mode is 'bridge'" in result.stderr
    assert "ok: dataset contract looks usable" in result.stdout


def test_baked_agent_host_is_reported_as_a_warning(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    task_toml = root / "task-0" / "task.toml"
    task_toml.write_text(
        task_toml.read_text().replace(
            'network_mode = "allowlist"\n\n[verifier]',
            'network_mode = "allowlist"\nallowed_hosts = ["model.example"]\n\n[verifier]',
            1,
        )
    )

    result = _run(root)

    assert result.returncode == 0, result.stderr
    assert "agent.allowed_hosts is not empty" in result.stderr
    assert "ok: dataset contract looks usable" in result.stdout
