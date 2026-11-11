#!/usr/bin/env bash
# Explicit REQUIRED preserves legacy behavior; missing/invalid policy fails closed.
# Removing NO_BACKUP must never silently restore backup permission.
research_backup_policy_load() {
  local policy_file="${RESEARCH_BACKUP_POLICY_FILE:-/etc/research-ledger/backup-policy.mode}"
  local metadata owner mode
  if [[ ! -e "$policy_file" && ! -L "$policy_file" ]]; then
    printf 'BACKUP_POLICY=fail reason=policy_file_missing\n' >&2
    return 1
  fi
  if [[ ! -f "$policy_file" || -L "$policy_file" || ! -r "$policy_file" ]]; then
    printf 'BACKUP_POLICY=fail reason=policy_file_invalid\n' >&2
    return 1
  fi
  metadata="$(stat -c '%u:%a' -- "$policy_file" 2>/dev/null)" \
    || metadata="$(stat -f '%u:%Lp' "$policy_file" 2>/dev/null)" || {
      printf 'BACKUP_POLICY=fail reason=policy_metadata_unavailable\n' >&2; return 1;
    }
  owner="${metadata%%:*}"
  mode="${metadata#*:}"
  [[ "$owner" =~ ^[0-9]+$ && "$mode" =~ ^[0-7]{3,4}$ ]] || {
    printf 'BACKUP_POLICY=fail reason=policy_metadata_invalid\n' >&2; return 1;
  }
  if (( (8#$mode & 0022) != 0 )) || {
    [[ "$policy_file" == /etc/research-ledger/backup-policy.mode ]] && [[ "$owner" != 0 ]];
  }; then
    printf 'BACKUP_POLICY=fail reason=policy_permissions_invalid\n' >&2
    return 1
  fi
  RESEARCH_BACKUP_POLICY="$(< "$policy_file")"
  case "$RESEARCH_BACKUP_POLICY" in
    REQUIRED|NO_BACKUP) ;;
    *) printf 'BACKUP_POLICY=fail reason=policy_value_invalid\n' >&2; return 1 ;;
  esac
}
