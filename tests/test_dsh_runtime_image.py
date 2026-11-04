"""Keep the pruned dsh runtime image honest about what it removes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "containers" / "mutate-dsh" / "Dockerfile"
PRUNE = ROOT / "containers" / "mutate-dsh" / "prune-runtime.sh"


def _instructions(text: str) -> list[str]:
    """Join Dockerfile line continuations into one string per instruction."""
    instructions: list[str] = []
    buffer: list[str] = []
    for line in text.splitlines():
        stripped = line.rstrip()
        if not buffer and (not stripped or stripped.lstrip().startswith("#")):
            continue
        buffer.append(stripped[:-1] if stripped.endswith("\\") else stripped)
        if not stripped.endswith("\\"):
            instructions.append(" ".join(buffer))
            buffer = []
    return instructions


def test_prune_runs_in_the_same_layer_as_the_install() -> None:
    runs = [step for step in _instructions(DOCKERFILE.read_text()) if step.startswith("RUN ")]
    install = [step for step in runs if "npm install --global" in step]
    assert len(install) == 1
    # A separate RUN would leave the removed bytes in the image layer.
    assert "bash /tmp/prune-runtime.sh" in install[0]
    assert sum("prune-runtime.sh" in step for step in runs) == 1


def test_prune_keeps_the_candidate_package_menu() -> None:
    script = PRUNE.read_text()
    # Unusable in a headless task container: desktop/web UI, inspector, and the
    # optional Office/speech engines (wrappers stay resolvable).
    for pattern in (
        "dsh-client-*",
        "dsh-web-frontend",
        "dsh-experimental-*inspector*",
        "libreoffice-kit-wasm",
        "sherpa-onnx-*",
    ):
        assert pattern in script, pattern
    assert "! -name 'sherpa-onnx-node'" in script
    # Tools, skills, providers and MCP packages are the evolution surface.
    for kept in ("dsh-tool-", "dsh-skill-", "dsh-llm", "dsh-mcp"):
        assert kept not in script, kept
