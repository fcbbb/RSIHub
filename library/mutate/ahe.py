"""AHE strategy: turn current evidence into one testable harness change."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from evolve.agent import AgentCommandError
from evolve.frozen import sdk
from evolve.frozen.config import integer, string
from evolve.frozen.interfaces import MutateOperator, MutateResult, OperatorContext
from evolve.patching import create_candidate_patch, load_surface_policy, patch_parent_ref
from library._shared.artifacts import render_artifact_guidance
from library._shared.runners import run_agent, runner_name
from library.mutate._config import WORKSPACE_CONFIG
from library.mutate._support.workspace import workspace_contract

MANIFEST_START = "<AHE_CHANGE_MANIFEST>"
MANIFEST_END = "</AHE_CHANGE_MANIFEST>"
MANIFEST_FILE = Path("target/.ahe-change-manifest.json")
CONFIG = WORKSPACE_CONFIG.extend(
    {
        "prompt_path": string(),
        "skills_dir": string(),
        "memory_dir": string(),
        # A rejected change manifest costs the whole generation, so give the
        # agent one more turn to rewrite the control file it already knows how
        # to produce. The candidate edits are kept; only the manifest is redone.
        "manifest_retries": integer(default=1, minimum=0),
    }
)


AHE_PROMPT = """# Agentic Harness Engineering

Improve the configured candidate harness; do not solve a benchmark task directly.
Optimize pass@1. Treat debugger reports as evidence, not proof. Follow the
workspace contract and make changes only within the declared mutable surface.

For this generation, use this order and do not edit before completing the evidence review:
1. Read the debugger overview and identify every selected failure signature.
2. For each distinct failure signature, read at least one selected task detail. Cite an exact, meaningful quote from that file; inspect raw trace evidence when the detail is incomplete or conflicting.
3. Read change_evaluation.json and the previous change context.
4. Write a falsifiable hypothesis that separates observed facts from inferred cause, names an expected benefit and regression risk, and states what evidence would disprove it.
5. Only after steps 1-4, decide KEEP, REVISE, or ROLLBACK + PIVOT. If the details do not support a change, preserve the harness and state why.
6. Identify the active execution path and choose the harness component matching the evidence.
7. Make one coherent change within the declared mutable surface and run proportionate checks.
8. Write the structured evidence review and change manifest to the required control file.

Files on the active execution path become the deployed benchmark-solving harness.
Evolution artifacts and instructions in this prompt are not available inside benchmark episodes.
If you edit a runtime prompt, include only instructions usable by the benchmark-solving agent.
Do not copy this evolution workflow, evidence
paths, KEEP/REVISE/ROLLBACK decisions, or manifest requirements into runtime files.
Do not refer to debuggers or other evolution-only context in runtime prompts.
Determine the execution path from the supplied workspace contract and evaluator configuration;
do not assume an agent class, configuration name, or repository layout.

Current debugger reports evaluate the selected parent. The new edit will be
evaluated by the next loop. Do not edit the Harbor adapter, evaluator, mechanism,
archive, workspace configuration, task partitions, model selection, credentials,
endpoints, or resource limits.
"""

CODEX_AHE_PROMPT = """# Agentic Harness Engineering

Improve the Codex harness under `target/`; do not solve a benchmark task
directly. Optimize pass@1. Treat debugger reports as evidence, not proof.

For this generation, use this order and do not edit before completing the evidence review:
1. Read the debugger overview and identify every selected failure signature.
2. For each distinct failure signature, read at least one selected task detail. Cite an exact, meaningful quote from that file; inspect raw trace evidence when the detail is incomplete or conflicting.
3. Read change_evaluation.json and the previous change context.
4. Write a falsifiable hypothesis that separates observed facts from inferred cause, names an expected benefit and regression risk, and states what evidence would disprove it.
5. Only after steps 1-4, decide KEEP, REVISE, or ROLLBACK + PIVOT. If the details do not support a change, preserve the harness and state why.
6. Identify the active execution path and choose the harness component matching the evidence.
7. Make one coherent change within the declared mutable surface and run proportionate checks.
8. Write the structured evidence review and change manifest to the required control file.

Files under `target/` become the deployed benchmark-solving harness. The
evolvable surface includes the runtime prompt, skills, Codex configuration, and
the local plugin under `target/plugins/`, including lifecycle hooks.
Evolution artifacts and instructions in this prompt are not available inside
benchmark episodes. If you edit runtime instructions or hook-provided context,
include only guidance usable by the benchmark-solving agent. Do not copy this
evolution workflow, evidence paths, KEEP/REVISE/ROLLBACK decisions, or manifest
requirements into target files. Do not refer to debuggers or other
evolution-only context in target runtime prompts, skills, or plugin output.
Canonical evaluation runs `target.agent:HarborAgent`, installs the candidate
plugin into a temporary Codex home, and invokes the pinned Codex CLI. Make
changes on that execution path.

Current debugger reports evaluate the selected parent. The new edit will be
evaluated by the next loop. Do not edit the evaluator, mechanism, archive,
workspace configuration, task partitions, model selection, credentials,
endpoints, or resource limits.
"""

DSH_AHE_PROMPT = """# Agentic Harness Engineering

Improve the DeepSeek Harness (dsh) candidate under `target/`; do not solve a
benchmark task directly. Optimize pass@1. Treat debugger reports as evidence,
not proof. Make changes only within the declared mutable surface.

For this generation, use this order and do not edit before completing the evidence review:
1. Read the debugger overview and identify every selected failure signature.
2. For each distinct failure signature, read at least one selected task detail. Cite an exact, meaningful quote from that file; inspect raw trace evidence when the detail is incomplete or conflicting.
3. Read change_evaluation.json and the previous change context.
4. Write a falsifiable hypothesis that separates observed facts from inferred cause, names an expected benefit and regression risk, and states what evidence would disprove it.
5. Only after steps 1-4, decide KEEP, REVISE, or ROLLBACK + PIVOT. If the details do not support a change, preserve the harness and state why.
6. Identify the active execution path and choose the harness component matching the evidence.
7. Make one coherent change within the declared mutable surface and run proportionate checks.
8. Write the structured evidence review and change manifest to the required control file.

`target/profile.cordis.yml` is a patch over the fixed `sdk-minimal` dsh bundle.
The evolvable surface is the candidate profile, `target/plugins/`, and
`target/skills/`. The sdk-minimal bundle is fixed and already installed; you
may add or change profile rows and local plugins that use any package the
runtime installs.

Addable capabilities (do not add npm dependencies or import removed demo-spine
packages):
- Profile rows for any installed `@deepseek-ai/dsh-*` package, including the
  fixed runtime's `dsh-tool-*`, model providers, and MCP resources.
- Local ESM plugins under `target/plugins/`, referenced from the profile as
  `./plugins/*.mjs`; the rollout driver rewrites those names to absolute
  paths. A plugin can register tools, providers, or lifecycle hooks through
  the `ctx` it receives (see `target/plugins/seed-probe.mjs`).
- Skill bundles under `target/skills/`. The sdk-minimal bundle does not load
  skills by default. To enable them, insert the installed skill rows into the
  profile:
      - insert:
          - id: skill
            name: '@deepseek-ai/dsh-skill'
          - id: skill-filesystem
            name: '@deepseek-ai/dsh-skill-filesystem'
            config: {customSkillDirs: ['/opt/evolve-dsh/state/candidate/skills']}
          - id: tool-skill
            name: '@deepseek-ai/dsh-tool-skill'
  Rows that do not exist yet must go under `insert`; a bare `- id: ...` entry
  patches an existing row and dsh skips it with "entry not found". Alternatively
  add a plugin that registers a skill provider. The candidate runtime is
  staged at `/opt/evolve-dsh/state/candidate`; a local plugin should resolve sibling
  paths from `import.meta.url` rather than assuming the process cwd.

Pinned DSH plugin API:
- The configured image pins dsh-plugin-manager, dsh-tools, dsh-agent, and
  dsh-agent-loop at version 0.2.1-rc.5. A candidate plugin exports name,
  optional inject, and apply(ctx, config); seed-probe is the local example.
- Register tools with ctx.tools.register({name, description, parameters, output,
  execute}). Use JSON Schema for parameters and output.schema; output.render(args,
  value) returns content blocks. execute(args, exec) should honor exec.signal.
  Registration returns a disposer for the tool.
- The supported schema subset is type, oneOf, properties, required,
  additionalProperties, items, enum, and const, with annotation fields.
- Runtime hooks in this image include system-prompt/assemble, agent/request,
  agent/pre-step, agent/disposed, workspace/session-activity, and
  workspace/session-stop. Check each hook's scope and payload; preserve waterfall
  next() behavior and dispose owned resources.
- Do not import Cordis or DSH packages, add npm dependencies, or rely on Host
  inspector tools from a candidate plugin. Use capabilities injected through ctx.

Never edit `target/agent.py`, `target/dsh_trajectory.py`, or
`target/runners/**`: those are frozen Harbor/rollout seams. Keep the profile
valid YAML and keep plugin imports resolvable. If you edit runtime
instructions or plugin output, include only guidance usable by the
benchmark-solving agent; do not copy this evolution workflow, evidence paths,
KEEP/REVISE/ROLLBACK decisions, or manifest requirements into target files, and
do not refer to debuggers or other evolution-only context there.

Current debugger reports evaluate the selected parent. The new edit will be
evaluated by the next loop. Do not edit the evaluator, mechanism, archive,
workspace configuration, task partitions, model selection, credentials,
endpoints, or resource limits.
"""

MANIFEST_TEMPLATE = {
    "iteration": 1,
    "hypothesis": "If ... then ... because the reviewed task evidence shows ...",
    "disconfirming_observation": "A concrete observation that would disprove the hypothesis",
    "evidence_reviews": [
        {
            "task_name": "task-name",
            "signature": "failure-signature",
            "evidence_quote": "exact text copied from the selected detail file",
            "finding": "what the quote supports and what it does not prove",
        }
    ],
    "changes": [
        {
            "id": "chg-1",
            "type": "new|improvement|rollback",
            "description": "what changed and why",
            "files": ["target/path.py"],
            "failure_pattern": "failure class addressed",
            "predicted_fixes": ["task-name"],
            "risk_tasks": [],
            "constraint_level": "middleware|tool_impl|tool_desc|skill|prompt|plugin|hook",
            "why_this_component": "why this component level fits the root cause",
        }
    ],
}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _safe_usage(usage: object) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {"usd": 0}
    normalized = dict(usage)
    usd = normalized.get("usd", 0)
    normalized["usd"] = usd if isinstance(usd, (int, float)) and not isinstance(usd, bool) else 0
    return normalized


def _iteration_matches(value: object, genid: str) -> bool:
    """Accept the operator genid, or the bare generation number it starts with.

    Fan-out children are addressed as ``2-1``, and models routinely write just
    ``2`` (or the number 2) for the generation they are editing. The field is a
    self-check that the model used the current context, not an identifier the
    run keys on, so the narrower value is accepted; anything else is not.
    """
    if str(value) == genid:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return False
    return str(value) == genid.split("-", 1)[0]


def _extract_manifest(output: str, genid: str) -> dict[str, Any]:
    starts = [match.start() for match in re.finditer(re.escape(MANIFEST_START), output)]
    ends = [match.start() for match in re.finditer(re.escape(MANIFEST_END), output)]
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise ValueError("mutate output must contain exactly one AHE manifest block")
    raw = output[starts[0] + len(MANIFEST_START) : ends[0]].strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("AHE manifest must be a JSON object")
    if not _iteration_matches(payload.get("iteration"), genid):
        raise ValueError("AHE manifest iteration does not match operator context")
    if not isinstance(payload.get("changes"), list):
        raise ValueError("AHE manifest changes must be a list")
    for field in ("hypothesis", "disconfirming_observation"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"AHE manifest {field} must be nonempty text")
    reviews = payload.get("evidence_reviews")
    if not isinstance(reviews, list):
        raise ValueError("AHE manifest evidence_reviews must be a list")
    for review in reviews:
        if not isinstance(review, dict) or any(
            not isinstance(review.get(field), str) or not review[field].strip()
            for field in ("task_name", "signature", "evidence_quote", "finding")
        ):
            raise ValueError("AHE manifest evidence review fields must be nonempty text")
    return payload


def _safe_task_name(task_name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", task_name):
        return task_name
    return "task-" + hashlib.sha256(task_name.encode()).hexdigest()


def _validate_reviewed_evidence(manifest: dict[str, Any], ctx: OperatorContext) -> dict[str, Any]:
    overview_path = ctx.run_dir / "analyze" / "evidence" / "overview.json"
    try:
        overview = json.loads(overview_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("AHE evidence overview is missing or invalid") from exc
    if not isinstance(overview, dict):
        raise ValueError("AHE evidence overview must be a JSON object")
    required = {
        str(signature)
        for signature in overview.get("failure_signatures", [])
        if isinstance(signature, str) and signature
    }
    selected = {
        (str(item.get("task_name")), str(item.get("signature")))
        for item in overview.get("selected_tasks", [])
        if isinstance(item, dict) and item.get("mode") == "debug"
    }
    detail_dir = ctx.run_dir / "analyze" / "analysis" / "detail"
    covered: set[str] = set()
    reviewed_tasks: set[str] = set()
    for review in manifest["evidence_reviews"]:
        task_name = review["task_name"]
        signature = review["signature"]
        if (task_name, signature) not in selected:
            raise ValueError(f"AHE evidence review does not match a selected task: {task_name}")
        detail_path = detail_dir / f"{_safe_task_name(task_name)}.md"
        try:
            detail_path.resolve().relative_to(detail_dir.resolve())
            detail = detail_path.read_text()
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"AHE selected task detail is missing or unsafe: {task_name}") from exc
        quote = review["evidence_quote"]
        if len(quote) < 12 or quote not in detail:
            raise ValueError(f"AHE evidence quote is not present in selected task detail: {task_name}")
        covered.add(signature)
        reviewed_tasks.add(task_name)
    missing = sorted(required - covered)
    if missing:
        raise ValueError("AHE evidence review must cover each selected failure signature: " + ", ".join(missing))
    return {
        "required_failure_signatures": sorted(required),
        "covered_failure_signatures": sorted(covered),
        "reviewed_tasks": sorted(reviewed_tasks),
        "review_count": len(manifest["evidence_reviews"]),
    }


def _read_manifest_file(checkout: Path, genid: str) -> dict[str, Any]:
    path = checkout / MANIFEST_FILE
    try:
        raw = path.read_text()
    except OSError as exc:
        raise ValueError(f"mutate operator must write the AHE manifest file: {MANIFEST_FILE}") from exc
    finally:
        path.unlink(missing_ok=True)
    return _extract_manifest(f"{MANIFEST_START}\n{raw}\n{MANIFEST_END}", genid)


def build_prompt(checkout: Path, observation: str, ctx: OperatorContext) -> str:
    del observation
    template = dict(MANIFEST_TEMPLATE)
    template["iteration"] = ctx.genid
    if runner_name(ctx) == "harbor":
        repository = Path("/app/task/workspace")
        current_run = repository / "runs" / f"gen-{ctx.genid}"
        experiment = repository
    else:
        repository = checkout
        current_run = ctx.run_dir
        experiment = ctx.workspace
    analysis = current_run / "analyze" / "analysis"
    overview = analysis / "overview.md"
    attribution = analysis / "change_evaluation.json"
    details = analysis / "detail"
    cases = current_run / "analyze" / "evidence" / "cases.jsonl"
    rollout = current_run / "rollout"
    if ctx.parent in (None, "0"):
        prior_change = "No selected-parent mutate change exists for this baseline generation."
    else:
        feedback = current_run / "feedback"
        prior_change = (
            f"Read the previous change context: `{feedback / 'last_accepted.diff'}` holds the "
            f"most recent accepted patch, and `{feedback / 'evidence' / 'history.json'}` holds "
            "lineage, scores, and verdicts."
        )
    if (checkout / "target" / "codex.toml").is_file():
        target_prompt = CODEX_AHE_PROMPT
    elif (checkout / "target" / "profile.cordis.yml").is_file():
        target_prompt = DSH_AHE_PROMPT
    else:
        target_prompt = AHE_PROMPT
    writes_manifest = (
        "After checks, before finishing the turn, "
        if target_prompt is DSH_AHE_PROMPT
        else "After checks and before the submission action, "
    )
    submit_clause = (
        "Finish the turn normally; the harness snapshots `target/`."
        if target_prompt is DSH_AHE_PROMPT
        else "Then submit normally."
    )
    return (
        f"{target_prompt.rstrip()}\n\n"
        "# Evidence review before decisions or edits\n\n"
        f"1. Read `{overview}` and list every distinct selected failure signature.\n"
        f"2. For each signature, read at least one matching per-task detail under `{details}`. An exact quote must appear in the final evidence review: copy the sentence character for character from that detail file, with no paraphrase and no added commentary, and put your own reading of it in `finding`. The validator checks that the whole quote string exists in that detail.\n"
        f"3. Use `{cases}` and raw rollout artifacts under `{rollout}` to resolve gaps or conflicts; do not claim evidence you did not inspect.\n"
        f"4. Read `{attribution}` and {prior_change}\n"
        "5. Record the observed evidence, falsifiable hypothesis, expected benefit, regression risk, and a disconfirming observation. Only then choose KEEP, REVISE, or ROLLBACK + PIVOT. If no selected failures exist, use an empty evidence_reviews list and do not invent failure evidence.\n"
        "6. Make any candidate edit only after that review.\n\n"
        "# Evidence Locations\n\n"
        f"Repository: {repository}\n"
        f"Feedback bundle: {current_run / 'feedback'}\n"
        f"Current generation artifacts: {current_run}\n"
        f"Raw trace evidence: {current_run / 'analyze' / 'evidence'}\n\n"
        f"{render_artifact_guidance(ctx, experiment)}\n\n"
        f"{workspace_contract(checkout, ctx.config, action_paths=['target'])}\n\n"
        "# Required Final Output\n\nEdit the candidate directly. "
        f"{writes_manifest}write the following JSON object to `{MANIFEST_FILE}`. Write JSON only; this control file "
        "is removed before the candidate patch is created. Follow this contract exactly: keep these keys, this "
        "nesting, and these value types, replace every example value with the real one, and do not rename, merge, "
        "add, or drop fields. `iteration` must be exactly the string shown above, not a number. If you choose KEEP, "
        "leave `changes` as an empty list and make no edit under `target/`; "
        "that generation records no proposal and keeps the parent. Any edit under `target/` must be covered by at "
        f"least one entry in `changes`. {submit_clause}\n\n"
        f"```json\n{json.dumps(template, indent=2)}\n```\n"
    )


def _merge_usage(total: dict[str, Any], extra: object) -> dict[str, Any]:
    merged = dict(total)
    for key, value in _safe_usage(extra).items():
        previous = merged.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = round(float(previous or 0) + float(value), 6)
        elif key not in merged:
            merged[key] = value
    return merged


def _correction_prompt(prompt: str, error: ValueError) -> str:
    return (
        f"{prompt}\n\n# Change manifest correction\n\n"
        f"Your candidate edit is already in place. The change manifest was rejected: {error}\n"
        f"Rewrite only `{MANIFEST_FILE}` so that it satisfies this check, then finish the turn. "
        "Do not edit `target/` again.\n"
    )


class AheMutate(MutateOperator):
    def mutate(self, checkout: Path, observation: str, ctx: OperatorContext) -> MutateResult:
        parent_ref = patch_parent_ref(checkout, ctx)
        out = ctx.run_dir / "mutate"
        out.mkdir(parents=True, exist_ok=True)
        prompt = build_prompt(checkout, observation, ctx)
        (out / "prompt.md").write_text(prompt)
        retries = int(ctx.config.get("manifest_retries", 1))
        usage: dict[str, Any] = {"usd": 0}
        manifest: dict[str, Any] | None = None
        evidence_audit: dict[str, Any] = {}
        manifest_error: ValueError | None = None
        for attempt in range(1, retries + 2):
            attempt_prompt = prompt if manifest_error is None else _correction_prompt(prompt, manifest_error)
            if attempt > 1:
                (out / f"prompt.retry-{attempt - 1}.md").write_text(attempt_prompt)
            try:
                agent_run = run_agent(checkout, attempt_prompt, ctx)
            except AgentCommandError as exc:
                (out / "output.txt").write_text(exc.output)
                _write_json(out / "usage.json", _merge_usage(usage, exc.usage))
                raise SystemExit(exc.returncode)
            usage = _merge_usage(usage, agent_run.usage)
            (out / "output.txt").write_text(agent_run.output)
            _write_json(out / "usage.json", usage)
            try:
                manifest = _read_manifest_file(checkout, ctx.genid)
                evidence_audit = _validate_reviewed_evidence(manifest, ctx)
            except ValueError as exc:
                manifest = None
                manifest_error = exc
                (out / "manifest_validation_error.txt").write_text(str(exc) + "\n")
                continue
            manifest_error = None
            (out / "manifest_validation_error.txt").unlink(missing_ok=True)
            break
        if manifest is None:
            assert manifest_error is not None
            raise manifest_error

        patch = create_candidate_patch(
            checkout=checkout,
            parent_ref=parent_ref,
            surface=load_surface_policy(checkout),
            repair=False,
        )
        (out / "model_patch.diff").write_text(patch.diff)
        (out / "patch.diff").write_text(patch.diff)
        (out / "output.txt").write_text(agent_run.output)
        _write_json(out / "changed.json", patch.changed_paths)
        _write_json(out / "surface-check.json", patch.surface_report)
        _write_json(out / "usage.json", usage)
        assert manifest is not None
        if not manifest["changes"] and patch.changed_paths:
            error = ValueError("AHE manifest reports no changes but the candidate worktree differs")
            (out / "manifest_validation_error.txt").write_text(str(error) + "\n")
            raise error
        _write_json(out / "change_manifest.json", manifest)
        _write_json(out / "evidence_audit.json", evidence_audit)
        notes = [
            "operator: ahe",
            f"runner: {runner_name(ctx)}",
            "change-manifest: parsed",
            "written-by: operators/mutate.py",
            *patch.notes,
        ]
        (out / "rationale.md").write_text("\n".join(notes) + "\n")
        return MutateResult(changed=patch.changed_paths, notes=notes, usage=usage)


if __name__ == "__main__":
    sdk.main(AheMutate, config_schema=CONFIG)
