#!/usr/bin/env python3
"""Build one Harbor dataset from DeepSWE, ReviewBench, and SWE-Atlas TW."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

SOURCES = ("deep_swe", "reviewbench", "swe_atlas")


def _canonical_repository(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("task has no repository metadata")
    repository = value.strip().rstrip("/")
    if repository.endswith(".git"):
        repository = repository[:-4]
    parsed = urlsplit(repository)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.lower()
        path = parsed.path.strip("/").lower()
        return f"{parsed.scheme.lower()}://{host}/{path}"
    return repository.lower()


def _task_name(path: Path, source: str, *, official_test: bool = False) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.name).strip("-.")
    if not safe:
        raise ValueError(f"cannot derive task ID from {path}")
    official = "official__" if source == "reviewbench" and official_test else ""
    return f"{source}__{official}{safe}"


def _insert_metadata(text: str, values: dict[str, object]) -> str:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "[metadata]"), None)
    if start is None:
        raise ValueError("task.toml has no [metadata] section")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    existing = {line.split("=", 1)[0].strip() for line in lines[start + 1 : end] if "=" in line}
    additions = [
        f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items() if key not in existing
    ]
    lines[end:end] = additions
    return "\n".join(lines) + "\n"


def _replace_task_name(text: str, package_name: str) -> str:
    lines = text.splitlines()
    in_task = False
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_task = stripped == "[task]"
        elif in_task and stripped.startswith("name ="):
            lines[index] = f"name = {json.dumps(package_name)}"
            replaced = True
            break
    if not replaced:
        raise ValueError("task.toml has no [task].name entry")
    return "\n".join(lines) + "\n"


def _repository(metadata: dict[str, object], source: str) -> str:
    keys = ("repository_url", "repository") if source != "reviewbench" else ("pr_key",)
    for key in keys:
        value = metadata.get(key)
        if source == "reviewbench" and isinstance(value, str):
            # The exporter names each task <owner>_<repo>_<pr>-<head8>.
            prefix = value.rsplit("_", 1)[0]
            owner, separator, repo = prefix.partition("_")
            if separator:
                return _canonical_repository(f"https://github.com/{owner}/{repo}")
        elif isinstance(value, str):
            return _canonical_repository(value)
    raise ValueError(f"{source} task is missing repository metadata")


def _pr_key(entry: dict[str, object]) -> str:
    repo = str(entry["repo"]).replace("https://github.com/", "").replace("/", "_")
    return f"{repo}_{entry['pr_number']}-{str(entry['head'])[:8]}"


def _task_dirs(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if path.is_dir() and (path / "task.toml").is_file())


def _write_manifest(root: Path, tasks: list[str]) -> None:
    lines = [
        "[dataset]",
        'name = "combined/deep-swe-reviewbench-swe-atlas"',
        'description = "Combined DeepSWE, ReviewBench, and SWE-Atlas Test Writing benchmark."',
        'keywords = ["coding-agent", "combined-benchmark"]',
        "",
    ]
    for task_id in tasks:
        lines.extend(("[[tasks]]", f'name = "combined/{task_id}"', ""))
    (root / "dataset.toml").write_text("\n".join(lines))


def build_dataset(
    *, deep_swe: Path, reviewbench_export: Path, reviewbench_source: Path, swe_atlas: Path, output: Path
) -> dict[str, int]:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing dataset: {output}")
    official_test = {_pr_key(entry) for entry in json.loads((reviewbench_source / "corpus/test/test.json").read_text())}
    inputs = {
        "deep_swe": _task_dirs(deep_swe / "tasks"),
        "reviewbench": _task_dirs(reviewbench_export),
        "swe_atlas": _task_dirs(swe_atlas),
    }
    if not all(inputs.values()):
        raise ValueError("each benchmark source must contain task.toml directories")

    output.mkdir(parents=True)
    records: list[str] = []
    counts: dict[str, int] = {}
    try:
        for source in SOURCES:
            for source_task in inputs[source]:
                config_path = source_task / "task.toml"
                config = tomllib.loads(config_path.read_text())
                metadata = config.get("metadata")
                if not isinstance(metadata, dict):
                    raise ValueError(f"{config_path} has no metadata table")
                repository = _repository(metadata, source)
                is_official = source == "reviewbench" and metadata.get("pr_key") in official_test
                task_id = _task_name(source_task, source, official_test=is_official)
                destination = output / task_id
                shutil.copytree(source_task, destination, symlinks=True)
                task_config_path = destination / "task.toml"
                task_text = task_config_path.read_text()
                task_text = _replace_task_name(task_text, f"combined/{task_id}")
                task_text = _insert_metadata(
                    task_text,
                    {
                        "evolve_source": source,
                        "evolve_repository": repository,
                        "evolve_official_test": is_official,
                        "evolve_source_task": source_task.name,
                    },
                )
                task_config_path.write_text(task_text)
                records.append(task_id)
                counts[source] = counts.get(source, 0) + 1
        _write_manifest(output, records)
        (output / "sources.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sources": counts,
                    "official_reviewbench_test_tasks": len(official_test),
                    "task_count": len(records),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep-swe", type=Path, required=True)
    parser.add_argument("--reviewbench-export", type=Path, required=True)
    parser.add_argument("--reviewbench-source", type=Path, required=True)
    parser.add_argument("--swe-atlas", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        counts = build_dataset(
            deep_swe=args.deep_swe,
            reviewbench_export=args.reviewbench_export,
            reviewbench_source=args.reviewbench_source,
            swe_atlas=args.swe_atlas,
            output=args.output,
        )
    except (
        FileExistsError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as error:
        print(f"dataset build failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"dataset": str(args.output.resolve()), "counts": counts, "total": sum(counts.values())},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
