from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

SAFE_ARTIFACTS = (
    "agent/mini-swe-agent.trajectory.json",
    "agent/mini-swe-agent.txt",
    "agent/trajectory.json",
    # Structured, verifier-owned evidence for artifact-producing evaluations.
    # Keep this explicit allowlist: arbitrary verifier files may contain secrets.
    "verifier/diagnostics.json",
    "verifier/evaluation.json",
    "verifier/judge.json",
    "verifier/poster.svg",
    "verifier/poster.png",
    "evolve-replay.json",
)
_CANDIDATE_MARKER = "EVOLVE_CANDIDATE_INVALID:"
_MISSING_TOOL_OUTPUT = "No tool output found for function call"
_VERIFIER_UV_RESOLUTION_ERROR = "No solution found when resolving tool dependencies:"
_VERIFIER_UV_OFFLINE_HINTS = (
    "was not found in the cache",
    "Packages were unavailable because the network was disabled",
)
_VERIFIER_UV_DOWNLOAD_ERROR = "error: Failed to download:"
_VERIFIER_UV_REQUEST_ERROR = "Caused by: Request failed"
_VERIFIER_CURL_SERVER_ERROR = re.compile(r"curl: \(\d+\) The requested URL returned error: 5\d\d\b")
_SAFE_ERROR_CODE = re.compile(r"[a-z0-9_]+")
_SENSITIVE_ENV_NAME = re.compile(r"(?i)(?:proxy|api[_-]?key|access[_-]?token|token|secret|password|authorization|auth)")

# Canonical, verifier-independent evidence carried across the certified boundary.
# The shape is stable for every benchmark; format-specific parsers only map into it.
VERIFIER_EVIDENCE_SCHEMA = "verifier-evidence-v1"
_EVIDENCE_MAX_REPORT_FILES = 64
_EVIDENCE_MAX_REPORT_BYTES = 4_000_000
_EVIDENCE_MAX_TESTS = 2_000
_EVIDENCE_MAX_FAILURES = 50
_EVIDENCE_MAX_MESSAGE = 1_000
_EVIDENCE_MAX_REWARD_FIELDS = 64
_EVIDENCE_TIMING_PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")
_STATUS_RANK = {"passed": 0, "skipped": 1, "pending": 2, "other": 3, "error": 4, "failed": 5}


def _redact_environment_values(text: str, environment: Mapping[str, str] | None = None) -> str:
    configured = os.environ if environment is None else environment
    values = {value for name, value in configured.items() if _SENSITIVE_ENV_NAME.search(name) and len(value) >= 8}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


def candidate_error_code(exception_info: object) -> str | None:
    if not isinstance(exception_info, dict):
        return None
    message = str(exception_info.get("exception_message") or "")
    if _MISSING_TOOL_OUTPUT in message:
        return "invalid_tool_history"
    if _CANDIDATE_MARKER not in message:
        return None
    code = message.partition(_CANDIDATE_MARKER)[2].strip().splitlines()[0].strip()
    return code if _SAFE_ERROR_CODE.fullmatch(code) else "candidate_invalid"


def _jobs_roots(jobs_dirs: Path | list[Path] | tuple[Path, ...]) -> list[Path]:
    if isinstance(jobs_dirs, Path):
        return [jobs_dirs]
    return [Path(path) for path in jobs_dirs]


def collect_harbor_artifacts(
    jobs_dirs: Path | list[Path] | tuple[Path, ...], *, reward_schema: str = "scalar"
) -> tuple[dict[str, Any], dict[str, Any], list[float]]:
    roots = _jobs_roots(jobs_dirs)
    trials = _load_task_trials(roots, reward_schema=reward_schema)
    return _build_task_vector(trials), _build_artifact_index(roots, trials), _scoring_rewards(trials)


def write_harbor_artifacts(
    jobs_dirs: Path | list[Path] | tuple[Path, ...], run_dir: Path, *, reward_schema: str = "scalar"
) -> list[float]:
    roots = _jobs_roots(jobs_dirs)
    for root in roots:
        _write_verifier_diagnostics(root)
        _write_replay_envelopes(root, reward_schema=reward_schema)
    task_vector, artifact_index, rewards = collect_harbor_artifacts(roots, reward_schema=reward_schema)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task_vector.json").write_text(json.dumps(task_vector, indent=2, sort_keys=True) + "\n")
    (run_dir / "evaluation_artifacts.json").write_text(json.dumps(artifact_index, indent=2, sort_keys=True) + "\n")
    (run_dir / "cost.json").write_text(
        json.dumps({"usd": sum(float(trial["cost_usd"]) for trial in artifact_index["trials"])}, sort_keys=True) + "\n"
    )
    return rewards


def _write_replay_envelopes(jobs_dir: Path, *, reward_schema: str = "scalar") -> None:
    """Project raw Harbor results onto the only verifier-safe replay boundary."""
    for result_path in sorted(jobs_dir.rglob("result.json")):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict):
            continue
        task_name, trial_name = result.get("task_name"), result.get("trial_name")
        if not isinstance(task_name, str) or not isinstance(trial_name, str):
            continue
        envelope: dict[str, Any] = {"schema_version": 2, "task_name": task_name, "trial_name": trial_name}
        rewards = _project_rewards(result, reward_schema)
        if rewards:
            envelope["verifier_result"] = {"rewards": rewards}
        envelope["verifier_evidence"] = _verifier_evidence(result_path.parent, result, rewards)
        exception = result.get("exception_info")
        if isinstance(exception, dict):
            safe_exception = {
                "exception_type": _safe_label(exception.get("exception_type")),
                "exception_message": _exception_message(exception.get("exception_message")),
            }
            envelope["exception_info"] = {key: value for key, value in safe_exception.items() if value is not None}
        agent = result.get("agent_result")
        if isinstance(agent, dict):
            safe_agent = {
                key: value
                for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")
                if (value := _safe_number(agent.get(key))) is not None
            }
            if safe_agent:
                envelope["agent_result"] = safe_agent
        (result_path.parent / "evolve-replay.json").write_text(json.dumps(envelope, sort_keys=True) + "\n")


def _project_rewards(result: dict[str, Any], reward_schema: str) -> dict[str, Any]:
    """Return every safe scalar reward field, not just the aggregate reward.

    Benchmarks describe their score with different field names. Carrying all
    scalar fields through keeps the aggregate reward for scoring while giving
    downstream analysis the benchmark's own breakdown (for example f2p/p2p).
    """
    verifier_result = result.get("verifier_result")
    rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
    if not isinstance(rewards, dict):
        return {}
    if reward_schema == "reviewbench" and _reviewbench_overall(rewards) is None:
        return {}
    projected: dict[str, Any] = {}
    for key, value in list(rewards.items())[:_EVIDENCE_MAX_REWARD_FIELDS]:
        safe_key = _safe_label(str(key), limit=120)
        if safe_key is None:
            continue
        if isinstance(value, bool):
            projected[safe_key] = value
        elif isinstance(value, (int, float)) and math.isfinite(float(value)):
            projected[safe_key] = value
    return projected


def _safe_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _redact_environment_values(value.strip())[:limit]


def _split_category(name: str) -> tuple[str | None, str]:
    match = re.match(r"^\[([A-Za-z0-9_.:-]{1,40})\]\s+(.*)$", name)
    if match is None:
        return None, name
    category, rest = match.group(1), match.group(2).strip()
    return (category, rest) if rest else (None, name)


def _read_report_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            payload = handle.read(_EVIDENCE_MAX_REPORT_BYTES)
    except OSError:
        return None
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _read_report_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(_EVIDENCE_MAX_REPORT_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _looks_like_ctrf(payload: dict[str, Any]) -> bool:
    if payload.get("reportFormat") == "CTRF":
        return True
    results = payload.get("results")
    return isinstance(results, dict) and ("tests" in results or "summary" in results)


def _ctrf_records(payload: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    results = payload.get("results")
    if not isinstance(results, dict):
        return {}, []
    summary = results.get("summary")
    tests = results.get("tests")
    if not isinstance(tests, list):
        counts: dict[str, int] = {}
        if isinstance(summary, dict):
            mapping = {
                "tests": "total",
                "passed": "passed",
                "failed": "failed",
                "skipped": "skipped",
                "pending": "pending",
                "other": "other",
            }
            for source, target in mapping.items():
                number = _safe_number(summary.get(source))
                if number is not None:
                    counts[target] = int(number)
        return counts, []
    records: list[dict[str, Any]] = []
    for test in tests[:_EVIDENCE_MAX_TESTS]:
        if not isinstance(test, dict):
            continue
        name = _safe_text(test.get("name"), 240)
        if not name:
            continue
        category, name = _split_category(name)
        status = str(test.get("status") or "other").strip().lower()
        if status not in _STATUS_RANK:
            status = "other"
        message = _safe_text(test.get("message") or test.get("trace") or "", _EVIDENCE_MAX_MESSAGE)
        records.append({"id": name, "category": category, "status": status, "message": message})
    return {}, records


def _junit_records(text: str) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(text)
    except (ElementTree.ParseError, ValueError):
        return []
    records: list[dict[str, Any]] = []
    for case in root.iter("testcase"):
        name = _safe_text(case.get("name") or "", 240) or "unknown"
        classname = _safe_text(case.get("classname") or "", 120) or None
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if failure is not None:
            status, node = "failed", failure
        elif error is not None:
            status, node = "error", error
        elif skipped is not None:
            status, node = "skipped", None
        else:
            status, node = "passed", None
        message = _safe_text(
            (node.get("message") or node.text or "") if node is not None else "", _EVIDENCE_MAX_MESSAGE
        )
        records.append({"id": name, "category": classname, "status": status, "message": message})
    return records


def _tap_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    pattern = re.compile(r"^(not ok|ok)\s+\d+\s*(?:-\s*)?(.+?)\s*$")
    for line in text.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        body = _safe_text(match.group(2), 240)
        name = _safe_text(re.split(r"\s+#\s+", body, maxsplit=1)[0], 240)
        if not name:
            continue
        status = "failed" if match.group(1) == "not ok" else "passed"
        if re.search(r"(?i)#\s*skip", body):
            status = "skipped"
        records.append({"id": name, "category": None, "status": status, "message": ""})
    return records


def _merge_records(target: dict[tuple[str, str], dict[str, Any]], records: list[dict[str, Any]]) -> None:
    for record in records:
        key = (str(record.get("category") or ""), str(record.get("id") or ""))
        current = target.get(key)
        if current is None or _STATUS_RANK.get(str(record.get("status")), 3) > _STATUS_RANK.get(
            str(current.get("status")), 3
        ):
            target[key] = record


def _counts_from_records(records: list[dict[str, Any]]) -> dict[str, int]:
    total = len(records)
    passed = sum(record["status"] == "passed" for record in records)
    skipped = sum(record["status"] in {"skipped", "pending"} for record in records)
    counts = {"total": total, "passed": passed, "failed": total - passed - skipped}
    if skipped:
        counts["skipped"] = skipped
    return counts


def _failure_records(records: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    failures = [record for record in records.values() if record["status"] not in {"passed", "skipped", "pending"}]
    failures.sort(
        key=lambda record: (-_STATUS_RANK.get(str(record["status"]), 3), record["category"] or "", record["id"])
    )
    return failures[:_EVIDENCE_MAX_FAILURES]


def _report_files(verifier: Path) -> list[Path]:
    """Return report files, primary ones (directly under verifier/) first."""
    paths = [
        path
        for path in sorted(verifier.rglob("*"))
        if path.is_file() and path.suffix.lower() in {".json", ".xml", ".tap"}
    ]
    paths.sort(key=lambda path: (len(path.relative_to(verifier).parts) > 1, path.name != "ctrf.json", path.as_posix()))
    return paths[:_EVIDENCE_MAX_REPORT_FILES]


def _parse_report_file(path: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = _read_report_json(path)
        if payload is None or not _looks_like_ctrf(payload):
            return {}, []
        return _ctrf_records(payload)
    if suffix == ".xml":
        return {}, _junit_records(_read_report_text(path))
    return {}, _tap_records(_read_report_text(path))


def _read_json_document(path: Path) -> object:
    try:
        with path.open("rb") as handle:
            payload = handle.read(_EVIDENCE_MAX_REPORT_BYTES)
    except OSError:
        return None
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _bounded_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures.sort(
        key=lambda record: (
            -_STATUS_RANK.get(str(record.get("status")), 3),
            str(record.get("category") or ""),
            str(record.get("id") or ""),
        )
    )
    return failures[:_EVIDENCE_MAX_FAILURES]


def _sweatlas_evidence(verifier: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Map SWE-Atlas ``evaluation_results.json`` onto the canonical evidence shape.

    ``counts`` covers every check the verifier reports (baseline tests, gates,
    rubrics, mutation no-ops), not just test cases, so it stays consistent with
    the ``failures`` list. The mutation run is *expected* to fail the relevant
    tests, so only mutation no-ops are failures — a plain FAILED status there is
    correct behaviour.
    """
    payload = _read_report_json(verifier / "evaluation_results.json")
    if payload is None:
        return {}, []
    mutation = payload.get("mutation_testing")
    mutation = mutation if isinstance(mutation, dict) else {}
    failures: list[dict[str, Any]] = []
    total = passed = 0
    baseline = mutation.get("baseline")
    tests = baseline.get("tests") if isinstance(baseline, dict) else None
    baseline_total = baseline_passed = 0
    if isinstance(tests, list):
        for test in tests[:_EVIDENCE_MAX_TESTS]:
            if not isinstance(test, dict):
                continue
            name = _safe_text(test.get("name"), 240)
            if not name:
                continue
            baseline_total += 1
            status = str(test.get("status") or "").strip()
            if status.upper() == "PASSED":
                baseline_passed += 1
            else:
                failures.append(
                    {"id": name, "category": "baseline", "status": "failed", "message": _safe_text(status, 120)}
                )
    if baseline_total == 0:
        total_number = _safe_number(mutation.get("baseline_relevant_total_count"))
        passed_number = _safe_number(mutation.get("baseline_relevant_passed_count"))
        if total_number is not None and passed_number is not None:
            baseline_total, baseline_passed = int(total_number), int(passed_number)
    total += baseline_total
    passed += baseline_passed
    not_failed = mutation.get("mutation_not_failed_relevant_tests")
    if isinstance(not_failed, list):
        for item in not_failed[:_EVIDENCE_MAX_TESTS]:
            name = _safe_text(item, 240)
            if not name:
                continue
            total += 1
            failures.append(
                {
                    "id": name,
                    "category": "mutation",
                    "status": "failed",
                    "message": "relevant test did not fail after mutation",
                }
            )
    for gate in ("overall", "manifest", "mutation", "rubrics"):
        value = payload.get(f"{gate}_pass")
        if value is True:
            total += 1
            passed += 1
        elif value is False:
            total += 1
            failures.append({"id": gate, "category": "gate", "status": "failed", "message": f"{gate} gate failed"})
    rubrics = payload.get("rubrics")
    rubric_scores = rubrics.get("rubric_scores") if isinstance(rubrics, dict) else None
    if isinstance(rubric_scores, list):
        for rubric in rubric_scores[:_EVIDENCE_MAX_TESTS]:
            if not isinstance(rubric, dict) or not isinstance(rubric.get("passes"), bool):
                continue
            total += 1
            if rubric["passes"]:
                passed += 1
                continue
            title = _safe_text(rubric.get("title"), 240) or _safe_text(rubric.get("id"), 80) or "rubric"
            score = rubric.get("score")
            justification = _safe_text(score.get("justification"), 400) if isinstance(score, dict) else ""
            message = f"{title}: {justification}" if justification else title
            failures.append(
                {
                    "id": title,
                    "category": "rubric",
                    "status": "failed",
                    "message": message[:_EVIDENCE_MAX_MESSAGE],
                }
            )
    counts = {"total": total, "passed": passed, "failed": max(0, total - passed)} if total else {}
    return counts, _bounded_failures(failures)


def _reviewbench_evidence(verifier: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Map ReviewBench ``scores.details.json`` candidate findings onto the shape.

    ``novel_tp`` (an unmatched candidate the judge classified as a true
    positive) *raises* the augmented F1 in ``scorer.ts``, so it counts as
    passed alongside ``matched_tp``; only the false-positive statuses are
    failures.
    """
    document = _read_json_document(verifier / "scores.details.json")
    if not isinstance(document, list):
        return {}, []
    total = passed = 0
    failures: list[dict[str, Any]] = []
    for pull_request in document[:64]:
        findings = pull_request.get("candidate_findings") if isinstance(pull_request, dict) else None
        if not isinstance(findings, list):
            continue
        for finding in findings[:_EVIDENCE_MAX_TESTS]:
            if not isinstance(finding, dict):
                continue
            status = str(finding.get("status") or "").strip().lower()
            if not status:
                continue
            total += 1
            if status in {"matched_tp", "novel_tp"}:
                passed += 1
                continue
            location = _safe_text(finding.get("file"), 240)
            start = _safe_number(finding.get("start_line"))
            end = _safe_number(finding.get("end_line"))
            identifier = location or f"finding-{total}"
            if location and start is not None:
                suffix = f":{int(start)}"
                if end is not None and int(end) != int(start):
                    suffix += f"-{int(end)}"
                identifier = f"{location}{suffix}"
            failures.append(
                {
                    "id": identifier,
                    "category": _safe_text(finding.get("result_category"), 40) or None,
                    "status": "failed",
                    "message": _safe_text(finding.get("message"), _EVIDENCE_MAX_MESSAGE),
                }
            )
    counts = {"total": total, "passed": passed, "failed": total - passed} if total else {}
    return counts, _bounded_failures(failures)


_STRUCTURED_EVIDENCE_ADAPTERS = (
    ("evaluation_results.json", _sweatlas_evidence),
    ("scores.details.json", _reviewbench_evidence),
)


def _report_evidence(trial_dir: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Parse standard test reports (CTRF, JUnit XML, TAP) into one test view.

    Reports directly under ``verifier/`` describe the scored test set; files in
    subdirectories are typically broader baseline/post-change dumps. Prefer the
    primary group so counts stay aligned with the reward semantics, and fall back
    to nested reports only when the primary group has nothing to say.

    Within a group, reports merge by test identity with the worst status winning,
    which handles frameworks that split baseline and new-state reports without
    double-counting shared tests.

    Benchmarks that predate standard report formats may instead emit a
    verifier-owned structured document; the adapters below map those documents
    into the same shape. Standard reports always take precedence.
    """
    verifier = trial_dir / "verifier"
    if not verifier.is_dir():
        return {}, []
    fallback_counts: dict[str, int] = {}
    for primary in (True, False):
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for path in _report_files(verifier):
            if (len(path.relative_to(verifier).parts) <= 1) != primary:
                continue
            counts, records = _parse_report_file(path)
            if counts and not fallback_counts:
                fallback_counts = counts
            _merge_records(merged, records)
        if merged:
            return _counts_from_records(list(merged.values())), _failure_records(merged)
    for name, adapter in _STRUCTURED_EVIDENCE_ADAPTERS:
        if (verifier / name).is_file():
            counts, failures = adapter(verifier)
            if counts or failures:
                return counts, failures
    return fallback_counts, []


def _duration_seconds(value: object) -> float | None:
    if not isinstance(value, dict):
        return None
    for key in ("duration_s", "duration", "elapsed_s", "elapsed"):
        number = _safe_number(value.get(key))
        if number is not None:
            return round(float(number), 3)
    try:
        start = datetime.fromisoformat(str(value["started_at"]).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(value["finished_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return round((finish - start).total_seconds(), 3)


def _trial_timing(result: dict[str, Any]) -> dict[str, float]:
    timing: dict[str, float] = {}
    for phase in _EVIDENCE_TIMING_PHASES:
        seconds = _duration_seconds(result.get(phase))
        if seconds is not None:
            timing[phase] = seconds
    return timing


def _evidence_status(result: dict[str, Any], rewards: dict[str, Any]) -> str:
    """Classify the verifier run itself, independent of the evidence shape.

    Benchmark schemas that score without a ``reward`` key (ReviewBench encodes
    its score as validated counts) still count as scored.
    """
    reward = rewards.get("reward")
    if isinstance(reward, (int, float)) and not isinstance(reward, bool) and math.isfinite(float(reward)):
        return "scored"
    if _reviewbench_overall(rewards) is not None:
        return "scored"
    exception = result.get("exception_info")
    if isinstance(exception, dict):
        text = f"{exception.get('exception_type') or ''} {exception.get('exception_message') or ''}".lower()
        if "timeout" in text:
            return "timeout"
        if exception.get("exception_type"):
            return "error"
    return "unscored"


def _verifier_evidence(trial_dir: Path, result: dict[str, Any], rewards: dict[str, Any]) -> dict[str, Any]:
    counts, failures = _report_evidence(trial_dir)
    timing = _trial_timing(result)
    missing: list[str] = []
    if not rewards:
        missing.append("rewards")
    if not counts and not failures:
        missing.append("detail")
    if not timing:
        missing.append("timing")
    if not missing:
        completeness = "full"
    elif len(missing) == 3:
        completeness = "missing"
    else:
        completeness = "partial"
    return {
        "schema_version": VERIFIER_EVIDENCE_SCHEMA,
        "status": _evidence_status(result, rewards),
        "counts": counts,
        "failures": failures,
        "timing": timing,
        "completeness": completeness,
        "missing": missing,
    }


def _safe_number(value: object) -> int | float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    # ``json.loads`` accepts ``Infinity``/``NaN`` by default; downstream
    # ``int(value)`` would raise on either, so drop them here.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _safe_label(value: object, limit: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    text = _redact_environment_values(value)
    return text if len(text) <= limit and re.fullmatch(r"[A-Za-z0-9_.:-]+", text) else None


def _safe_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value[:20] if (text := _safe_label(item)) is not None]


def _safe_numeric_map(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int | float] = {}
    for key, item in value.items():
        number = _safe_number(item)
        safe_key = _safe_label(str(key))
        if number is not None and safe_key is not None:
            result[safe_key] = number
    return result


def _verifier_diagnostics(verifier_result: object) -> dict[str, Any] | None:
    if not isinstance(verifier_result, dict):
        return None
    diagnostics: dict[str, Any] = {"schema_version": 1}
    for key in ("reward",):
        if (number := _safe_number(verifier_result.get(key))) is not None:
            diagnostics[key] = number
    for key in ("status", "termination_reason"):
        if (text := _safe_label(verifier_result.get(key))) is not None:
            diagnostics[key] = text
    if reward_basis := _safe_text_list(verifier_result.get("reward_basis")):
        diagnostics["reward_basis"] = reward_basis

    reward_info = verifier_result.get("reward_info")
    if isinstance(reward_info, dict):
        safe_reward_info: dict[str, Any] = {}
        action_checks = reward_info.get("action_checks")
        if isinstance(action_checks, list):
            safe_checks: list[dict[str, Any]] = []
            for check in action_checks[:50]:
                if not isinstance(check, dict):
                    continue
                safe_check: dict[str, Any] = {}
                if isinstance(check.get("action_match"), bool):
                    safe_check["action_match"] = check["action_match"]
                if (number := _safe_number(check.get("action_reward"))) is not None:
                    safe_check["action_reward"] = number
                if (text := _safe_label(check.get("tool_type"))) is not None:
                    safe_check["tool_type"] = text
                if safe_check:
                    safe_checks.append(safe_check)
            if safe_checks:
                safe_reward_info["action_checks"] = safe_checks
        db_check = reward_info.get("db_check")
        if isinstance(db_check, dict):
            safe_db_check: dict[str, Any] = {}
            if isinstance(db_check.get("db_match"), bool):
                safe_db_check["db_match"] = db_check["db_match"]
            if (number := _safe_number(db_check.get("db_reward"))) is not None:
                safe_db_check["db_reward"] = number
            if safe_db_check:
                safe_reward_info["db_check"] = safe_db_check
        if (number := _safe_number(reward_info.get("reward"))) is not None:
            safe_reward_info["reward"] = number
        if reward_basis := _safe_text_list(reward_info.get("reward_basis")):
            safe_reward_info["reward_basis"] = reward_basis
        if reward_breakdown := _safe_numeric_map(reward_info.get("reward_breakdown")):
            safe_reward_info["reward_breakdown"] = reward_breakdown
        if safe_reward_info:
            diagnostics["reward_info"] = safe_reward_info

    runtime = verifier_result.get("runtime_initialization")
    if isinstance(runtime, dict):
        safe_runtime: dict[str, Any] = {}
        accepted = runtime.get("accepted")
        if isinstance(accepted, dict):
            safe_accepted: dict[str, Any] = {}
            for key in ("max_errors", "max_steps"):
                if (number := _safe_number(accepted.get(key))) is not None:
                    safe_accepted[key] = number
            if "seed" in accepted and (
                (seed := _safe_number(accepted.get("seed"))) is not None or accepted["seed"] is None
            ):
                safe_accepted["seed"] = seed
            if safe_accepted:
                safe_runtime["accepted"] = safe_accepted
        for key in (
            "accepted_event_ordinal",
            "accepted_once_count",
            "event_ordinal",
            "idempotent_replay_count",
            "schema_version",
            "start_event_ordinal",
        ):
            if (number := _safe_number(runtime.get(key))) is not None:
                safe_runtime[key] = number
        if (text := _safe_label(runtime.get("phase"))) is not None:
            safe_runtime["phase"] = text
        if rejected := _safe_numeric_map(runtime.get("rejected_mutations")):
            safe_runtime["rejected_mutations"] = rejected
        if safe_runtime:
            diagnostics["runtime_initialization"] = safe_runtime

    return diagnostics if len(diagnostics) > 1 else None


def _write_verifier_diagnostics(jobs_dir: Path) -> None:
    for result_path in sorted(jobs_dir.rglob("result.json")):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict) or not result.get("task_name") or not result.get("trial_name"):
            continue
        verifier_path = result_path.parent / "verifier" / "result.json"
        try:
            verifier_result = json.loads(verifier_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        diagnostics = _verifier_diagnostics(verifier_result)
        if diagnostics is None:
            continue
        output = result_path.parent / "verifier" / "diagnostics.json"
        output.write_text(json.dumps(diagnostics, indent=2, sort_keys=True) + "\n")


_REVIEWBENCH_COUNT_FIELDS = (
    "candidate_count",
    "golden_tp_count",
    "matched_count",
    "matched_tp_count",
    "unmatched_count",
    "unmatched_tp_count",
    "unmatched_fp_count",
    "golden_covered_count",
)


def _reviewbench_overall(reward: object) -> dict[str, int] | None:
    """Validate ReviewBench counts and return the ``overall`` stratum."""
    if not isinstance(reward, dict) or reward.get("rb_schema") != 1:
        return None
    counts: dict[str, int] = {}
    for field in _REVIEWBENCH_COUNT_FIELDS:
        value = reward.get(f"overall.{field}")
        if type(value) not in (int, float) or not 0 <= value <= 2**53 - 1 or value != int(value):
            return None
        counts[field] = int(value)
    if (
        counts["candidate_count"] != counts["matched_count"] + counts["unmatched_count"]
        or counts["unmatched_count"] != counts["unmatched_tp_count"] + counts["unmatched_fp_count"]
        or counts["matched_tp_count"] > counts["matched_count"]
        or counts["golden_covered_count"] > counts["golden_tp_count"]
    ):
        return None
    return counts


def _reviewbench_reward(result: dict[str, Any]) -> dict[str, Any] | None:
    rewards = (result.get("verifier_result") or {}).get("rewards")
    return rewards if _reviewbench_overall(rewards) is not None else None


def _reviewbench_f1(counts: dict[str, int]) -> float | None:
    """Mirror ReviewBench's augmented F1; None means the PR is undefined."""
    precision_denominator = counts["candidate_count"]
    recall_denominator = counts["golden_tp_count"] + counts["unmatched_tp_count"]
    precision = (
        (counts["matched_tp_count"] + counts["unmatched_tp_count"]) / precision_denominator
        if precision_denominator
        else None
    )
    recall = (
        (counts["golden_covered_count"] + counts["unmatched_tp_count"]) / recall_denominator
        if recall_denominator
        else None
    )
    if precision is None or recall is None:
        return None
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _uses_reviewbench_metric(result: dict[str, Any], reward_schema: str) -> bool:
    task_name = result.get("task_name")
    return reward_schema == "reviewbench" or (
        reward_schema == "multibench" and isinstance(task_name, str) and task_source(task_name) == "reviewbench"
    )


def _has_scoring_reward(result: dict[str, Any], reward_schema: str) -> bool:
    if _uses_reviewbench_metric(result, reward_schema):
        return _reviewbench_reward(result) is not None
    return (_reward(result) or 0.0) > 0.0


def _impute_reviewbench_rewards(trials: list[dict[str, Any]], *, reviewbench_only: bool = False) -> None:
    """Make RSIHub's mean equal ReviewBench's macro augmented F1.

    Undefined PRs are replaced by the mean of the defined PRs in the same
    evaluation.  In a combined benchmark, only ReviewBench tasks participate in
    this imputation; the other source scores keep their native rewards.
    """
    selected = [
        trial for trial in trials if not reviewbench_only or task_source(str(trial["task_name"])) == "reviewbench"
    ]
    completed = [trial for trial in selected if trial["status"] == "benchmark_complete"]
    defined = [float(trial["reward"]) for trial in completed if trial["reward"] is not None]
    if not defined:
        for trial in completed:
            trial["status"] = "infrastructure_failed"
            trial["reward"] = None
            trial["owner"] = "evaluator"
            trial["exception_type"] = "ReviewBenchUndefinedMetric"
            trial["exception_message"] = "no PR has a defined augmented F1"
        return
    mean = sum(defined) / len(defined)
    for trial in completed:
        if trial["reward"] is None:
            trial["reward"] = mean
            trial["reward_imputed"] = True


FINAL_TRIAL_STATUSES = frozenset({"benchmark_complete", "timeout", "candidate_invalid"})


_MULTIBENCH_SOURCES = frozenset({"deep_swe", "reviewbench", "swe_atlas"})


def task_source(task_name: str) -> str | None:
    source, separator, _ = task_name.partition("__")
    return source if separator and source in _MULTIBENCH_SOURCES else None


def is_official_reviewbench_task(task_name: str) -> bool:
    return task_name.startswith("reviewbench__official__")


def _load_task_trials(
    jobs_dirs: Path | list[Path] | tuple[Path, ...], *, reward_schema: str = "scalar"
) -> list[dict[str, Any]]:
    roots = _jobs_roots(jobs_dirs)
    trials: list[dict[str, Any]] = []
    for root_index, jobs_dir in enumerate(roots):
        trials.extend(_load_root_trials(jobs_dir, reward_schema=reward_schema, root_index=root_index))
    if len(roots) > 1:
        trials = _merge_resumed_trials(trials)
    if reward_schema == "reviewbench":
        _impute_reviewbench_rewards(trials)
    elif reward_schema == "multibench":
        _impute_reviewbench_rewards(trials, reviewbench_only=True)
    return sorted(trials, key=lambda trial: (str(trial["task_name"]), str(trial["trial_name"])))


def _load_root_trials(jobs_dir: Path, *, reward_schema: str, root_index: int) -> list[dict[str, Any]]:
    if not jobs_dir.exists():
        return []
    verifier_timeout_is_final_zero = _verifier_timeout_is_final_zero(jobs_dir)
    trials: list[dict[str, Any]] = []
    for result_path in sorted(jobs_dir.rglob("result.json")):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict):
            continue
        task_name = result.get("task_name")
        trial_name = result.get("trial_name")
        if not isinstance(task_name, str) or not isinstance(trial_name, str):
            continue
        trial_dir = result_path.parent
        verifier_dependency_error = (
            None if _has_scoring_reward(result, reward_schema) else _verifier_dependency_error(trial_dir)
        )
        status, reward, owner = _trial_result(
            result,
            verifier_timeout_is_final_zero=verifier_timeout_is_final_zero,
            verifier_dependency_error=verifier_dependency_error,
            reward_schema=reward_schema,
        )
        if status == "timeout" and owner == "benchmark_agent" and _network_only_timeout(trial_dir):
            status, reward, owner = "infrastructure_failed", None, "agent_runtime"
        exception_info = result.get("exception_info")
        exception_type = None
        exception_message = None
        if isinstance(exception_info, dict):
            raw_type = exception_info.get("exception_type")
            raw_message = exception_info.get("exception_message")
            exception_type = str(raw_type) if raw_type else None
            exception_message = _exception_message(raw_message)
        if exception_type is None and verifier_dependency_error:
            exception_type = "VerifierDependencyError"
            exception_message = verifier_dependency_error
        trials.append(
            {
                "task_name": task_name,
                "trial_name": trial_name,
                "status": status,
                "reward": reward,
                "owner": owner,
                "exception_type": exception_type,
                "exception_message": exception_message,
                "cost_usd": _cost_usd(result),
                "trial_dir": trial_dir,
                "jobs_root": jobs_dir,
                "root_index": root_index,
                "started_at": str(result.get("started_at") or ""),
                "artifacts": _safe_artifacts(jobs_dir, trial_dir),
            }
        )
    return trials


def _merged_trial_order(trial: dict[str, Any]) -> tuple[int, str, str]:
    return (int(trial["root_index"]), str(trial["started_at"]), str(trial["trial_name"]))


def _merge_resumed_trials(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine evidence from earlier attempts with the trials of this one.

    A task keeps at most its configured number of trials.  Final trials are
    preferred; when fewer than the configured number exist, the most recent
    unfinished trials are retained so the evidence still explains the gap.
    """
    attempts = _planned_attempts(trials)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trial in trials:
        grouped.setdefault(str(trial["task_name"]), []).append(trial)
    merged: list[dict[str, Any]] = []
    for entries in grouped.values():
        final = sorted((entry for entry in entries if entry["status"] in FINAL_TRIAL_STATUSES), key=_merged_trial_order)
        pending = sorted(
            (entry for entry in entries if entry["status"] not in FINAL_TRIAL_STATUSES), key=_merged_trial_order
        )
        kept = final[-attempts:] if len(final) > attempts else list(final)
        remaining = attempts - len(kept)
        if remaining > 0:
            kept.extend(pending[-remaining:])
        merged.extend(kept)
    return merged


def _planned_attempts(trials: list[dict[str, Any]]) -> int:
    """Resolve the configured per-task trial count from the attempt run plan."""
    for trial in trials:
        root = trial.get("jobs_root")
        if not isinstance(root, Path):
            continue
        try:
            payload = json.loads((root.parent / "run-plan.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            attempts = payload.get("attempts_per_task")
            if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 1:
                return attempts
    return 1


def _build_task_vector(trials: list[dict[str, Any]]) -> dict[str, Any]:
    tasks: dict[str, list[dict[str, Any]]] = {}
    for entry in trials:
        tasks.setdefault(str(entry["task_name"]), []).append(entry)
    serialized_tasks: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for task_name, task_trials in sorted(tasks.items()):
        serialized_trials: list[dict[str, Any]] = []
        for index, entry in enumerate(sorted(task_trials, key=lambda trial: str(trial["trial_name"]))):
            serialized = {
                "trial": index,
                "status": entry["status"],
                "reward": entry["reward"],
                "owner": entry["owner"],
            }
            if entry["exception_type"] is not None:
                serialized["exception_type"] = entry["exception_type"]
            if entry["exception_message"] is not None:
                serialized["exception_message"] = entry["exception_message"]
            if entry.get("reward_imputed"):
                serialized["reward_imputed"] = True
            serialized_trials.append(serialized)
        serialized_tasks[task_name] = {"trials": serialized_trials}
    return {"schema_version": 1, "tasks": serialized_tasks}


def _build_artifact_index(jobs_dirs: list[Path], trials: list[dict[str, Any]]) -> dict[str, Any]:
    index: dict[str, Any] = {
        "jobs_dir": str(jobs_dirs[-1].resolve()),
        "trials": [
            {
                "task_name": entry["task_name"],
                "trial_name": entry["trial_name"],
                "cost_usd": entry["cost_usd"],
                "files": entry["artifacts"],
            }
            for entry in trials
        ],
    }
    if len(jobs_dirs) > 1:
        index["jobs_roots"] = [str(root.resolve()) for root in jobs_dirs]
    return index


def _scoring_rewards(trials: list[dict[str, Any]]) -> list[float]:
    return [float(entry["reward"]) for entry in trials if entry["reward"] is not None]


def _reward(result: dict[str, Any]) -> float | None:
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    return float(reward) if isinstance(reward, (int, float)) and not isinstance(reward, bool) else None


def _network_only_timeout(trial_dir: Path) -> bool:
    """Require retained input-only trajectory plus repeated transport errors."""
    try:
        trajectory = json.loads((trial_dir / "agent/trajectory.json").read_text())
        steps = trajectory.get("steps")
        if not isinstance(steps, list) or not steps:
            return False
        if any(not isinstance(step, dict) or step.get("source") not in {"system", "user"} for step in steps):
            return False
        with (trial_dir / "agent/codex.txt").open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 262144))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, ValueError, AttributeError):
        return False
    errors = 0
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "error"
            and str(event.get("message", "")).startswith("Reconnecting... waiting for network (Connection failed:")
        ):
            errors += 1
    return errors >= 2


def _verifier_timeout_is_final_zero(jobs_dir: Path) -> bool:
    root_config = jobs_dir / "config.json"
    configs = [root_config] if root_config.is_file() else []
    if not configs:
        configs = [path for path in jobs_dir.glob("*/config.json") if path.parent.parent == jobs_dir]
    if len(configs) != 1:
        return False
    try:
        payload = json.loads(configs[0].read_text())
        retry = payload.get("retry") if isinstance(payload, dict) else None
        excluded = set((retry or {}).get("exclude_exceptions") or [])
        return int((retry or {}).get("max_retries") or 0) >= 1 and "VerifierTimeoutError" not in excluded
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _trial_result(
    result: dict[str, Any],
    *,
    verifier_timeout_is_final_zero: bool,
    verifier_dependency_error: str | None = None,
    reward_schema: str = "scalar",
) -> tuple[str, float | None, str]:
    if verifier_dependency_error:
        return "infrastructure_failed", None, "evaluator"
    exception = result.get("exception_info") or {}
    exception_type = str(exception.get("exception_type") or "")
    if exception_type in {"AgentTimeoutError", "AgentExecutionTimeoutError"}:
        return "timeout", 0.0, "benchmark_agent"
    if (
        exception_type == "VerifierTimeoutError"
        and verifier_timeout_is_final_zero
        and result.get("agent_result") is not None
    ):
        return "timeout", 0.0, "benchmark_verifier"
    if exception_type:
        if candidate_error_code(exception):
            return "candidate_invalid", None, "candidate"
        return "infrastructure_failed", None, "ambiguous"
    if _uses_reviewbench_metric(result, reward_schema):
        reviewbench_reward = _reviewbench_reward(result)
        if reviewbench_reward is None:
            return "infrastructure_failed", None, "evaluator"
        counts = _reviewbench_overall(reviewbench_reward)
        assert counts is not None
        return "benchmark_complete", _reviewbench_f1(counts), "benchmark"
    reward = _reward(result)
    if reward is not None:
        return "benchmark_complete", reward, "benchmark"
    return "infrastructure_failed", None, "evaluator"


def _verifier_dependency_error(trial_dir: Path) -> str | None:
    output = trial_dir / "verifier" / "test-stdout.txt"
    try:
        text = output.read_text(errors="replace")
    except OSError:
        return None
    if _VERIFIER_UV_RESOLUTION_ERROR in text and any(hint in text for hint in _VERIFIER_UV_OFFLINE_HINTS):
        return "verifier uv tool dependency resolution failed"
    if _VERIFIER_UV_DOWNLOAD_ERROR in text and _VERIFIER_UV_REQUEST_ERROR in text:
        return "verifier uv tool dependency download failed"
    if _VERIFIER_CURL_SERVER_ERROR.search(text):
        return "verifier dependency bootstrap download failed"
    return None


def _cost_usd(result: dict[str, Any]) -> float:
    value = (result.get("agent_result") or {}).get("cost_usd")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _exception_message(value: object) -> str | None:
    if not value:
        return None
    message = _redact_environment_values(str(value).strip().splitlines()[0].strip())
    return None if message.startswith("Traceback") else message or None


def _safe_artifacts(jobs_dir: Path, trial_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for relative_path in SAFE_ARTIFACTS:
        path = trial_dir / relative_path
        if not path.is_file():
            continue
        payload = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(jobs_dir).as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return files
