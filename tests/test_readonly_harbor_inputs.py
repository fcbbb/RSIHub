import importlib
import os
from pathlib import Path

import pytest


def _runner():
    return importlib.import_module("library._shared.runners.harbor")


def test_readonly_runner_copies_workspace_files_with_readonly_permissions(tmp_path: Path) -> None:
    module = _runner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "runs" / "diagnostic.jsonl"
    source.parent.mkdir()
    source.write_text("safe evidence\n")
    destination = tmp_path / "inputs" / "trace.jsonl"
    destination.parent.mkdir()

    module._copy_workspace_input(source, workspace, destination)

    assert destination.read_text() == "safe evidence\n"
    assert destination.stat().st_mode & 0o777 == 0o444


def test_readonly_runner_rejects_input_paths_outside_workspace(tmp_path: Path) -> None:
    module = _runner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "private.txt"
    source.write_text("private")
    destination = tmp_path / "input.txt"

    with pytest.raises(ValueError, match="inside the workspace"):
        module._copy_workspace_input(source, workspace, destination)


def test_readonly_runner_rejects_symlinked_input_paths(tmp_path: Path) -> None:
    module = _runner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real = workspace / "real.txt"
    real.write_text("evidence")
    link = workspace / "linked.txt"
    os.symlink(real, link)

    with pytest.raises(ValueError, match="symlinks"):
        module._copy_workspace_input(link, workspace, tmp_path / "copy.txt")


def test_readonly_runner_rejects_modified_input_digest(tmp_path: Path) -> None:
    module = _runner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "trace.jsonl"
    source.write_text("evidence")
    destination = tmp_path / "copy.jsonl"

    with pytest.raises(ValueError, match="integrity check"):
        module._copy_workspace_input(
            source,
            workspace,
            destination,
            expected_sha256="0" * 64,
        )

    assert not destination.exists()


def test_readonly_runner_rejects_non_basename_input_names() -> None:
    module = _runner()

    with pytest.raises(ValueError, match="one relative filename"):
        module._input_basename("../trace.jsonl")
