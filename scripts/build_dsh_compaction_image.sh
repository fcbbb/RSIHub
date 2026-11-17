#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PINS="$ROOT/containers/runtime-versions.env"
IMAGE=${1:-}
REBUILD=${2:-}
[[ $# -ge 1 && $# -le 2 && ( -z $REBUILD || $REBUILD == --rebuild ) ]] || {
  echo "usage: build_dsh_compaction_image.sh IMAGE [--rebuild]" >&2
  exit 2
}

pin() { sed -n "s/^$1=//p" "$PINS"; }
BASE_IMAGE=$(awk -F= -v image="$IMAGE" '$1 == image { print substr($0, index($0, "=") + 1) }' \
  "$ROOT/containers/mutate-dsh/compaction-images.env")
[[ -n $BASE_IMAGE ]] || { echo "unsupported DSH compaction image: $IMAGE" >&2; exit 2; }
[[ -n $BASE_IMAGE ]] || { echo "missing base image pin" >&2; exit 2; }
for tool in docker cmp; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 2; }
done
docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || {
  echo "base image is not available locally: $BASE_IMAGE" >&2
  exit 2
}

check_overlay() {
  local image=$1 rel
  for rel in headless.mutate.cordis.yml headless.analyst.cordis.yml; do
    docker run --rm --network none --read-only --entrypoint /bin/cat \
      "$image" "/opt/evolve/dsh/$rel" | cmp - "$ROOT/containers/mutate-dsh/$rel" || {
        echo "overlay config mismatch in $image: $rel (use --rebuild to refresh)" >&2
        return 1
      }
  done
  local version
  version=$(docker run --rm --network none --read-only --entrypoint /bin/sh \
    "$image" -ec 'dsh --version') || {
      echo "DSH runtime validation failed for $image" >&2
      return 1
    }
  [[ $version == "$(pin DSH_MUTATE_VERSION)" ]] || {
    echo "unexpected DSH version in $image: $version" >&2
    return 1
  }
}

IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null || true)
if [[ -n $IMAGE_ID && $REBUILD != --rebuild ]]; then
  check_overlay "$IMAGE"
  echo "Validated compaction image: $IMAGE ($IMAGE_ID), base $BASE_IMAGE"
  exit 0
fi

docker build --pull=false \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -f "$ROOT/containers/mutate-dsh/Dockerfile.overlay" \
  -t "$IMAGE" "$ROOT/containers/mutate-dsh"
IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$IMAGE")
[[ $IMAGE_ID =~ ^sha256:[a-f0-9]{64}$ ]] || {
  echo "cannot resolve a local image ID for $IMAGE" >&2
  exit 2
}
check_overlay "$IMAGE"
echo "Built and validated compaction image: $IMAGE ($IMAGE_ID), base $BASE_IMAGE"
