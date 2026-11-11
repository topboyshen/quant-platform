#!/usr/bin/env bash
# Local verifiable copy of research-data. The repository has no remote; this
# bundle is the only off-working-tree copy of its history.
set -euo pipefail
umask 077
dest="${RESEARCH_DATA_BUNDLE_DIR:-/home/ubuntu/backups/research-data}"
mkdir -p -- "$dest"
stamp="$(TZ=Asia/Shanghai date +%F)"
git -C /home/ubuntu/research-data bundle create "$dest/research-data-${stamp}.bundle" --all
find "$dest" -type f -name 'research-data-*.bundle' -mtime +21 -delete
