from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "scripts" / "reviewbench" / "patch_sidecar_dns.py"
PATCH_TASK = ROOT / "scripts" / "reviewbench" / "patch_task.py"

TASK = """\
schema_version = "1.3"

[agent]
timeout_sec = 900
network_mode = "allowlist"
allowed_hosts = ["model.example"]

[verifier]
environment_mode = "separate"
network_mode = "allowlist"
allowed_hosts = ["model.example"]

[verifier.env]
OPENAI_API_KEY = "${RB_HARBOR_OPENAI_API_KEY}"

[verifier.environment]
network_mode = "allowlist"
allowed_hosts = ["model.example"]
"""

POLICY = """\
#!/bin/sh
setup_nftables() {
    fib daddr type local return
    fib daddr type local accept
}
"""


def test_sidecar_dns_patch_is_idempotent(tmp_path: Path) -> None:
    policy = tmp_path / "network-policy"
    policy.write_text(POLICY)

    first = subprocess.run(
        [sys.executable, str(SIDECAR), str(policy), "1.1.1.1"],
        check=False,
        capture_output=True,
        text=True,
    )
    patched = policy.read_text()
    second = subprocess.run(
        [sys.executable, str(SIDECAR), str(policy), "1.1.1.1"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert "rsihub local DNS allowance" in patched
    assert "for ns in 127.0.0.11 1.1.1.1; do" in patched
    assert "$(nft_dns_rules return)" in patched
    assert "$(nft_dns_rules accept)" in patched
    assert policy.read_text() == patched
    assert "already patched" in second.stdout


def test_patch_task_strips_the_agent_host_and_keeps_the_judge_host(tmp_path: Path) -> None:
    task = tmp_path / "task-0"
    task.mkdir()
    (task / "task.toml").write_text(TASK)

    first = subprocess.run(
        [sys.executable, str(PATCH_TASK), str(task), "openai", "judge-model", "https://judge.example/v1"],
        check=False,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        [sys.executable, str(PATCH_TASK), str(task), "openai", "judge-model", "https://judge.example/v1"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    payload = tomllib.loads((task / "task.toml").read_text())
    assert payload["agent"]["network_mode"] == "allowlist"
    assert payload["agent"]["allowed_hosts"] == []
    assert payload["verifier"]["allowed_hosts"] == ["model.example"]
    assert payload["verifier"]["env"]["PI_CODING_AGENT_DIR"] == "/tests/judge/.reviewbench-models"
    registry = task / "tests" / "judge" / ".reviewbench-models" / "models.json"
    assert registry.is_file()
