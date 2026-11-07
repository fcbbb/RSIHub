"""Harbor candidate adapter for DeepSeek Harness (dsh).

This is a Harbor ``BaseInstalledAgent``, the same contract Codex and
MiniSWE implement: the agent is **installed into and runs inside the task
container**, while the thin Python adapter only stages files and interprets
results. Per trial it:

1. resolves the candidate profile from ``EVOLVE_CANDIDATE_SOURCE``
   (= ``<eval-checkout>/target``, injected by the harbor engine),
2. in ``install()`` uploads ``@deepseek-ai/dsh`` (plus Node when the task image
   has none or one older than 22.19) from ``DSH_RUNTIME_IMAGE`` (the locally
   built dsh image) or a pre-extracted ``DSH_RUNTIME_DIR`` and extracts it
   under ``/opt/evolve-dsh``,
3. in ``run()`` uploads the candidate tree and cordis patches and executes
   ``runners/sdk_minimal_run.mjs`` inside the container, which drives
   ``dsh --profile sdk-minimal`` over its JSON-RPC stdio protocol,
4. converts the session log written under ``/logs/agent/dsh-home`` into
   ``<logs>/trajectory.json`` so RSIHub's analyze operators
   (trace_browser / failure_patterns / trajectory_only) can read it.

The bash tool is the task container's own shell (sdk-minimal's local
terminal-bash); there is no host-side docker-exec bridge.

Endpoint governance: the adapter maps ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``
(frozen into the workspace identity at init) onto dsh's ``DEEPSEEK_BASE_URL`` /
``DEEPSEEK_API_KEY`` so model routing is owned by the workspace contract.

The frozen harness pieces (the in-container runner and cordis compositions)
live in ``runners/`` next to this file and are resolved relative to it; the
recipe's ``surface.exclude`` keeps them, this adapter, and the converter out
of the mutable surface.

Optional restricted-network compensations (all no-ops when unset):
  DSH_ASSETS_DIR             dir with ``uv``/``uvx`` binaries and ``py313.tar.gz``
                             to preload into containers whose graders cannot
                             reach github
  DSH_CONTAINER_APT_MIRROR   apt mirror base URL rewritten into sources.list
  DSH_CONTAINER_PIP_INDEX    pip index URL written to /etc/pip.conf
  DSH_CONTAINER_PROXY        http(s) proxy exported inside the container
  DSH_CONTAINER_NO_PROXY     no_proxy list (default: localhost,127.0.0.1)
  DSH_DOCKER_BIN             host ``docker`` CLI used to stage the runtime
                             image (optional; else resolved from ``PATH``).

This file ships in the seed but sits in ``surface.exclude``: a candidate that
edits it is rejected as ``invalid_proposal``.
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import importlib.util
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.models.trial.paths import EnvironmentPaths

CONTAINER_RUNTIME_ROOT = "/opt/evolve-dsh"
CONTAINER_DSH_BIN = f"{CONTAINER_RUNTIME_ROOT}/dsh/lib/bin.js"
CONTAINER_NODE = "/usr/local/bin/node"
RUNTIME_IMAGE_ENV = "DSH_RUNTIME_IMAGE"
RUNTIME_DIR_ENV = "DSH_RUNTIME_DIR"

# `@deepseek-ai/dsh`'s `lib/bin.js` self-dispatches on `import.meta.main`, which
# Node only exposes from 22.19 (and 24.2+). On an older runtime that expression
# is `undefined`, so dsh prints nothing, does nothing, and exits 0 -- the runner
# then dies with an unsettled top-level await (Node exit 13) instead of a useful
# error. The capability is probed directly instead of comparing version numbers
# so 23.x/24.0/24.1 installs are judged by what they actually support.
NODE_FEATURE_PROBE = "node --input-type=module -e 'process.stdout.write(String(import.meta.main))'"

# Fixed container paths used by run(): the adapter stages its files here and
# never depends on the host layout or on a docker-exec shell bridge.
CANDIDATE_IN_CONTAINER = "/tmp/evolve-candidate"
OVERLAY_IN_CONTAINER = "/tmp/evolve-candidate.overlay.cordis.yml"
HARBOR_PATCH_IN_CONTAINER = "/tmp/evolve-harbor.cordis.yml"
RUNNER_IN_CONTAINER = "/tmp/evolve-sdk-run.mjs"
TASK_IN_CONTAINER = "/tmp/evolve-task.txt"
CONTAINER_LOGS = EnvironmentPaths.agent_dir.as_posix()


def _load_sibling(module_name: str, filename: str):
    """Load a sibling helper without mutating ``sys.path``."""
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {filename} helper from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve_docker_bin = _load_sibling("dsh_docker_bin", "runners/docker_bin.py").resolve_docker_bin
convert_session = _load_sibling("dsh_trajectory", "dsh_trajectory.py").convert_session
materialize_candidate_overlay = _load_sibling(
    "dsh_candidate_overlay", "runners/candidate_overlay.py"
).materialize_candidate_overlay


def _docker(argv: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, capture_output=capture, text=True)


def _cache_key(image: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", image)


@functools.lru_cache(maxsize=32)
def _image_identity(docker_bin: str, image: str) -> str:
    """Include the image ID so a rebuilt tag never reuses a stale cache."""
    inspected = _docker(
        [docker_bin, "image", "inspect", "--format", "{{.Id}}", image],
        check=False,
    )
    identity = (inspected.stdout or "").strip()
    return f"{image}@{identity}" if identity else image


def _extract_from_image(docker_bin: str, image: str, staging: Path) -> None:
    helper = f"evolve-dsh-runtime-{uuid.uuid4().hex[:10]}"
    created = _docker([docker_bin, "create", "--name", helper, image], check=False)
    if created.returncode != 0:
        raise RuntimeError(
            f"cannot stage dsh runtime from image {image!r} "
            f"(build it with scripts/setup_terminal_bench.sh): {created.stderr.strip()}"
        )
    try:
        _docker(
            [
                docker_bin,
                "cp",
                f"{helper}:/usr/local/lib/node_modules/@deepseek-ai/dsh",
                str(staging / "dsh"),
            ]
        )
    finally:
        _docker([docker_bin, "rm", "-f", helper], check=False)


def _node_from_image(docker_bin: str, image: str) -> Path:
    """Extract the image's Node binary on demand (only if the task lacks one)."""
    root = Path(tempfile.gettempdir()) / "evolve-dsh-runtime" / f"node-{_cache_key(_image_identity(docker_bin, image))}"
    node = root / "node"
    if node.is_file():
        return node
    helper = f"evolve-dsh-node-{uuid.uuid4().hex[:10]}"
    created = _docker([docker_bin, "create", "--name", helper, image], check=False)
    if created.returncode != 0:
        raise RuntimeError(f"cannot stage Node from image {image!r}: {created.stderr.strip()}")
    root.mkdir(parents=True, exist_ok=True)
    try:
        _docker([docker_bin, "cp", f"{helper}:/usr/local/bin/node", str(node)])
    finally:
        _docker([docker_bin, "rm", "-f", helper], check=False)
    node.chmod(0o755)
    return node


def _runtime_tarball(docker_bin: str, image: str | None, runtime_dir: str | None) -> Path:
    """Build (once per image/dir) the tarball uploaded into every trial container.

    ``install()`` must transfer the runtime through Harbor's environment
    abstraction (``upload_file``), so the payload is a single archive. A file
    lock plus an atomic rename keeps concurrent installs from rebuilding it.
    """
    if runtime_dir:
        label = runtime_dir
    elif image:
        label = _image_identity(docker_bin, image)
    else:
        raise RuntimeError(
            "dsh runtime is not configured; set DSH_RUNTIME_IMAGE "
            "(for example the image built by scripts/setup_terminal_bench.sh) "
            "or DSH_RUNTIME_DIR with an extracted dsh tree"
        )
    root = Path(tempfile.gettempdir()) / "evolve-dsh-runtime" / _cache_key(label)
    tarball = root / "dsh-runtime.tar.gz"
    ready = root / ".ready"
    if ready.is_file() and tarball.is_file():
        return tarball
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent / f"{root.name}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ready.is_file() and tarball.is_file():
            return tarball
        staging = root.parent / f"{root.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            if runtime_dir:
                source = Path(runtime_dir).expanduser()
                if not (source / "dsh" / "lib" / "bin.js").is_file():
                    raise RuntimeError(f"DSH_RUNTIME_DIR={runtime_dir!r} has no dsh/lib/bin.js")
                shutil.copytree(source / "dsh", staging / "dsh", symlinks=True)
            else:
                assert image is not None
                _extract_from_image(docker_bin, image, staging)
            tmp_tar = staging / "dsh-runtime.tar.gz"
            with tarfile.open(tmp_tar, "w:gz") as archive:
                archive.add(staging / "dsh", arcname="dsh")
            (staging / ".ready").write_text(label)
            shutil.rmtree(root, ignore_errors=True)
            staging.rename(root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return tarball


class DshAgent(BaseInstalledAgent):
    SUPPORTS_ATIF = False

    @staticmethod
    def name() -> str:
        return "dsh-candidate"

    def _env(self, key: str, default: str | None = None) -> str | None:
        value = self._extra_env.get(key)
        if value is None:
            value = os.environ.get(key)
        return default if value is None else value

    @staticmethod
    def _runners_dir() -> Path:
        return Path(__file__).resolve().parent / "runners"

    # ------------------------------------------------------------------
    # Optional container bootstrap. TB2 graders assume container network
    # access; in restricted environments the snippets below compensate
    # without touching scoring. Every snippet is gated on its env var.
    # ------------------------------------------------------------------
    _APT_SNIPPET = r"""
BM={mirror}
FILES="/etc/apt/sources.list $(ls /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources 2>/dev/null)"
if [ -f /etc/os-release ] && grep -qi ubuntu /etc/os-release; then
  sed -i -E "s|https?://[a-z0-9.]*archive.ubuntu.com/ubuntu|$BM/ubuntu|g; s|https?://security.ubuntu.com/ubuntu|$BM/ubuntu|g; s|https?://ports.ubuntu.com/ubuntu-ports|$BM/ubuntu-ports|g" $FILES 2>/dev/null
else
  sed -i -E "s|https?://deb.debian.org/debian-security|$BM/debian-security|g; s|https?://security.debian.org/debian-security|$BM/debian-security|g; s|https?://security.debian.org|$BM/debian-security|g; s|https?://deb.debian.org/debian|$BM/debian|g" $FILES 2>/dev/null
fi
"""

    _PIP_SNIPPET = r"""
{{ echo "[global]"; echo "index-url = {index}"; }} > /etc/pip.conf 2>/dev/null
"""

    _ASSETS_SNIPPET = r"""
if [ -f /tmp/py313.tar.gz ]; then
  mkdir -p /opt && tar xzf /tmp/py313.tar.gz -C /opt 2>/dev/null
  ln -sf /opt/py313/bin/python3.13 /usr/local/bin/python3.13 2>/dev/null
fi
if [ -f /tmp/uv ]; then chmod +x /tmp/uv /tmp/uvx 2>/dev/null; cp /tmp/uv /usr/local/bin/uv 2>/dev/null; cp /tmp/uvx /usr/local/bin/uvx 2>/dev/null; fi
for H in /root "$HOME" /home/*; do
  [ -d "$H" ] || continue
  mkdir -p "$H/.local/bin" 2>/dev/null
  {{ printf 'export PATH="/usr/local/bin:%s/.local/bin:$PATH"\n' "$H"
    echo 'export UV_PYTHON_DOWNLOADS=never'
  }} > "$H/.local/bin/env" 2>/dev/null
  cp /tmp/uv "$H/.local/bin/uv" 2>/dev/null; cp /tmp/uvx "$H/.local/bin/uvx" 2>/dev/null; chmod +x "$H/.local/bin/uv" "$H/.local/bin/uvx" 2>/dev/null
done
"""

    _PROXY_SNIPPET = r"""
PXY={proxy}
NPXY="{no_proxy}"
{{ printf 'export http_proxy=%s https_proxy=%s HTTP_PROXY=%s HTTPS_PROXY=%s\n' "$PXY" "$PXY" "$PXY" "$PXY"
  printf 'export no_proxy=%s NO_PROXY=%s\n' "$NPXY" "$NPXY"; }} >> /etc/bash.bashrc 2>/dev/null
for H in /root "$HOME" /home/*; do
  [ -f "$H/.local/bin/env" ] || continue
  {{ printf 'export http_proxy=%s https_proxy=%s HTTP_PROXY=%s HTTPS_PROXY=%s\n' "$PXY" "$PXY" "$PXY" "$PXY"
    printf 'export no_proxy=%s NO_PROXY=%s\n' "$NPXY" "$NPXY"; }} >> "$H/.local/bin/env" 2>/dev/null
done
"""

    def _bootstrap_command(self) -> str | None:
        parts: list[str] = []
        mirror = self._env("DSH_CONTAINER_APT_MIRROR")
        if mirror:
            parts.append(self._APT_SNIPPET.format(mirror=mirror))
        pip_index = self._env("DSH_CONTAINER_PIP_INDEX")
        if pip_index:
            parts.append(self._PIP_SNIPPET.format(index=pip_index))
        if self._env("DSH_ASSETS_DIR"):
            parts.append(self._ASSETS_SNIPPET)
        proxy = self._env("DSH_CONTAINER_PROXY")
        if proxy:
            parts.append(
                self._PROXY_SNIPPET.format(
                    proxy=proxy,
                    no_proxy=self._env("DSH_CONTAINER_NO_PROXY", "localhost,127.0.0.1"),
                )
            )
        if not parts:
            return None
        return "set +e\n" + "\n".join(parts) + "\ntrue\n"

    # ------------------------------------------------------------------
    # BaseInstalledAgent contract
    # ------------------------------------------------------------------
    async def install(self, environment) -> None:
        """Install dsh (and Node when needed) into the task container."""
        await self.exec_as_root(environment, "mkdir -p /opt/evolve-dsh /installed-agent")
        image = self._env(RUNTIME_IMAGE_ENV)
        runtime_dir = self._env(RUNTIME_DIR_ENV)
        # DSH_RUNTIME_DIR is a docker-free alternative to the runtime image.
        docker_bin = ""
        if image and not runtime_dir:
            docker_bin = resolve_docker_bin(env={**os.environ, **self._extra_env})
        tarball = await asyncio.to_thread(_runtime_tarball, docker_bin, image, runtime_dir)
        await environment.upload_file(str(tarball), "/tmp/evolve-dsh-runtime.tar.gz")
        await self.exec_as_root(
            environment,
            "rm -rf /opt/evolve-dsh/dsh && tar -xzf /tmp/evolve-dsh-runtime.tar.gz -C /opt/evolve-dsh && "
            f"test -f {CONTAINER_DSH_BIN}",
        )
        await self._ensure_node(environment, docker_bin, image, runtime_dir)

    @staticmethod
    async def _node_ready(environment) -> bool:
        """True when the container's ``node`` can actually boot dsh."""
        probe = await environment.exec(NODE_FEATURE_PROBE)
        return probe.return_code == 0 and (probe.stdout or "").strip() == "true"

    async def _ensure_node(self, environment, docker_bin: str, image: str | None, runtime_dir: str | None) -> None:
        if await self._node_ready(environment):
            return
        host_node: Path | None = None
        if runtime_dir:
            candidate = Path(runtime_dir).expanduser() / "node"
            if candidate.is_file():
                host_node = candidate
        if host_node is None and image:
            if not docker_bin:
                docker_bin = resolve_docker_bin(env={**os.environ, **self._extra_env})
            host_node = await asyncio.to_thread(_node_from_image, docker_bin, image)
        if host_node is None or not host_node.is_file():
            raise RuntimeError(
                "task container has no Node able to run dsh (needs import.meta.main, "
                "i.e. >= 22.19) and no Node binary is available to stage"
            )
        await environment.upload_file(str(host_node), "/tmp/evolve-node")
        await self.exec_as_root(environment, f"install -m 755 /tmp/evolve-node {CONTAINER_NODE}")
        if not await self._node_ready(environment):
            check = await environment.exec("node --version")
            raise RuntimeError(
                f"staged Node {check.stdout.strip() or '<unknown>'} cannot run dsh "
                "(needs import.meta.main, i.e. Node >= 22.19)"
            )

    def get_version_command(self) -> str | None:
        return f"node {CONTAINER_DSH_BIN} --version"

    async def setup(self, environment) -> None:
        try:
            assets_dir = self._env("DSH_ASSETS_DIR")
            if assets_dir:
                for name, dest in (("uv", "/tmp/uv"), ("uvx", "/tmp/uvx"), ("py313.tar.gz", "/tmp/py313.tar.gz")):
                    src = Path(assets_dir) / name
                    if src.is_file():
                        await environment.upload_file(str(src), dest)
            command = self._bootstrap_command()
            if command:
                await environment.exec(command=command, user="root", timeout_sec=120)
        except Exception as error:  # best-effort: fall back to the container as-is
            self.logger.warning("container bootstrap skipped: %r", error)
        await super().setup(environment)

    def _candidate_dir(self) -> Path:
        source = self._env("EVOLVE_CANDIDATE_SOURCE")
        if not source:
            raise RuntimeError("EVOLVE_CANDIDATE_INVALID: EVOLVE_CANDIDATE_SOURCE missing")
        candidate = Path(source).expanduser().resolve()
        if not (candidate / "profile.cordis.yml").is_file():
            raise RuntimeError(f"EVOLVE_CANDIDATE_INVALID: no profile.cordis.yml under {candidate}")
        return candidate

    @staticmethod
    def _tar_candidate(candidate: Path, destination: Path) -> Path:
        with tarfile.open(destination, "w:gz") as archive:
            for child in candidate.iterdir():
                archive.add(child, arcname=child.name)
        return destination

    async def _container_cwd(self, environment) -> str:
        result = await environment.exec("pwd")
        return (result.stdout or "").strip() or "/app"

    async def run(self, instruction, environment, context) -> None:
        candidate = self._candidate_dir()
        runners = self._runners_dir()
        cordis = Path(self._env("DSH_ROLLOUT_CORDIS", str(runners / "compositions" / "rollout.base.cordis.yml")))
        if not cordis.is_file():
            raise RuntimeError(f"rollout cordis composition missing: {cordis}")
        runner = runners / "sdk_minimal_run.mjs"
        if not runner.is_file():
            raise RuntimeError(f"in-container dsh runner missing: {runner}")

        logs = Path(self.logs_dir)
        logs.mkdir(parents=True, exist_ok=True)
        task_file = logs / "instruction.txt"
        task_file.write_text(instruction)
        archive = await asyncio.to_thread(self._tar_candidate, candidate, logs / "candidate.tar.gz")
        overlay = materialize_candidate_overlay(
            candidate,
            logs,
            output_path=logs / "candidate.overlay.cordis.yml",
            plugin_root=Path(CANDIDATE_IN_CONTAINER),
        )

        await self.exec_as_root(
            environment,
            f"rm -rf {CANDIDATE_IN_CONTAINER} {TASK_IN_CONTAINER} && mkdir -p {CANDIDATE_IN_CONTAINER}",
        )
        await environment.upload_file(str(archive), "/tmp/evolve-candidate.tar.gz")
        await self.exec_as_root(environment, f"tar -xzf /tmp/evolve-candidate.tar.gz -C {CANDIDATE_IN_CONTAINER}")
        for source, destination in (
            (overlay, OVERLAY_IN_CONTAINER),
            (cordis, HARBOR_PATCH_IN_CONTAINER),
            (runner, RUNNER_IN_CONTAINER),
            (task_file, TASK_IN_CONTAINER),
        ):
            await environment.upload_file(str(source), destination)

        model = self.model_name or "deepseek-v4-flash"
        if "/" in model:
            model = model.split("/", 1)[1]
        timeout = int(float(self._env("DSH_TASK_TIMEOUT_SEC", "1800") or "1800"))
        cwd = await self._container_cwd(environment)
        env = {
            "DSH_DBIN": CONTAINER_DSH_BIN,
            "DSH_PATCHES": f"{OVERLAY_IN_CONTAINER}:{HARBOR_PATCH_IN_CONTAINER}",
            "DSH_CWD": cwd,
            "DSH_PROVIDER": "deepseek-official",
            "DSH_MODEL": model,
            "DSH_CONTEXT_WINDOW": self._env("DSH_CONTEXT_WINDOW", "1000000") or "1000000",
            "DSH_MAX_TOKENS": self._env("DSH_MAX_TOKENS", "49152") or "49152",
            "DSH_SESSION_ID": (self.session_id or "task").replace("/", "_"),
            "DSH_HOME": f"{CONTAINER_LOGS}/dsh-home",
            "DSH_TASK_FILE": TASK_IN_CONTAINER,
            "DSH_FINAL_FILE": f"{CONTAINER_LOGS}/final_response.txt",
            "DSH_EVENTS_FILE": f"{CONTAINER_LOGS}/dsh-events.jsonl",
            "DSH_RUN_TIMEOUT_SEC": str(max(1, timeout - 5)),
            "DEEPSEEK_API_KEY": self._env("OPENAI_API_KEY", "") or "",
            "DEEPSEEK_BASE_URL": self._env("OPENAI_BASE_URL", "") or "",
        }
        # Empty values must not shadow credentials/config already present in
        # the container's environment.
        env = {key: value for key, value in env.items() if value}
        try:
            result = await environment.exec(
                command=f"node {RUNNER_IN_CONTAINER}",
                env=env,
                cwd=cwd,
                timeout_sec=timeout,
            )
        finally:
            # dsh creates $DSH_HOME/sessions with mode 0700; the converter runs
            # on the host against the /logs/agent bind mount, so make the tree
            # readable before parsing (best-effort: the container may be gone).
            try:
                await self.exec_as_root(
                    environment,
                    f"chmod -R a+rX {CONTAINER_LOGS}/dsh-home 2>/dev/null || true",
                )
            except Exception:
                pass
            self._write_trajectory(logs)
        if result.return_code != 0:
            # Docker exec merges the container's stderr into stdout, so classify
            # and report from the combined output (Harbor maps known patterns
            # such as provider rate limits onto typed errors).
            raise self._classify_exec_error(f"node {RUNNER_IN_CONTAINER} (session {self.session_id})", result)

    def _write_trajectory(self, logs: Path) -> None:
        """Convert the dsh session log into trajectory.json (best-effort)."""
        try:
            convert_session(logs / "dsh-home", logs / "trajectory.json")
        except Exception as error:
            self.logger.warning("trajectory conversion failed: %r", error)
