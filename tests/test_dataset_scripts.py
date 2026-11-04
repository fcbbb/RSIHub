"""Generic dataset download and the demo launcher's DATASET_PATH escape hatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FETCH = ROOT / "scripts" / "fetch_dataset.sh"
DEMO = ROOT / "scripts" / "run_recipe_demo.sh"

FAKE_TOOL = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
log = Path(os.environ["SETUP_CALLS"])
with log.open("a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")

if name != "uv":
    raise SystemExit(2)
if args[:2] == ["sync", "--frozen"]:
    raise SystemExit(0)
command = args[2:] if args[:2] == ["run", "--frozen"] else []
if command[:2] == ["harbor", "download"]:
    output = Path(command[command.index("-o") + 1])
    layout = os.environ.get("FAKE_EXPORT_LAYOUT", "nested")
    task = output / ("terminal-bench" if layout == "nested" else "") / "task-a"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("version = \"1.0\"\n")
    raise SystemExit(0)
raise SystemExit(0)
"""


def _environment(tmp_path: Path, **values: str) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tool = fake_bin / "tool"
    tool.write_text(FAKE_TOOL.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    tool.chmod(0o755)
    (fake_bin / "uv").symlink_to(tool)
    calls = tmp_path / "calls.jsonl"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "SETUP_CALLS": str(calls),
            **values,
        }
    )
    return environment, calls


def _calls(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _run(script: Path, args: list[str], *, cwd: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args], cwd=cwd, env=environment, text=True, capture_output=True, check=False
    )


def test_fetch_flattens_a_nested_export_and_names_it_after_the_dataset(tmp_path: Path) -> None:
    environment, calls_path = _environment(tmp_path, EVOLVE_ASSET_ROOT=str(tmp_path / "shared"))

    result = _run(FETCH, ["terminal-bench@2.0"], cwd=ROOT, environment=environment)

    assert result.returncode == 0, result.stderr
    target = tmp_path / "shared" / "terminal-bench-2.0"
    assert (target / "task-a" / "task.toml").is_file()
    assert not (target / "terminal-bench").exists()
    assert f"Use: --dataset {target}" in result.stdout
    download = next(call for call in _calls(calls_path) if call[:5] == ["uv", "run", "--frozen", "harbor", "download"])
    assert download[5] == "terminal-bench@2.0"
    assert Path(download[download.index("-o") + 1]).name.startswith(".terminal-bench-2.0.pending-")
    assert not list((tmp_path / "shared").glob(".fetch-*"))


def test_fetch_reuses_an_existing_dataset_without_downloading(tmp_path: Path) -> None:
    environment, calls_path = _environment(tmp_path, EVOLVE_ASSET_ROOT=str(tmp_path / "shared"))
    task = tmp_path / "shared" / "bench" / "task-a"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('version = "1.0"\n')

    result = _run(FETCH, ["bench@1.0", "bench"], cwd=ROOT, environment=environment)

    assert result.returncode == 0, result.stderr
    assert "Reused existing dataset" in result.stdout
    assert not any(call[:5] == ["uv", "run", "--frozen", "harbor", "download"] for call in _calls(calls_path))


def test_fetch_accepts_direct_exports_and_resolves_a_relative_root(tmp_path: Path) -> None:
    caller = tmp_path / "caller"
    caller.mkdir()
    environment, _calls_path = _environment(tmp_path, EVOLVE_ASSET_ROOT="assets", FAKE_EXPORT_LAYOUT="direct")

    result = _run(FETCH, ["swe-bench-verified@1.0", "swe"], cwd=caller, environment=environment)

    assert result.returncode == 0, result.stderr
    assert (caller / "assets" / "swe" / "task-a" / "task.toml").is_file()


def test_fetch_reports_an_export_without_harbor_tasks(tmp_path: Path) -> None:
    environment, _calls_path = _environment(
        tmp_path, EVOLVE_ASSET_ROOT=str(tmp_path / "shared"), FAKE_EXPORT_LAYOUT="direct"
    )
    # An empty export means the registry name or version is wrong.
    tool = tmp_path / "bin" / "uv"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)

    result = _run(FETCH, ["missing@9.9"], cwd=ROOT, environment=environment)

    assert result.returncode != 0
    assert "no Harbor tasks" in result.stderr
    assert not (tmp_path / "shared" / "missing-9.9").exists()
    assert not list((tmp_path / "shared").glob(".fetch-*"))


def test_fetch_rejects_a_name_that_is_not_a_directory(tmp_path: Path) -> None:
    environment, calls_path = _environment(tmp_path, EVOLVE_ASSET_ROOT=str(tmp_path / "shared"))

    result = _run(FETCH, ["bench@1.0", "nested/name"], cwd=ROOT, environment=environment)

    assert result.returncode != 0
    assert "directory name" in result.stderr
    assert _calls(calls_path) == []


def test_demo_uses_dataset_path_and_skips_the_terminal_bench_subset(tmp_path: Path) -> None:
    dataset = tmp_path / "other-dataset" / "task-a"
    dataset.mkdir(parents=True)
    (dataset / "task.toml").write_text('version = "1.0"\n')
    environment, calls_path = _environment(
        tmp_path,
        DATASET_PATH=str(dataset.parent),
        EVOLVE_ASSET_DIR=str(tmp_path / "missing-assets"),
        ENV_FILE=str(tmp_path / "missing.env"),
        WORKSPACE=str(tmp_path / "workspace"),
    )

    result = _run(DEMO, ["ahe_dsh"], cwd=ROOT, environment=environment)

    assert result.returncode == 0, result.stderr
    calls = _calls(calls_path)
    assert not any("prepare_dataset.py" in call for call in calls)
    init = next(call for call in calls if "init" in call)
    assert init[init.index("--dataset") + 1] == str(dataset.parent)


def test_demo_still_requires_the_pinned_subset_without_dataset_path(tmp_path: Path) -> None:
    environment, calls_path = _environment(
        tmp_path, EVOLVE_ASSET_DIR=str(tmp_path / "missing-assets"), WORKSPACE=str(tmp_path / "workspace")
    )

    result = _run(DEMO, ["ahe"], cwd=ROOT, environment=environment)

    assert result.returncode != 0
    assert "Terminal-Bench assets are missing" in result.stderr
    assert _calls(calls_path) == []


def test_dataset_scripts_are_portable_bash() -> None:
    for script in (FETCH, DEMO):
        result = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
