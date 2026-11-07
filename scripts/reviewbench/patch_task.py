#!/usr/bin/env python3
"""Patch one exported ReviewBench Harbor task for a custom judge.

The ReviewBench exporter only generates a model registry for its standard
Anthropic judge. Its agent and verifier both keep `network_mode = "allowlist"`
so the agent cannot fetch the upstream PR or its real reviews. This script
applies the two changes the exporter cannot express:

* a custom OpenAI-compatible models.json for the judge plus a
  `PI_CODING_AGENT_DIR` entry that points the verifier at it;
* removal of the deployment model host from `[agent].allowed_hosts`.

The exporter refuses to run without at least one `--agent-host` and writes it
straight into `[agent].allowed_hosts`, which would bake a deployment-specific
endpoint into the dataset. RSIHub injects the host per run instead
(`EVOLVE_HARBOR_AGENT_ALLOWED_HOSTS` → `harbor run --allow-agent-host`), so the
exported entry is stripped back out here. The judge host stays in `[verifier]`
and `[verifier.environment]`: the verifier is a separate environment and also
receives no `--allow-agent-host` merge.

DNS under the allowlist sandbox is handled once for both roles by
scripts/reviewbench/patch_sidecar_dns.py, which patches Harbor's shared egress
sidecar.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


def patch_task_toml(task: Path) -> None:
    path = task / "task.toml"
    text = path.read_text()
    has_pi = "PI_CODING_AGENT_DIR" in text
    section = None
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            out.append(line)
            if section == "[verifier.env]" and not has_pi:
                out.append('PI_CODING_AGENT_DIR = "/tests/judge/.reviewbench-models"')
                has_pi = True
            continue
        out.append(line)
    if not has_pi:
        raise SystemExit(f"{path}: no [verifier.env] section to patch")
    path.write_text("\n".join(out) + "\n")


def write_models_json(task: Path, provider: str, model: str, base_url: str) -> None:
    directory = task / "tests" / "judge" / ".reviewbench-models"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "providers": {
            provider: {
                "baseUrl": base_url,
                "api": "openai-completions",
                "models": [
                    {
                        "id": model,
                        "name": model,
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": 1000000,
                        "maxTokens": 8192,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    }
                ],
            }
        }
    }
    (directory / "models.json").write_text(json.dumps(payload, indent=2) + "\n")


def strip_agent_allowed_hosts(task: Path) -> None:
    """Empty `[agent].allowed_hosts`, keeping `network_mode = "allowlist"`.

    The mode must stay `allowlist`: `merge_extra_allowlists` ignores run-time
    hosts when the policy is `public`.
    """
    path = task / "task.toml"
    lines = path.read_text().splitlines()
    section = None
    stripped_hosts = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            out.append(line)
            continue
        if section == "[agent]" and stripped.startswith("allowed_hosts"):
            if "[" not in stripped or "]" not in stripped:
                raise SystemExit(f"{path}: multi-line [agent].allowed_hosts is unsupported")
            out.append("allowed_hosts = []")
            stripped_hosts = True
            continue
        out.append(line)
    if not stripped_hosts:
        raise SystemExit(f"{path}: no [agent].allowed_hosts to strip")
    path.write_text("\n".join(out) + "\n")


def verify_task_toml(task: Path) -> None:
    payload = tomllib.loads((task / "task.toml").read_text())
    agent = payload.get("agent") or {}
    if agent.get("allowed_hosts"):
        raise SystemExit(
            f"{task / 'task.toml'}: [agent].allowed_hosts must be empty; the model host is injected at run time"
        )
    if agent.get("network_mode") != "allowlist":
        raise SystemExit(f"{task / 'task.toml'}: [agent].network_mode must stay 'allowlist'")
    verifier = payload.get("verifier") or {}
    if verifier.get("network_mode") == "allowlist" and not verifier.get("allowed_hosts"):
        raise SystemExit(f"{task / 'task.toml'}: [verifier].allowed_hosts lost the judge host")


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("usage: patch_task.py TASK_DIR PROVIDER MODEL BASE_URL")
    task = Path(sys.argv[1])
    provider, model, base_url = sys.argv[2:5]
    if not (task / "task.toml").is_file():
        raise SystemExit(f"{task}: not an exported task directory")
    patch_task_toml(task)
    strip_agent_allowed_hosts(task)
    verify_task_toml(task)
    write_models_json(task, provider, model, base_url)
    print(f"patched {task.name}")


if __name__ == "__main__":
    main()
