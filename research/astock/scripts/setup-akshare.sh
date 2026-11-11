#!/usr/bin/env bash
# setup-akshare.sh — Linux/macOS data environment setup.
# Mirrors scripts/setup-akshare.ps1: creates the project .venv and installs the
# pinned dependencies from requirements.txt. Does not touch system Python or Pi
# credentials. Uses the Tsinghua PyPI mirror by default for speed in CN; override
# with PIP_INDEX_URL.

set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
requirements="${project_root}/requirements.txt"
venv_dir="${project_root}/.venv"
venv_python="${venv_dir}/bin/python"

if [[ ! -f "${requirements}" ]]; then
  echo "Missing dependency file: ${requirements}" >&2
  exit 1
fi

if [[ ! -x "${venv_python}" ]]; then
  echo "Creating the project Python environment (.venv)..."
  python3 -m venv "${venv_dir}"
  if [[ ! -x "${venv_python}" ]]; then
    echo "Failed to create .venv." >&2
    exit 1
  fi
fi

export PYTHONUTF8=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
# Ignore user/global pip.conf (which may hardcode a broken proxy) so the
# configured index_url below is used directly. Override with PIP_INDEX_URL.
export PIP_CONFIG_FILE=/dev/null
index_url="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# Drop any inherited HTTP proxies; the mirror above is directly reachable.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy

# Preserve NO_PROXY for hosts that should still bypass.
export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,ollama.com,registry.npmjs.org}"
export no_proxy="$NO_PROXY"

echo "Installing pinned dependencies from requirements.txt (index: ${index_url})..."
"${venv_python}" -m pip install --index-url "${index_url}" --requirement "${requirements}"

echo "Checking Python dependency consistency..."
"${venv_python}" -m pip check

"${venv_python}" -c "import importlib.metadata as m; print('akshare=' + m.version('akshare')); print('mcp=' + m.version('mcp'))"

# Initialize the SQLite history database (idempotent).
"${venv_python}" "${project_root}/mcp/storage.py" init || true

echo "Data environment is ready."
echo "Next: npm run data:test"