"""The dual-libc Node payload and its dispatcher.

Benchmark task images mix Debian/Ubuntu (glibc) with Alpine (musl), and a Node
built for one C library cannot boot on the other. The runtime tree therefore
ships both builds under ``dsh/node-bin``; ``dispatch`` picks the usable one at
run time. These tests drive the dispatcher directly with fake binaries so the
selection logic is covered without building the image (the image itself is
probed by ``evaluator/prepare-runtime.sh`` and ``evaluator/doctor_node_matrix.sh``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "containers" / "mutate-dsh" / "node-dispatch.sh"
MATRIX = ROOT / "recipes" / "ahe_dsh" / "evaluator" / "doctor_node_matrix.sh"


def _fake_node(path: Path, label: str, usable: bool = True) -> None:
    """A stand-in Node: answers ``-e ''`` (the dispatcher's usability probe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f'#!/bin/sh\ncase "$1" in --version) echo {label} ;; *) exit {0 if usable else 1} ;; esac\n'
    path.write_text(body)
    path.chmod(0o755)


def _run_dispatch(base: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(DISPATCH), "--version"],
        env={**os.environ, "DSH_NODE_BIN_DIR": str(base)},
        capture_output=True,
        text=True,
    )


def test_dispatch_prefers_the_glibc_build_on_a_glibc_host(tmp_path: Path) -> None:
    # The test host is glibc; the dispatcher must exec node-glibc without
    # requiring musl's LD_LIBRARY_PATH.
    _fake_node(tmp_path / "node-glibc", "glibc-node")
    _fake_node(tmp_path / "musl-node" / "node", "musl-node")
    result = _run_dispatch(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "glibc-node"


def test_dispatch_falls_back_to_the_other_libc_when_the_preferred_one_cannot_start(
    tmp_path: Path,
) -> None:
    # An image with both loaders (Alpine + gcompat) is judged by what actually
    # starts, not by which loader files exist.
    _fake_node(tmp_path / "node-glibc", "glibc-node", usable=False)
    _fake_node(tmp_path / "musl-node" / "node", "musl-node")
    result = _run_dispatch(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "musl-node"


def test_dispatch_explains_an_unusable_payload(tmp_path: Path) -> None:
    _fake_node(tmp_path / "node-glibc", "glibc-node", usable=False)
    _fake_node(tmp_path / "musl-node" / "node", "musl-node", usable=False)
    result = _run_dispatch(tmp_path)
    assert result.returncode == 127
    assert "no Node build" in result.stderr
    assert "musl-node" in result.stderr


def test_dispatch_falls_back_to_its_own_directory_outside_a_task_container(tmp_path: Path) -> None:
    # In a task container the tree sits at the fixed runtime root; when the
    # dispatcher is run straight out of the image or an extracted tree, its
    # siblings are next to it and DSH_NODE_BIN_DIR is unset.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "dispatch"
    shutil.copy(DISPATCH, script)
    _fake_node(bin_dir / "node-glibc", "glibc-node")
    environment = {k: v for k, v in os.environ.items() if k != "DSH_NODE_BIN_DIR"}
    result = subprocess.run(["sh", str(script), "--version"], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "glibc-node"


def test_doctor_matrix_is_a_runnable_shell_script() -> None:
    assert MATRIX.is_file()
    assert os.access(MATRIX, os.X_OK)
    if not shutil.which("sh"):
        return
    result = subprocess.run(["sh", "-n", str(MATRIX)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
