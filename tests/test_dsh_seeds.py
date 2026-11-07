"""The two dsh seeds serve different runtime models and must stay separate.

``seeds/dsh`` is the legacy host-SDK target used by ``hyperagents_dsh``
(candidate rollouts go through ``runners/rollout_driver.py`` with a host-side
``docker exec`` bridge, and mutate runs ``runners/mutate_local.py`` locally).
``seeds/dsh-harbor`` is the ``ahe_dsh`` target: a plain Harbor InstalledAgent
that stages the dsh runtime from ``DSH_RUNTIME_IMAGE`` into the task container,
so it must not carry the host-side kit.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "seeds" / "dsh"
HARBOR = ROOT / "seeds" / "dsh-harbor"

# Host-side pieces of the legacy target: mutate runs on the host and rollouts
# drive dsh through a docker-exec shell bridge.
LEGACY_ONLY = {
    "runners/mutate_local.py",
    "runners/mutate_driver.py",
    "runners/rollout_driver.py",
    "runners/runtime_mode.py",
    "runners/compositions/mutate.cordis.yml",
}


def _files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }


def test_legacy_seed_keeps_the_host_side_kit() -> None:
    legacy = _files(LEGACY)
    assert LEGACY_ONLY <= legacy
    assert {"agent.py", "profile.cordis.yml"} <= legacy


def test_harbor_seed_carries_no_host_mutator() -> None:
    harbor = _files(HARBOR)
    assert harbor
    for relative in LEGACY_ONLY:
        assert not (HARBOR / relative).exists(), relative


def test_harbor_seed_stages_the_runtime_image() -> None:
    agent = (HARBOR / "agent.py").read_text()
    assert "DSH_RUNTIME_IMAGE" in agent
    assert "sdk_minimal_run.mjs" in agent
    # No docker-exec bridge: the candidate itself runs inside the container.
    assert "docker exec" not in agent


def _load_harbor_agent():
    spec = importlib.util.spec_from_file_location("dsh_harbor_agent_under_test", HARBOR / "agent.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeEnvironment:
    """Stub container used to drive ``DshAgent._ensure_node`` in-process."""

    def __init__(self, probe: str, readiness: list[bool], version: str = "22.9.0") -> None:
        self.probe = probe
        self.readiness = list(readiness)
        self.version = version
        self.uploads: list[tuple[str, str]] = []
        self.root_commands: list[str] = []

    async def exec(self, command: str, **_: object) -> SimpleNamespace:
        if command == self.probe:
            ready = self.readiness.pop(0) if self.readiness else False
            value = "true" if ready else "undefined"
            return SimpleNamespace(return_code=0, stdout=value, stderr="")
        assert command == "node --version"
        return SimpleNamespace(return_code=0, stdout=f"v{self.version}\n", stderr="")

    async def upload_file(self, source: str, destination: str) -> None:
        self.uploads.append((source, destination))


def _harbor_agent(tmp_path: Path, runtime_node: bool = True):
    module = _load_harbor_agent()
    agent = module.DshAgent(logs_dir=tmp_path / "logs", model_name="deepseek-v4-flash", extra_env={})

    async def exec_as_root(_environment, command: str, **_kwargs: object) -> None:
        _environment.root_commands.append(command)

    agent.exec_as_root = exec_as_root
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    if runtime_node:
        staged = runtime_dir / "node"
        staged.write_text("#!/bin/sh\necho v22.23.2\n")
        staged.chmod(0o755)
    return module, agent, runtime_dir


def test_ensure_node_keeps_a_node_that_supports_the_dsh_entry_point(tmp_path: Path) -> None:
    module, agent, runtime_dir = _harbor_agent(tmp_path)
    environment = _FakeEnvironment(module.NODE_FEATURE_PROBE, [True], version="22.19.0")
    asyncio.run(agent._ensure_node(environment, "", None, str(runtime_dir)))
    assert environment.uploads == []
    assert environment.root_commands == []


def test_ensure_node_stages_node_when_the_container_cannot_run_dsh(tmp_path: Path) -> None:
    # dsh's lib/bin.js silently no-ops without import.meta.main, so the
    # container's Node must be replaced rather than trusted by version string.
    module, agent, runtime_dir = _harbor_agent(tmp_path)
    environment = _FakeEnvironment(module.NODE_FEATURE_PROBE, [False, True])
    asyncio.run(agent._ensure_node(environment, "", None, str(runtime_dir)))
    assert environment.uploads == [
        (str(runtime_dir / "node"), "/tmp/evolve-node"),
    ]
    assert environment.root_commands == ["install -m 755 /tmp/evolve-node /usr/local/bin/node"]


def test_ensure_node_rejects_a_staged_node_that_still_cannot_run_dsh(tmp_path: Path) -> None:
    module, agent, runtime_dir = _harbor_agent(tmp_path)
    environment = _FakeEnvironment(module.NODE_FEATURE_PROBE, [False, False], version="22.9.0")
    with pytest.raises(RuntimeError, match="cannot run dsh"):
        asyncio.run(agent._ensure_node(environment, "", None, str(runtime_dir)))


def test_recipes_select_their_seed_variant() -> None:
    ahe = yaml.safe_load((ROOT / "recipes" / "ahe_dsh" / "evolve.yaml").read_text())
    hyperagents = yaml.safe_load((ROOT / "recipes" / "hyperagents_dsh" / "evolve.yaml").read_text())
    assert ahe["target"]["seed"] == "builtin-dsh-harbor"
    assert hyperagents["target"]["seed"] == "builtin-dsh"
