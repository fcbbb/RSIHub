from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from harbor.environments.base import ExecResult

from evolve.integrations.harbor.dsh_container import DshContainerAgent


class FakeEnvironment:
    def __init__(self, results: list[ExecResult] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.uploads: list[tuple[str, str]] = []
        self._results = list(results or [])

    def type(self) -> str:
        return "docker"

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        self.uploads.append((str(source_path), target_path))

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        self.calls.append({"command": command, "cwd": cwd, "env": dict(env or {}), "timeout_sec": timeout_sec})
        if self._results:
            return self._results.pop(0)
        return ExecResult(stdout="", stderr="", return_code=0)


def _agent(tmp_path: Path, **kwargs: object) -> DshContainerAgent:
    return DshContainerAgent(
        logs_dir=tmp_path / "logs",
        model_name="openai/gpt-5.4-2026-03-05",
        extra_env={
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "HTTPS_PROXY": "http://proxy.invalid",
        },
        **kwargs,
    )


def test_role_and_model_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported dsh container role"):
        _agent(tmp_path, role="reviewer")

    agent = DshContainerAgent(logs_dir=tmp_path / "logs", role="mutate")
    with pytest.raises(ValueError, match="requires operators"):
        asyncio.run(agent.setup(FakeEnvironment()))


def test_mutate_run_maps_env_and_writes_trajectory(tmp_path: Path) -> None:
    environment = FakeEnvironment(
        [
            ExecResult(
                stdout='{"type":"session","sessionId":"s-1"}\n{"type":"final","text":"changed target"}\n',
                stderr="",
                return_code=0,
            )
        ]
    )
    context = type("Context", (), {"metadata": None})()
    agent = _agent(tmp_path, role="mutate")

    asyncio.run(agent.run("improve the harness", environment, context))

    task_upload = environment.uploads[0]
    assert task_upload[1] == "/tmp/evolve-dsh-task.txt"
    assert Path(task_upload[0]).read_text() == "improve the harness\n"
    call = environment.calls[0]
    assert call["cwd"] == "/app/task/workspace"
    assert "--profile headless" in call["command"]
    assert "--patch /opt/evolve/dsh/headless.mutate.cordis.yml" in call["command"]
    assert call["env"]["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert call["timeout_sec"] == 3300
    assert call["env"]["DSH_MODEL"] == "gpt-5.4-2026-03-05"
    assert call["env"]["DEEPSEEK_API_KEY"] == "test-key"
    assert call["env"]["DEEPSEEK_BASE_URL"] == "https://example.invalid/v1"
    assert call["env"]["HTTPS_PROXY"] == "http://proxy.invalid"
    assert call["env"]["DSH_HOME"] == "/tmp/evolve-dsh-home"

    trajectory = json.loads((tmp_path / "logs" / "trajectory.json").read_text())
    assert trajectory == {"steps": [{"source": "agent", "message": "changed target"}]}
    assert context.metadata["dsh"]["session_id"] == "s-1"


def test_analyst_run_uses_full_access_overlay(tmp_path: Path) -> None:
    environment = FakeEnvironment([ExecResult(stdout='{"type":"final","text":"analysis"}\n', stderr="", return_code=0)])
    agent = _agent(tmp_path, role="analyst")

    asyncio.run(agent.run("analyze evidence", environment, type("Context", (), {"metadata": None})()))

    call = environment.calls[0]
    assert call["cwd"] == "/app/task"
    assert "--patch /opt/evolve/dsh/headless.analyst.cordis.yml" in call["command"]
    assert call["env"]["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert call["timeout_sec"] == 900


def test_run_requires_a_final_event(tmp_path: Path) -> None:
    environment = FakeEnvironment(
        [ExecResult(stdout='{"type":"session","sessionId":"s-1"}\n', stderr="boom", return_code=0)]
    )
    agent = _agent(tmp_path, role="mutate")

    with pytest.raises(RuntimeError, match="no final event"):
        asyncio.run(agent.run("task", environment, type("Context", (), {"metadata": None})()))


def test_setup_validates_overlay_composition(tmp_path: Path) -> None:
    environment = FakeEnvironment()
    agent = _agent(tmp_path, role="analyst")

    asyncio.run(agent.setup(environment))

    assert len(environment.calls) == 2
    assert "dsh --version" in environment.calls[0]["command"]
    assert "--dump-config" in environment.calls[1]["command"]
    assert "headless.analyst.cordis.yml" in environment.calls[1]["command"]


def test_setup_fails_when_overlay_does_not_compose(tmp_path: Path) -> None:
    environment = FakeEnvironment(
        [
            ExecResult(stdout="0.2.1-rc.5\n", stderr="", return_code=0),
            ExecResult(stdout="", stderr="bad yaml", return_code=1),
        ]
    )
    agent = _agent(tmp_path, role="mutate")

    with pytest.raises(RuntimeError, match="does not compose"):
        asyncio.run(agent.setup(environment))
