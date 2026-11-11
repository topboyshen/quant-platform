#!/usr/bin/env bash
set -euo pipefail

# Hermes runs this wrapper with --no-agent and delivers stdout verbatim.
# The immutable release symlink is switched only after a silent preview passes.
release_dir="${QUANTDINGER_RESEARCH_RELEASE:-/home/ubuntu/releases/quantdinger-research-radar/current}"
user_id="${QUANTDINGER_RESEARCH_USER_ID:-1}"
db_container="${QUANTDINGER_DB_CONTAINER:-radar-quantdinger-postgres}"

exec /usr/bin/python3 -B \
  "${release_dir}/quantdinger_research_radar.py" \
  --user-id "${user_id}" \
  --db-container "${db_container}" \
  --live-quotes
