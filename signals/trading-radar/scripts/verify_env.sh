#!/usr/bin/env bash
# 本地或 CI：校验当前环境的 pip 依赖是否自洽（无缺失、无冲突）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec python3 -m pip check
