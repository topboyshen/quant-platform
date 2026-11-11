#!/bin/bash
# Keep the newest tagged Radar images and remove only older, unused revisions.
# Dry-run is the default; systemd invokes this with --apply.
set -euo pipefail

apply=false
retain="${RADAR_IMAGE_RETAIN:-2}"
repositories="${RADAR_IMAGE_REPOSITORIES:-trading-radar-integration trading-radar-dashboard trading-radar-monitor trading-radar-ml-serve trading-radar-quantdinger-backend trading-radar-quantdinger-frontend trading-radar-macro-risk trading-radar-runtime-observer}"
dangling_age="${RADAR_DANGLING_IMAGE_AGE:-168h}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply) apply=true ;;
        --retain)
            shift
            retain="${1:-}"
            ;;
        *)
            echo "usage: $0 [--apply] [--retain N]" >&2
            exit 2
            ;;
    esac
    shift
done

case "$retain" in
    ''|*[!0-9]*) echo "--retain must be a positive integer" >&2; exit 2 ;;
esac
if [ "$retain" -lt 1 ]; then
    echo "--retain must be at least 1" >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed" >&2
    exit 1
fi

used_ids="$(docker container ls -aq | while read -r container_id; do
    [ -n "$container_id" ] && docker container inspect --format '{{.Image}}' "$container_id"
done | sort -u)"

# Image ids named by production pins, release receipts and frozen manifests.
# Protect the union of all references: conservative by design, and it does not
# require deciding which of the conflicting pins is authoritative.
referenced_ids=""
for ref_root in /home/ubuntu/trading-radar/deploy /home/ubuntu/releases; do
    [ -d "$ref_root" ] || continue
    referenced_ids+="$(grep -RhoE 'sha256:[0-9a-f]{64}' "$ref_root" 2>/dev/null || true)"$'\n'
done
referenced_ids="$(printf '%s' "$referenced_ids" | sort -u)"
protected_ids="$(printf '%s\n%s\n' "$used_ids" "$referenced_ids" \
    | grep -E '^sha256:[0-9a-f]{64}$' | sort -u || true)"

if $apply; then
    # A local tag keeps a referenced image out of `docker image prune` even if
    # the retention loop below removes its release tag in this same run.
    while read -r image_id; do
        [ -n "$image_id" ] || continue
        docker image inspect "$image_id" >/dev/null 2>&1 || continue
        if ! docker image inspect --format '{{range .RepoTags}}{{println .}}{{end}}' "$image_id" \
            | grep -q '^local-retention/'; then
            bare_id="${image_id#sha256:}"
            docker tag "$image_id" "local-retention/pin-referenced:${bare_id:0:12}"
            echo "protect $image_id: referenced by pin/receipt"
        fi
    done <<< "$referenced_ids"
fi

for repository in $repositories; do
    repository_retain="$retain"
    # The standalone dashboard is disabled and needs only one short-term
    # rollback image. Core services and QuantDinger keep current + rollback.
    if [ "$repository" = "trading-radar-dashboard" ]; then
        repository_retain=1
    fi
    index=0
    docker image ls "$repository" --format '{{.Repository}}:{{.Tag}} {{.ID}}' | while read -r reference image_id; do
        [ -n "${reference:-}" ] || continue
        [ "$reference" != "$repository:<none>" ] || continue
        index=$((index + 1))
        if [ "$index" -le "$repository_retain" ]; then
            echo "keep $reference ($image_id): newest $repository_retain"
            continue
        fi
        full_id="$(docker image inspect --format '{{.Id}}' "$reference" 2>/dev/null || true)"
        if [ -n "$full_id" ] && printf '%s\n' "$used_ids" | grep -Fqx "$full_id"; then
            echo "keep $reference ($image_id): referenced by a container"
            continue
        fi
        if [ -n "$full_id" ] && printf '%s\n' "$protected_ids" | grep -Fqx "$full_id"; then
            echo "keep $reference ($image_id): referenced by pin/receipt"
            continue
        fi
        if $apply; then
            docker image rm "$reference"
        else
            echo "would remove $reference ($image_id)"
        fi
    done
done

if $apply; then
    docker image prune --force --filter "until=$dangling_age"
    # The --filter form only reaches BuildKit through the buildx frontend; with
    # the plugin absent it falls back to the legacy builder, prints DEPRECATED
    # and reclaims 0B. Fall back to pruning the BuildKit cache whole instead.
    if docker buildx version >/dev/null 2>&1; then
        docker buildx prune --force --filter "until=$dangling_age"
    else
        docker builder prune --all --force
    fi
else
    echo "would prune dangling images older than $dangling_age"
    if docker buildx version >/dev/null 2>&1; then
        echo "would prune build cache older than $dangling_age"
    else
        echo "would prune all build cache (buildx plugin absent)"
    fi
fi
