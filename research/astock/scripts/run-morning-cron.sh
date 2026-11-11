#!/usr/bin/env bash
# run-morning-cron.sh — compatibility-named source-only runner (Linux).
# The existing user timer owns weekdays 08:00 Asia/Shanghai,
# --strict-data --trading-days-only (skips holidays via trading calendar,
# never publishes demo data). Delivery belongs to the research-seal success
# chain and scripts/deliver-morning-report.mjs. Pins PATH and keeps the
# fixed bridge proxy-free; default facts compilation does not call a model.

set -euo pipefail

# Cron has a minimal PATH; ensure node/npm/pi (symlinks in /usr/local/bin) resolve.
export PATH="/usr/local/bin:/usr/bin:/bin"
export HOME="${HOME:-/home/ubuntu}"
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
export TZ="Asia/Shanghai"

# The bridge subprocess also runs proxy-free via buildMinimalBridgeEnvironment.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="localhost,127.0.0.1,ollama.com,registry.npmjs.org"
export no_proxy="$NO_PROXY"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly MAX_ATTEMPTS=3
readonly TOTAL_WORKFLOW_BUDGET_SECONDS=3300
readonly MIN_RETRY_RUNTIME_SECONDS=60
readonly RETRY_BACKOFF_FIRST_SECONDS=20
readonly RETRY_BACKOFF_SECOND_SECONDS=60

current_utc() {
  /usr/bin/date -u +%Y-%m-%dT%H:%M:%SZ
}

current_target_date() {
  /usr/bin/date --date "$1" +%Y-%m-%d
}

run_workflow_attempt() {
  local timeout_seconds="$1"
  local target_date="$2"
  local frozen_cutoff_at="$3"
  local frozen_input="$4"
  local frozen_context_sha256="$5"
  local -a workflow_args=(
    scripts/morning-report.mjs
    --strict-data
    --trading-days-only
    --date "$target_date"
    --frozen-cutoff "$frozen_cutoff_at"
  )
  if [[ -n "$frozen_input" ]]; then
    workflow_args+=(
      --input "$frozen_input"
      --frozen-context-sha256 "$frozen_context_sha256"
    )
  fi
  if (( interrupted_exit != 0 )); then
    return "$interrupted_exit"
  fi
  /usr/bin/timeout --kill-after=10s "${timeout_seconds}s" /usr/local/bin/node \
    "${workflow_args[@]}" &
  workflow_pid=$!
  # Close the small launch race where the wrapper receives a signal after the
  # preflight check but before timeout's process group is visible to the trap.
  if (( interrupted_exit == 143 )); then
    signal_workflow_group TERM
  elif (( interrupted_exit == 130 )); then
    signal_workflow_group INT
  fi
  wait "$workflow_pid"
}

retry_decision() {
  local output_variable="$1"
  local workflow_exit="$2"
  local attempt="$3"
  local attempt_started_at="$4"
  local attempt_ended_at="$5"
  local frozen_cutoff_at="$6"
  local target_date="$7"
  local captured_output=""
  local decision_exit
  if captured_output="$(
    /usr/bin/timeout --kill-after=1s 5s /usr/local/bin/node scripts/morning-retry-decision.mjs \
      --exit-code "$workflow_exit" \
      --attempt "$attempt" \
      --max-attempts "$MAX_ATTEMPTS" \
      --started-at "$attempt_started_at" \
      --ended-at "$attempt_ended_at" \
      --frozen-cutoff "$frozen_cutoff_at" \
      --target-date "$target_date"
  )"; then
    printf -v "$output_variable" '%s' "$captured_output"
    return 0
  else
    decision_exit=$?
  fi
  printf -v "$output_variable" '%s' "$captured_output"
  return "$decision_exit"
}

parse_retry_directive() {
  local directive="$1"
  local action
  local mode
  local context_hash
  local extra
  if [[ "$directive" == *$'\n'* || "$directive" == *$'\r'* ]]; then
    return 1
  fi
  IFS=$'\t' read -r action mode context_hash extra <<<"$directive"
  if [[ "$action" != "retry" || -n "$extra" ]]; then
    return 1
  fi
  if [[ "$mode" == "recollect" && "$context_hash" == "-" ]]; then
    retry_mode="recollect"
    retry_context_sha256=""
    return 0
  fi
  if [[ "$mode" == "frozen_context" && "$context_hash" =~ ^[0-9a-f]{64}$ ]]; then
    retry_mode="frozen_context"
    retry_context_sha256="$context_hash"
    return 0
  fi
  return 1
}

run_retry_backoff() {
  /usr/bin/sleep "$1"
}

workflow_pid_alive() {
  [[ -n "$workflow_pid" ]] && kill -0 -- "$workflow_pid" 2>/dev/null
}

workflow_group_alive() {
  [[ -n "$workflow_pid" ]] && kill -0 -- "-$workflow_pid" 2>/dev/null
}

workflow_pid_running() {
  local running_pid
  while IFS= read -r running_pid; do
    if [[ "$running_pid" == "$workflow_pid" ]]; then
      return 0
    fi
  done < <(jobs -pr)
  return 1
}

signal_workflow_group() {
  local signal_name="$1"
  if workflow_group_alive; then
    if kill -s "$signal_name" -- "-$workflow_pid" 2>/dev/null; then
      return 0
    fi
  fi
  # Cron shells run without job control. GNU timeout creates its own process
  # group after exec, leaving a narrow interval where -PID does not exist yet.
  # Signal the direct timeout PID in that interval so cancellation cannot turn
  # into an unbounded wait for the remaining workflow budget.
  if workflow_pid_alive; then
    kill -s "$signal_name" -- "$workflow_pid" 2>/dev/null || true
  fi
}

wait_for_workflow_exit() {
  local checks="$1"
  local index
  for ((index = 0; index < checks; index += 1)); do
    # kill -0 remains true for an exited but unreaped direct child. Reap only
    # after Bash's job table confirms it is no longer running, which keeps this
    # polling loop bounded without blocking on a live child.
    if [[ -n "$workflow_pid" ]] && ! workflow_pid_running; then
      wait "$workflow_pid" 2>/dev/null || true
    fi
    if ! workflow_pid_alive && ! workflow_group_alive; then
      return 0
    fi
    /usr/bin/sleep 0.1
  done
  ! workflow_pid_alive && ! workflow_group_alive
}

handle_term() {
  if (( interrupted_exit == 0 )); then
    interrupted_exit=143
    signal_workflow_group TERM
  fi
}

handle_int() {
  if (( interrupted_exit == 0 )); then
    interrupted_exit=130
    signal_workflow_group INT
  fi
}

stop_interrupted_workflow() {
  # Bash wait returns as soon as a trapped signal is handled. Keep the wrapper
  # alive until the dedicated workflow group has actually exited, escalating
  # only after a short grace period. Delivery is not owned by this runner.
  if [[ -z "$workflow_pid" ]]; then
    return 0
  fi
  if ! wait_for_workflow_exit 20; then
    signal_workflow_group KILL
  fi
  if ! wait_for_workflow_exit 20; then
    return 1
  fi
  wait "$workflow_pid" 2>/dev/null || true
}

main() {
  cd "$PROJECT_ROOT"
  local final_delivery="${ASTOCK_FINAL_DELIVERY:-disabled}"
  local workflow_pid=""
  local interrupted_exit=0
  local attempt=1
  local workflow_exit=1
  local attempt_started_at=""
  local attempt_ended_at=""
  local frozen_cutoff_at
  local attempt_input=""
  local attempt_context_sha256=""
  local target_date
  local budget_deadline
  local remaining
  local backoff
  local retry_exit
  local retry_output
  local retry_mode=""
  local retry_context_sha256=""

  if [[ "$final_delivery" != "disabled" ]]; then
    printf '%s\n' 'AStock: legacy_wrapper_delivery_disabled; use scripts/deliver-morning-report.mjs through the research-seal success chain' >&2
    return 64
  fi

  if (( $# == 0 )); then
    frozen_cutoff_at="$(current_utc)"
    target_date="$(current_target_date "$frozen_cutoff_at")"
  elif (( $# == 4 )) \
    && [[ "$1" == "--target-date" ]] \
    && [[ "$3" == "--frozen-cutoff" ]]; then
    target_date="$2"
    frozen_cutoff_at="$4"
  else
    printf '%s\n' 'AStock: usage: run-morning-cron.sh [--target-date YYYY-MM-DD --frozen-cutoff UTC_ISO_INSTANT]' >&2
    return 64
  fi
  if [[ ! "$target_date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
    || [[ ! "$frozen_cutoff_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,3})?Z$ ]]; then
    printf '%s\n' 'AStock: invalid fixed morning occurrence identity' >&2
    return 64
  fi
  local cutoff_target_date
  if ! cutoff_target_date="$(current_target_date "$frozen_cutoff_at")" \
    || [[ "$cutoff_target_date" != "$target_date" ]]; then
    printf '%s\n' 'AStock: target date and frozen cutoff identify different occurrences' >&2
    return 64
  fi
  budget_deadline=$((SECONDS + TOTAL_WORKFLOW_BUDGET_SECONDS))
  trap handle_term TERM
  trap handle_int INT

  while (( attempt <= MAX_ATTEMPTS )); do
    remaining=$((budget_deadline - SECONDS))
    if (( remaining <= 0 )); then
      workflow_exit=124
      break
    fi

    attempt_started_at="$(current_utc)"
    # GNU timeout owns a dedicated process group by default. The traps above
    # forward an adapter signal to the entire group, preventing a bridge
    # descendant from surviving after the orchestrator records unknown.
    if run_workflow_attempt \
      "$remaining" \
      "$target_date" \
      "$frozen_cutoff_at" \
      "$attempt_input" \
      "$attempt_context_sha256"; then
      workflow_exit=0
    else
      workflow_exit=$?
    fi
    attempt_ended_at="$(current_utc)"

    if (( interrupted_exit != 0 )); then
      if ! stop_interrupted_workflow; then
        printf '%s\n' 'AStock: interrupted workflow did not exit within the bounded kill window' >&2
      fi
      trap - TERM INT
      return "$interrupted_exit"
    fi
    workflow_pid=""

    # Successful/skipped work, intentional publication blocks, authoritative
    # timeouts and legacy reserved exit 76 are terminal for this occurrence.
    if (( workflow_exit == 0 || workflow_exit == 2 || workflow_exit == 76 || workflow_exit == 124 || workflow_exit >= 128 )); then
      break
    fi
    if (( attempt >= MAX_ATTEMPTS )); then
      break
    fi

    retry_output=""
    if retry_decision \
      retry_output \
      "$workflow_exit" \
      "$attempt" \
      "$attempt_started_at" \
      "$attempt_ended_at" \
      "$frozen_cutoff_at" \
      "$target_date"; then
      retry_exit=0
    else
      retry_exit=$?
    fi
    if (( interrupted_exit != 0 )); then
      if ! stop_interrupted_workflow; then
        printf '%s\n' 'AStock: interrupted workflow did not exit within the bounded kill window' >&2
      fi
      trap - TERM INT
      return "$interrupted_exit"
    fi
    if (( retry_exit != 0 )) || ! parse_retry_directive "$retry_output"; then
      break
    fi
    # A frozen snapshot that disappears or fails validation must fail closed;
    # never convert that incident into a wider evidence-window recollection.
    if [[ -n "$attempt_input" && "$retry_mode" == "recollect" ]]; then
      printf '%s\n' 'AStock: frozen context could not be reused; stopping without recollection' >&2
      break
    fi
    if [[ "$retry_mode" == "frozen_context" ]]; then
      attempt_input="work/morning-context.json"
      attempt_context_sha256="$retry_context_sha256"
    else
      attempt_input=""
      attempt_context_sha256=""
    fi

    if (( retry_exit == 0 )); then
      if (( attempt == 1 )); then
        backoff=$RETRY_BACKOFF_FIRST_SECONDS
      else
        backoff=$RETRY_BACKOFF_SECOND_SECONDS
      fi
      remaining=$((budget_deadline - SECONDS))
      if (( remaining <= backoff + MIN_RETRY_RUNTIME_SECONDS )); then
        printf '%s\n' 'AStock: retryable failure confirmed but total workflow budget is exhausted; stopping safely' >&2
        break
      fi
      printf 'AStock: retrying confirmed transient workflow failure (attempt %s of %s)\n' \
        "$((attempt + 1))" "$MAX_ATTEMPTS" >&2
      run_retry_backoff "$backoff" || true
      if (( interrupted_exit != 0 )); then
        if ! stop_interrupted_workflow; then
          printf '%s\n' 'AStock: interrupted workflow did not exit within the bounded kill window' >&2
        fi
        trap - TERM INT
        return "$interrupted_exit"
      fi
      attempt=$((attempt + 1))
      continue
    fi
    break
  done

  trap - TERM INT
  if (( interrupted_exit != 0 )); then
    if ! stop_interrupted_workflow; then
      printf '%s\n' 'AStock: interrupted workflow did not exit within the bounded kill window' >&2
    fi
    return "$interrupted_exit"
  fi
  printf '%s\n' 'AStock: source-only run completed; delivery belongs to the research-seal success chain' >&2
  return "$workflow_exit"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
