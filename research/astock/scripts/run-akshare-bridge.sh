#!/usr/bin/env bash
# run-akshare-bridge.sh — Linux/macOS wrapper for the fixed AkShare stdin bridge.
# Mirrors scripts/run-akshare-bridge.ps1. Only ever invokes the project .venv
# Python (never the system Python). Reads one request JSON from stdin.

set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_python="${project_root}/.venv/bin/python"
bridge_path="${project_root}/mcp/akshare_bridge.py"

if [[ ! -x "${venv_python}" ]]; then
  echo "The project .venv does not exist. Run 'npm run data:setup:unix' first." >&2
  exit 1
fi
if [[ ! -f "${bridge_path}" ]]; then
  echo "The AkShare stdin bridge does not exist: ${bridge_path}" >&2
  exit 1
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

exec "${venv_python}" "${bridge_path}"