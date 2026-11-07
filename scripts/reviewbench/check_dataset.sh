#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec uv run --project "$ROOT" --frozen python "$ROOT/scripts/reviewbench/check_dataset.py" "$@"