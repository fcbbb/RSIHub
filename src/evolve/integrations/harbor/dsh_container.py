"""Fixed DeepSeek Harness Harbor agents for AHE mutate and debugger stages.

The candidate being evolved stays on the host-side ``sdk-minimal`` runtime
(``target.agent:DshAgent``).  This module runs the *meta* agents inside the
``evolve-mutate-dsh`` task image with ``dsh --profile headless``:

* ``role=mutate`` edits ``/app/task/workspace`` under ``danger-full-access``.
  dsh's sandboxed bash needs bwrap or Landlock, neither of which is available
  in this disposable container image; ``workspace-write`` fails closed with
  "no sandbox backend", so the mutate agent cannot run any command (including
  the boot checks its prompt asks for). ``danger-full-access`` keeps the same
  approval policy (``never``) but runs commands unconfined inside the
  single-use mutation container.
* ``role=analyst`` reads ``/app/task/inputs/trace-evidence.json`` and runs
  read-only checks under ``danger-full-access``.  The debugger needs the same
  unconfined shell as the mutator because this image has no sandbox backend;
  its container is a separate single-use Harbor trial, so commands cannot
  touch the candidate workspace.

The role is an interface (``agent_kwargs.role``), not a separate class, so the
recipe can keep every non-candidate agent fixed while still selecting mutate or
analyst.  The dsh version is also configurable through ``agent_kwargs.version``.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, override

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

DEFAULT_DSH_VERSION = "0.2.1-rc.5"
DSH_HOME = "/tmp/evolve-dsh-home"
TASK_PATH = "/tmp/evolve-dsh-task.txt"
OVERLAY_ROOT = Path("/opt/evolve/dsh")
MUTATE_WORKDIR = "/app/task/workspace"
ANALYST_WORKDIR = "/app/task"
_ROLES = {
    "mutate": {
        "overlay": "headless.mutate.cordis.yml",
        "workdir": MUTATE_WORKDIR,
        "permission_mode": "danger-full-access",
        "timeout_s": 3300,
    },
    "analyst": {
        "overlay": "headless.analyst.cordis.yml",
        "workdir": ANALYST_WORKDIR,
        "permission_mode": "danger-full-access",
        "timeout_s": 900,
    },
}


class DshContainerAgent(BaseAgent):
    """Run one headless dsh turn inside the Harbor mutation container."""

    SUPPORTS_ATIF = False

    def __init__(
        self,
        *args: Any,
        role: str = "mutate",
        version: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if role not in _ROLES:
            available = ", ".join(sorted(_ROLES))
            raise ValueError(f"unsupported dsh container role {role!r}; available: {available}")
        self._role = role
        self._dsh_version = str(version or DEFAULT_DSH_VERSION)
        self._session_id: str | None = None
        self._event_count = 0

    @staticmethod
    @override
    def name() -> str:
        return "dsh-headless"

    @override
    def version(self) -> str | None:
        return self._dsh_version

    @property
    def _role_config(self) -> dict[str, Any]:
        return _ROLES[self._role]

    @property
    def _overlay_path(self) -> str:
        return str(OVERLAY_ROOT / self._role_config["overlay"])

    def _model_id(self) -> str:
        if not self.model_name:
            raise ValueError(
                "DshContainerAgent requires operators.<stage>.config.model (dsh resolves the model from DSH_MODEL)"
            )
        return self.model_name.split("/", 1)[-1]

    def _run_env(self) -> dict[str, str]:
        env = dict(self._extra_env)
        env["DSH_HOME"] = DSH_HOME
        env["DSH_PERMISSION_MODE"] = str(self._role_config["permission_mode"])
        env["DSH_MODEL"] = self._model_id()
        env.setdefault("DSH_CONTEXT_WINDOW", "1000000")
        env.setdefault("DSH_TELEMETRY_DISABLED", "1")

        api_key = env.get("OPENAI_API_KEY") or env.get("DEEPSEEK_API_KEY")
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
        base_url = env.get("OPENAI_BASE_URL") or env.get("OPENAI_API_BASE")
        if base_url:
            env["DEEPSEEK_BASE_URL"] = base_url
        return env

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        env = self._run_env()
        probe = await environment.exec(
            command=f"test -f {shlex.quote(self._overlay_path)} && dsh --version",
            env=env,
            timeout_sec=60,
        )
        if probe.return_code != 0:
            raise RuntimeError(
                "dsh mutation image is missing dsh or its headless overlay: "
                f"{(probe.stderr or probe.stdout or '').strip()[-2000:]}"
            )
        composed = await environment.exec(
            command=(f"dsh --profile headless --dump-config --patch {shlex.quote(self._overlay_path)}"),
            env=env,
            timeout_sec=120,
        )
        if composed.return_code != 0:
            raise RuntimeError(
                f"dsh headless overlay does not compose: {(composed.stderr or composed.stdout or '').strip()[-4000:]}"
            )

    def _parse_events(self, stdout: str) -> tuple[list[dict[str, Any]], str]:
        events: list[dict[str, Any]] = []
        final_text: str | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            if event.get("type") == "final":
                text = event.get("text")
                final_text = text if isinstance(text, str) else None
            elif event.get("type") == "session":
                session_id = event.get("sessionId") or event.get("session_id")
                if isinstance(session_id, str):
                    self._session_id = session_id
        if final_text is None:
            error = next(
                (event.get("message") for event in events if event.get("type") == "error"),
                None,
            )
            detail = f"dsh error: {error}" if error else "dsh emitted no final event"
            raise RuntimeError(detail)
        return events, final_text

    def _write_trajectory(self, final_text: str, events: list[dict[str, Any]]) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "dsh-events.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
        )
        (self.logs_dir / "trajectory.json").write_text(
            json.dumps(
                {
                    "steps": [
                        {
                            "source": "agent",
                            "message": final_text,
                        }
                    ]
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        task_path = self.logs_dir / "dsh-task.txt"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(instruction.rstrip() + "\n")
        await environment.upload_file(task_path, TASK_PATH)

        env = self._run_env()
        workdir = str(self._role_config["workdir"])
        timeout_s = int(env.get("DSH_TASK_TIMEOUT_SEC", self._role_config["timeout_s"]))
        command = (
            f"cd {shlex.quote(workdir)} && "
            f"dsh --profile headless --patch {shlex.quote(self._overlay_path)} "
            f"--json - < {shlex.quote(TASK_PATH)}"
        )
        result = await environment.exec(
            command=command,
            cwd=workdir,
            env=env,
            timeout_sec=timeout_s,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        (self.logs_dir / "dsh-stdout.jsonl").write_text(stdout)
        (self.logs_dir / "dsh-stderr.txt").write_text(stderr)

        try:
            events, final_text = self._parse_events(stdout)
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} (exit {result.return_code}); stderr tail: {stderr.strip()[-4000:]}") from exc
        if result.return_code != 0:
            raise RuntimeError(f"dsh headless exited {result.return_code}; stderr tail: {stderr.strip()[-4000:]}")

        self._event_count = len(events)
        self._write_trajectory(final_text, events)
        context.metadata = {
            **(context.metadata or {}),
            "dsh": {
                "role": self._role,
                "session_id": self._session_id,
                "events": self._event_count,
            },
        }
