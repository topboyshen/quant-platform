#!/bin/sh
# Build the static shell into dist/ (no external deps). BUILD id = short sha256 of sources.
set -e
cd "$(dirname "$0")"
B=$(cat src/index.html src/shell-assets/* | sha256sum | cut -c1-10)
rm -rf dist && mkdir -p dist/shell-assets
cp src/shell-assets/* dist/shell-assets/
sed "s/__BUILD__/$B/g" src/index.html > dist/index.html
(cd dist && find . -type f | sort | xargs sha256sum) > dist.sha256
echo "built $B"
