#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
required=(
  SIXSENSE_HOME
  SIXSENSE_PYTHON
  SIXSENSE_SCANNER
  SIXSENSE_DEALIASER
  SIXSENSE_BUDGET
  SIXSENSE_PPI
  SIXSENSE_GRADIENT
)

missing=()
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    missing+=("$name")
  fi
done
if (( ${#missing[@]} )); then
  echo "6Sense 前置条件缺失：${missing[*]}" >&2
  echo "见 $repo_root/external/6sense/README.md" >&2
  exit 2
fi

for path in \
  "$SIXSENSE_HOME/6sense_runner.py" \
  "$SIXSENSE_HOME/config.py" \
  "$SIXSENSE_PYTHON" \
  "$SIXSENSE_SCANNER" \
  "$SIXSENSE_DEALIASER"; do
  if [[ ! -e "$path" ]]; then
    echo "6Sense 路径不存在：$path" >&2
    exit 2
  fi
done

if grep -q 'YOUR/HOME/DIRECTORY/HERE' "$SIXSENSE_HOME/config.py"; then
  echo "6Sense config.py 尚未填写 HOME_DIRECTORY 等路径" >&2
  exit 2
fi

exec "$SIXSENSE_PYTHON" "$SIXSENSE_HOME/6sense_runner.py" \
  "$SIXSENSE_BUDGET" "$SIXSENSE_PPI" "$SIXSENSE_GRADIENT"
