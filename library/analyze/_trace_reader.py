"""Read full, redacted AHE evidence in explicit event and character ranges."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def _input_path(name: str, input_dir: Path | None = None) -> Path:
    if Path(name).name != name or name in {".", ".."}:
        raise ValueError("evidence filename must be a basename")
    root = (input_dir or Path(__file__).resolve().parent).resolve()
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError("evidence file is missing or not a regular file")
    if path.resolve().parent != root:
        raise ValueError("evidence file escapes the input directory")
    return path


def _records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line_index, line in enumerate(stream):
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                yield line_index, value


def _strings(value: Any, field: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield field or "$", value
    elif isinstance(value, dict):
        for key, item in value.items():
            child = f"{field}.{key}" if field else str(key)
            yield from _strings(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{field}[{index}]")


def search_file(path: Path, query: str) -> list[dict[str, Any]]:
    needle = query.casefold()
    if not needle:
        raise ValueError("search query must not be empty")
    matches = []
    for line_index, record in _records(path):
        record_index = record.get("record_index", line_index)
        for field, text in _strings(record):
            start = 0
            offsets = []
            while (offset := text.casefold().find(needle, start)) >= 0:
                offsets.append([offset, offset + len(query)])
                start = offset + max(1, len(query))
            if offsets:
                matches.append(
                    {
                        "record_index": record_index,
                        "record_type": record.get("record_type", "unknown"),
                        "field": field,
                        "matches": offsets,
                        "total_chars": len(text),
                    }
                )
    return matches


def _field_value(record: dict[str, Any], field: str | None) -> Any:
    value: Any = record
    if not field:
        return value
    for segment in field.split("."):
        if isinstance(value, dict) and segment in value:
            value = value[segment]
        elif isinstance(value, list) and segment.isdecimal():
            value = value[int(segment)]
        else:
            raise ValueError(f"field path does not exist: {field}")
    return value


def read_slice(
    path: Path,
    record_index: int,
    field: str | None,
    offset: int,
    length: int,
) -> dict[str, Any]:
    if offset < 0 or length <= 0:
        raise ValueError("offset must be nonnegative and length must be positive")
    for line_index, record in _records(path):
        actual_index = record.get("record_index", line_index)
        if actual_index != record_index:
            continue
        value = _field_value(record, field)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        end = min(len(text), offset + length)
        return {
            "record_index": actual_index,
            "record_type": record.get("record_type", "unknown"),
            "field": field or "$",
            "total_chars": len(text),
            "start": offset,
            "end": end,
            "next_offset": end if end < len(text) else None,
            "text": text[offset:end],
        }
    raise ValueError(f"record not found: {record_index}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search", help="find text and return event locations")
    search.add_argument("file")
    search.add_argument("query")
    show = subparsers.add_parser("show", help="read an explicit event/field character range")
    show.add_argument("file")
    show.add_argument("record_index", type=int)
    show.add_argument("--field")
    show.add_argument("--offset", type=int, default=0)
    show.add_argument("--length", type=int, required=True)
    args = parser.parse_args()
    try:
        path = _input_path(args.file)
        if args.command == "search":
            result: Any = search_file(path, args.query)
        else:
            result = read_slice(path, args.record_index, args.field, args.offset, args.length)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
