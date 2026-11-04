"""The two dsh seeds serve different runtime models and must stay separate.

``seeds/dsh`` is the legacy host-SDK target used by ``hyperagents_dsh``
(candidate rollouts go through ``runners/rollout_driver.py`` with a host-side
``docker exec`` bridge, and mutate runs ``runners/mutate_local.py`` locally).
``seeds/dsh-harbor`` is the ``ahe_dsh`` target: a plain Harbor InstalledAgent
that stages the dsh runtime from ``DSH_RUNTIME_IMAGE`` into the task container,
so it must not carry the host-side kit.
"""

from __future__ import annotations

from pathlib import Path

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


def test_recipes_select_their_seed_variant() -> None:
    ahe = yaml.safe_load((ROOT / "recipes" / "ahe_dsh" / "evolve.yaml").read_text())
    hyperagents = yaml.safe_load((ROOT / "recipes" / "hyperagents_dsh" / "evolve.yaml").read_text())
    assert ahe["target"]["seed"] == "builtin-dsh-harbor"
    assert hyperagents["target"]["seed"] == "builtin-dsh"
