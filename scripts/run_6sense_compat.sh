#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compat_dir="$repo_root/external/6sense/compat"
fixture="${SIXSENSE_COMPAT_FIXTURE:-$compat_dir/fixture.json}"
output="${SIXSENSE_COMPAT_OUTPUT:-$repo_root/docker/run/6sense-compat.json}"
image="${SIXSENSE_COMPAT_IMAGE:-mentors-task-6sense-compat:dev}"
goproxy="${SIXSENSE_GOPROXY:-https://goproxy.cn,direct}"

if ! command -v docker >/dev/null 2>&1; then
  echo "6Sense 兼容轨道需要 Docker" >&2
  exit 2
fi
if [[ ! -f "$fixture" ]]; then
  echo "fixture 不存在：$fixture" >&2
  exit 2
fi

mkdir -p "$(dirname "$output")"
DOCKER_BUILDKIT=0 docker build \
  --tag "$image" \
  --build-arg "GOPROXY=$goproxy" \
  --file "$compat_dir/Dockerfile" \
  "$compat_dir"

docker run --rm \
  --cap-add=NET_RAW \
  -e SIXSENSE_IFACE="${SIXSENSE_IFACE:-eth0}" \
  -v "$fixture:/input/fixture.json:ro" \
  -v "$(dirname "$output"):/out:rw" \
  "$image" \
  --input /input/fixture.json \
  --out "/out/$(basename "$output")"

echo "6Sense 兼容轨道完成：$output"
