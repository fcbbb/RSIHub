"""Select a statistically supported best-so-far AHE parent."""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from typing import Any

from evolve.frozen import sdk
from evolve.frozen.interfaces import ArchiveView, OperatorContext, SelectOperator, SelectResult
from library.select._config import SELECT_CONFIG as CONFIG

_CONFIDENCE = 0.95
_RESAMPLES = 10_000
_PASS_THRESHOLD = 1.0
_SOURCE_PREFIXES = (
    ("deep_swe__", "deep_swe"),
    ("reviewbench__", "reviewbench"),
    ("swe_atlas__", "swe_atlas"),
)


def _generation_key(row: dict[str, Any]) -> tuple[int, str]:
    genid = str(row.get("genid", ""))
    head = genid.split("-", 1)[0]
    return (int(head) if head.isdigit() else -1, genid)


def _task_source(task_id: str) -> str | None:
    short_name = task_id.rsplit("/", 1)[-1]
    for prefix, source in _SOURCE_PREFIXES:
        if short_name.startswith(prefix):
            return source
    return None


def _task_rewards(row: dict[str, Any]) -> dict[str, float] | None:
    vector = row.get("task_vector")
    tasks = vector.get("tasks") if isinstance(vector, dict) else None
    if not isinstance(tasks, dict) or not tasks:
        return None
    result: dict[str, float] = {}
    for task_id, evidence in tasks.items():
        trials = evidence.get("trials") if isinstance(evidence, dict) else None
        if not isinstance(trials, list) or not trials:
            return None
        rewards: list[float] = []
        for trial in trials:
            if not isinstance(trial, dict):
                return None
            status = str(trial.get("status", ""))
            owner = str(trial.get("owner", ""))
            eligible = status == "benchmark_complete" or (
                status == "timeout" and owner in {"benchmark_agent", "benchmark_verifier"}
            )
            reward = trial.get("reward")
            if (
                not eligible
                or not isinstance(reward, (int, float))
                or isinstance(reward, bool)
                or not math.isfinite(float(reward))
            ):
                return None
            rewards.append(float(reward))
        result[str(task_id)] = sum(rewards) / len(rewards)
    return result


def _paired_differences(
    incumbent: dict[str, Any], candidate: dict[str, Any]
) -> tuple[list[float], dict[str, list[float]], str] | None:
    if (
        not incumbent.get("task_set_hash")
        or incumbent.get("task_set_hash") != candidate.get("task_set_hash")
        or incumbent.get("evaluator_fingerprint") != candidate.get("evaluator_fingerprint")
    ):
        return None
    before = _task_rewards(incumbent)
    after = _task_rewards(candidate)
    if before is None or after is None or before.keys() != after.keys() or len(before) < 2:
        return None
    differences = [after[task] - before[task] for task in sorted(before)]
    components = incumbent.get("score_components")
    macro = isinstance(components, dict) and {"deep_swe", "reviewbench", "swe_atlas"} <= set(components)
    if not macro:
        return differences, {"all": differences}, "task_mean"
    groups: dict[str, list[float]] = defaultdict(list)
    for task, difference in zip(sorted(before), differences, strict=True):
        source = _task_source(task)
        if source is None:
            return None
        groups[source].append(difference)
    if set(groups) != {"deep_swe", "reviewbench", "swe_atlas"}:
        return None
    return differences, dict(groups), "equal_source_macro"


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _confidence_interval(
    groups: dict[str, list[float]],
    *,
    candidate: str,
    incumbent: str,
    task_set_hash: str,
    score_aggregation: str,
) -> tuple[float, float, float]:
    seed_bytes = hashlib.sha256(f"{candidate}\0{incumbent}\0{task_set_hash}".encode()).digest()
    rng = random.Random(int.from_bytes(seed_bytes[:8], "big"))
    samples: list[float] = []
    if score_aggregation == "equal_source_macro":
        strata = [groups[name] for name in sorted(groups)]
        for _ in range(_RESAMPLES):
            source_means = [sum(group[rng.randrange(len(group))] for _ in group) / len(group) for group in strata]
            samples.append(sum(source_means) / len(source_means))
    else:
        values = groups["all"]
        for _ in range(_RESAMPLES):
            samples.append(sum(values[rng.randrange(len(values))] for _ in values) / len(values))
    point = sum(sum(group) / len(group) for group in groups.values()) / len(groups)
    tail = (1.0 - _CONFIDENCE) / 2.0
    return point, _percentile(samples, tail), _percentile(samples, 1.0 - tail)


def _transitions(before: dict[str, float], after: dict[str, float]) -> dict[str, int]:
    counts = {"P2P": 0, "F2P": 0, "P2F": 0, "F2F": 0}
    for task in before:
        was_pass = before[task] >= _PASS_THRESHOLD
        now_pass = after[task] >= _PASS_THRESHOLD
        key = "P2P" if was_pass and now_pass else "F2P" if not was_pass and now_pass else "P2F" if was_pass else "F2F"
        counts[key] += 1
    return counts


class AheChampionSelect(SelectOperator):
    def pick(self, archive: ArchiveView, ctx: OperatorContext) -> SelectResult:
        parents = sorted(archive.valid_parents(), key=_generation_key)
        if not parents:
            raise SystemExit("no valid AHE parents")
        champion = parents[0]
        comparisons: list[dict[str, Any]] = []
        for candidate in parents[1:]:
            incumbent_id = str(champion["genid"])
            candidate_id = str(candidate["genid"])
            base = {
                "incumbent": incumbent_id,
                "candidate": candidate_id,
                "incumbent_score": float(champion["score"]),
                "candidate_score": float(candidate["score"]),
            }
            if float(candidate["score"]) <= float(champion["score"]):
                comparisons.append({**base, "decision": "retain", "reason": "candidate score did not improve"})
                continue
            paired = _paired_differences(champion, candidate)
            if paired is None:
                comparisons.append(
                    {**base, "decision": "retain", "reason": "task evidence is incomplete or incomparable"}
                )
                continue
            before = _task_rewards(champion)
            after = _task_rewards(candidate)
            assert before is not None and after is not None
            differences, groups, aggregation = paired
            point, lower, upper = _confidence_interval(
                groups,
                candidate=candidate_id,
                incumbent=incumbent_id,
                task_set_hash=str(candidate["task_set_hash"]),
                score_aggregation=aggregation,
            )
            transitions = _transitions(before, after)
            promote = lower > 0.0
            comparisons.append(
                {
                    **base,
                    "decision": "promote" if promote else "retain",
                    "reason": "paired 95% interval is above zero" if promote else "paired 95% interval includes zero",
                    "task_count": len(differences),
                    "score_aggregation": aggregation,
                    "delta": point,
                    "confidence_interval_95": [lower, upper],
                    "transitions": transitions,
                }
            )
            if promote:
                champion = candidate
        decision = {
            "schema_version": 1,
            "selector": "ahe_champion",
            "confidence_level": _CONFIDENCE,
            "resamples": _RESAMPLES,
            "champion": str(champion["genid"]),
            "comparisons": comparisons,
        }
        return SelectResult(parents=[str(champion["genid"])], decision=decision)


if __name__ == "__main__":
    sdk.main(AheChampionSelect, config_schema=CONFIG)
