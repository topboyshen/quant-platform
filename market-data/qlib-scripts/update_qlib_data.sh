#!/usr/bin/env bash
# P0 hardening candidate for the Qlib CN data updater.
#
# Validate this candidate offline before installation.
# It deliberately never changes a mihomo selector. The configured HTTP proxy
# is treated as a transport endpoint only.

set -Eeuo pipefail
umask 022

QLIB_HOME="${QLIB_HOME:-$HOME/qlib}"
DATA_DIR="${QLIB_DATA_DIR:-$QLIB_HOME/data}"
LOG="${QLIB_LOG:-$QLIB_HOME/logs/update_data.log}"
LOCK_FILE="${QLIB_LOCK_FILE:-$DATA_DIR/.update.lock}"
# Keep transport selection local to each curl invocation. The existing SOCKS
# listener was verified independently of the unreliable HTTP CONNECT listener.
PROXY="${QLIB_PROXY:-socks5h://127.0.0.1:7891}"
REPO="${QLIB_REPO:-chenditc/investment_data}"
ASSET_NAME="${QLIB_ASSET_NAME:-qlib_bin.tar.gz}"
API_URL="${QLIB_API_URL:-https://api.github.com/repos/$REPO/releases/latest}"

CURL_BIN="${QLIB_CURL_BIN:-curl}"
FLOCK_BIN="${QLIB_FLOCK_BIN:-flock}"
PYTHON_BIN="${QLIB_PYTHON_BIN:-python3}"
TAR_BIN="${QLIB_TAR_BIN:-tar}"
GZIP_BIN="${QLIB_GZIP_BIN:-gzip}"
SHA256_BIN="${QLIB_SHA256_BIN:-sha256sum}"
EXCHANGE_BIN="${QLIB_EXCHANGE_BIN:-}"

CONNECT_TIMEOUT="${QLIB_CONNECT_TIMEOUT:-15}"
API_MAX_TIME="${QLIB_API_MAX_TIME:-30}"
DOWNLOAD_MAX_TIME="${QLIB_DOWNLOAD_MAX_TIME:-180}"
RETRY_ATTEMPTS="${QLIB_RETRY_ATTEMPTS:-4}"
RETRY_DELAY="${QLIB_RETRY_DELAY:-10}"

ACTIVE_DIR="$DATA_DIR/cn_data"
ROLLBACK_DIR="$DATA_DIR/.cn_data.rollback"
TAG_FILE="$DATA_DIR/.tag"
DOWNLOAD_CACHE_DIR="$DATA_DIR/.qlib-download-cache"
CACHE_META="$DOWNLOAD_CACHE_DIR/release.meta"
CACHE_TARBALL="$DOWNLOAD_CACHE_DIR/$ASSET_NAME.part"
RUN_ID="$(date -u '+%Y%m%dT%H%M%SZ')-$$"

WORK_DIR=""
COMMITTED=0

mkdir -p "$(dirname "$LOG")"
exec 3>>"$LOG"

log() {
  printf '[%s] run=%s %s\n' "$(date '+%F %T')" "$RUN_ID" "$*" >&3
}

die() {
  log "level=ERROR $*"
  exit 1
}

validate_runtime_setting() {
  local name="$1"
  local value="$2"
  case "$value" in
    ''|*[!0-9]*) die "invalid_setting name=$name value=$value" ;;
  esac
}

safe_remove_tree() {
  local target="$1"
  case "$target" in
    "$DATA_DIR"/.qlib-update.*|"$DATA_DIR"/.cn_data.rollback|"$DATA_DIR"/.cn_data.uncommitted.*)
      rm -rf -- "$target"
      ;;
    *)
      log "level=CRITICAL refused_remove path=$target"
      return 1
      ;;
  esac
}

read_tag_file() {
  local path="$1"
  if [ -s "$path" ]; then
    tr -d '\r\n' < "$path"
  fi
}

# Linux renameat2(RENAME_EXCHANGE) swaps two existing paths in one filesystem
# atomically: readers always see either the old or the new active directory.
atomic_exchange() {
  local left="$1"
  local right="$2"
  if [ -n "$EXCHANGE_BIN" ]; then
    "$EXCHANGE_BIN" "$left" "$right"
    local exchange_rc=$?
    return "$exchange_rc"
  fi
  "$PYTHON_BIN" - "$left" "$right" 2>>"$LOG" <<'PY'
import ctypes
import os
import sys

left, right = map(os.fsencode, sys.argv[1:])
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = getattr(libc, "renameat2", None)
if renameat2 is None:
    raise SystemExit("libc does not provide renameat2")
renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
renameat2.restype = ctypes.c_int
AT_FDCWD = -100
RENAME_EXCHANGE = 2
if renameat2(AT_FDCWD, left, AT_FDCWD, right, RENAME_EXCHANGE) != 0:
    err = ctypes.get_errno()
    raise OSError(err, os.strerror(err), os.fsdecode(left), os.fsdecode(right))
PY
}

# A leftover rollback directory means staging or an atomic exchange was
# interrupted. The active directory carries its release tag. If that tag
# matches the atomically replaced legacy tag file, the active tree is committed
# (or still the original tree) and the rollback/staging tree can be discarded.
# Otherwise exchange the directories again to restore the old tree.
recover_interrupted_swap() {
  [ -e "$ROLLBACK_DIR" ] || return 0

  if [ ! -e "$ACTIVE_DIR" ]; then
    mv -- "$ROLLBACK_DIR" "$ACTIVE_DIR"
    log "level=WARN recovery=restored_old reason=active_missing"
    return 0
  fi

  local active_tag rollback_tag legacy_tag
  active_tag="$(read_tag_file "$ACTIVE_DIR/.qlib-release" || true)"
  rollback_tag="$(read_tag_file "$ROLLBACK_DIR/.qlib-release" || true)"
  legacy_tag="$(read_tag_file "$TAG_FILE" || true)"
  if [ -z "$legacy_tag" ]; then
    log "level=CRITICAL recovery_ambiguous reason=legacy_tag_missing active_tag=${active_tag:-none} rollback_tag=${rollback_tag:-none}"
    return 1
  fi

  # Active already agrees with the committed tag: rollback is either fully
  # validated pre-exchange staging or the old tree after a completed commit.
  if [ -n "$active_tag" ] && [ "$active_tag" = "$legacy_tag" ]; then
    if safe_remove_tree "$ROLLBACK_DIR"; then
      log "level=WARN recovery=discarded_nonactive_tree active_tag=$active_tag rollback_tag=${rollback_tag:-none}"
      return 0
    fi
    return 1
  fi

  # The initially deployed legacy tree has no embedded tag. A tagged rollback
  # that differs from the legacy .tag can therefore only be pre-exchange
  # staging; discard it without touching the active tree.
  if [ -z "$active_tag" ] && [ -n "$rollback_tag" ] && [ "$rollback_tag" != "$legacy_tag" ]; then
    if safe_remove_tree "$ROLLBACK_DIR"; then
      log "level=WARN recovery=discarded_staging reason=active_legacy_untagged rollback_tag=$rollback_tag"
      return 0
    fi
    return 1
  fi

  # An active embedded tag different from the legacy .tag, paired with either
  # the tagged legacy tree or the one-time untagged legacy tree in rollback,
  # uniquely identifies an exchanged but uncommitted transaction.
  if [ -n "$active_tag" ] && [ "$active_tag" != "$legacy_tag" ] && \
     { [ -z "$rollback_tag" ] || [ "$rollback_tag" = "$legacy_tag" ]; }; then
    if atomic_exchange "$ACTIVE_DIR" "$ROLLBACK_DIR" && safe_remove_tree "$ROLLBACK_DIR"; then
      log "level=WARN recovery=restored_old reason=tag_not_committed"
      return 0
    fi
    log "level=CRITICAL recovery_failed rollback=$ROLLBACK_DIR"
    return 1
  fi

  log "level=CRITICAL recovery_ambiguous active_tag=${active_tag:-none} rollback_tag=${rollback_tag:-none} legacy_tag=$legacy_tag"
  return 1
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM HUP
  set +e

  if [ "$COMMITTED" -eq 0 ] && [ -e "$ROLLBACK_DIR" ]; then
    recover_interrupted_swap || rc=1
  fi
  if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
    safe_remove_tree "$WORK_DIR" || rc=1
  fi

  exit "$rc"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [ "${QLIB_TEST_MODE:-0}" = "1" ] && [ -n "${QLIB_TEST_PID_FILE:-}" ]; then
  printf '%s\n' "$$" > "$QLIB_TEST_PID_FILE"
fi

maybe_fail_for_test() {
  local point="$1"
  if [ "${QLIB_TEST_MODE:-0}" = "1" ] && [ "${QLIB_TEST_FAILPOINT:-}" = "$point" ]; then
    log "level=ERROR injected_failpoint=$point"
    return 97
  fi
  return 0
}

maybe_pause_for_test() {
  local point="$1"
  if [ "${QLIB_TEST_MODE:-0}" = "1" ] && [ "${QLIB_TEST_PAUSEPOINT:-}" = "$point" ]; then
    log "level=INFO injected_pausepoint=$point"
    while :; do
      sleep 1
    done
  fi
}

file_size() {
  if [ -f "$1" ]; then
    wc -c < "$1" | tr -d '[:space:]'
  else
    printf '0\n'
  fi
}

prepare_download_cache() {
  case "$ASSET_NAME" in
    ''|*/*|.|..) die "unsafe_asset_name name=$ASSET_NAME" ;;
  esac

  if [ -e "$DOWNLOAD_CACHE_DIR" ] || [ -L "$DOWNLOAD_CACHE_DIR" ]; then
    [ -d "$DOWNLOAD_CACHE_DIR" ] && [ ! -L "$DOWNLOAD_CACHE_DIR" ] || \
      die "download_cache_not_directory path=$DOWNLOAD_CACHE_DIR"
    [ -O "$DOWNLOAD_CACHE_DIR" ] || die "download_cache_wrong_owner path=$DOWNLOAD_CACHE_DIR"
  else
    mkdir -m 700 -- "$DOWNLOAD_CACHE_DIR" || die "download_cache_mkdir_failed path=$DOWNLOAD_CACHE_DIR"
  fi
  chmod 700 "$DOWNLOAD_CACHE_DIR" || die "download_cache_chmod_failed path=$DOWNLOAD_CACHE_DIR"

  local item
  for item in "$CACHE_META" "$CACHE_TARBALL"; do
    if [ -e "$item" ] || [ -L "$item" ]; then
      [ -f "$item" ] && [ ! -L "$item" ] || die "download_cache_unsafe_entry path=$item"
      [ -O "$item" ] || die "download_cache_wrong_owner path=$item"
      chmod 600 "$item" || die "download_cache_chmod_failed path=$item"
    fi
  done
}

reset_download_cache() {
  rm -f -- "$CACHE_META" "$CACHE_TARBALL"
}

validate_runtime_setting CONNECT_TIMEOUT "$CONNECT_TIMEOUT"
validate_runtime_setting API_MAX_TIME "$API_MAX_TIME"
validate_runtime_setting DOWNLOAD_MAX_TIME "$DOWNLOAD_MAX_TIME"
validate_runtime_setting RETRY_ATTEMPTS "$RETRY_ATTEMPTS"
validate_runtime_setting RETRY_DELAY "$RETRY_DELAY"
[ "$RETRY_ATTEMPTS" -gt 0 ] || die "invalid_setting name=RETRY_ATTEMPTS value=0"

case "$DATA_DIR" in
  /*) ;;
  *) die "data_dir_not_absolute path=$DATA_DIR" ;;
esac
[ "$DATA_DIR" != "/" ] || die "unsafe_data_dir path=/"
[ -d "$DATA_DIR" ] || die "data_dir_missing path=$DATA_DIR"
[ -d "$ACTIVE_DIR" ] || die "active_dir_missing path=$ACTIVE_DIR"

for required_bin in "$CURL_BIN" "$FLOCK_BIN" "$PYTHON_BIN" "$TAR_BIN" "$GZIP_BIN" "$SHA256_BIN"; do
  command -v "$required_bin" >/dev/null 2>&1 || die "missing_command command=$required_bin"
done
if [ -n "$EXCHANGE_BIN" ]; then
  command -v "$EXCHANGE_BIN" >/dev/null 2>&1 || die "missing_command command=$EXCHANGE_BIN"
fi

exec 9>"$LOCK_FILE"
if ! "$FLOCK_BIN" -n 9; then
  log "level=WARN result=locked action=skip"
  exit 75
fi

recover_interrupted_swap || die "startup_recovery_failed"

WORK_DIR="$(mktemp -d "$DATA_DIR/.qlib-update.XXXXXX")" || die "mktemp_failed"
RELEASE_JSON="$WORK_DIR/release.json"
META_FILE="$WORK_DIR/release.meta"
TARBALL=""
NEW_DIR="$WORK_DIR/cn_data"
TAG_TMP="$WORK_DIR/.tag.new"

log "level=INFO event=update_start"

curl_api_common=(
  --fail-with-body --silent --show-error --location
  --proto '=https' --proto-redir '=https' --tlsv1.2
  --connect-timeout "$CONNECT_TIMEOUT" --max-time "$API_MAX_TIME"
  --retry 2 --retry-delay 2 --retry-all-errors
  --output "$RELEASE_JSON"
)

if "$CURL_BIN" "${curl_api_common[@]}" --noproxy '*' "$API_URL" >>"$LOG" 2>&1; then
  log "level=INFO release_api=direct result=ok"
else
  api_rc=$?
  log "level=WARN release_api=direct result=failed curl_rc=$api_rc"
  if "$CURL_BIN" "${curl_api_common[@]}" --proxy "$PROXY" --noproxy '' "$API_URL" >>"$LOG" 2>&1; then
    log "level=INFO release_api=proxy result=ok"
  else
    api_rc=$?
    die "release_api_failed curl_rc=$api_rc"
  fi
fi

if ! "$PYTHON_BIN" - "$RELEASE_JSON" "$ASSET_NAME" "$REPO" >"$META_FILE" 2>>"$LOG" <<'PY'
import datetime as dt
import json
import os
import re
import sys
from urllib.parse import urlsplit

release_path, asset_name, repo = sys.argv[1:]
with open(release_path, "r", encoding="utf-8") as fh:
    release = json.load(fh)

tag = release.get("tag_name")
if not isinstance(tag, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", tag):
    raise SystemExit("release tag is not an ISO date")
dt.date.fromisoformat(tag)

assets = [a for a in release.get("assets", []) if a.get("name") == asset_name]
if len(assets) != 1:
    raise SystemExit(f"expected exactly one {asset_name!r} asset, got {len(assets)}")
asset = assets[0]

size = asset.get("size")
if not isinstance(size, int) or size <= 0:
    raise SystemExit("asset size is missing or invalid")

url = asset.get("browser_download_url")
if not isinstance(url, str):
    raise SystemExit("asset URL is missing")
parts = urlsplit(url)
expected_path = f"/{repo}/releases/download/{tag}/{asset_name}"
if parts.scheme != "https" or parts.hostname != "github.com" or parts.path != expected_path:
    raise SystemExit("asset URL is outside the expected GitHub release path")

digest = asset.get("digest")
if not digest:
    override = os.environ.get("QLIB_EXPECTED_SHA256", "")
    digest = f"sha256:{override}" if override else ""
if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
    raise SystemExit("a trusted sha256 asset digest is required")

print(tag)
print(url)
print(size)
print(digest.lower())
PY
then
  die "release_metadata_invalid"
fi

LATEST_TAG="$(sed -n '1p' "$META_FILE")"
LATEST_URL="$(sed -n '2p' "$META_FILE")"
LATEST_EXPECTED_SIZE="$(sed -n '3p' "$META_FILE")"
LATEST_DIGEST="$(sed -n '4p' "$META_FILE")"
[ -n "$LATEST_TAG" ] && [ -n "$LATEST_URL" ] && [ -n "$LATEST_EXPECTED_SIZE" ] && [ -n "$LATEST_DIGEST" ] || \
  die "release_metadata_incomplete"

ACTIVE_TAG="$(read_tag_file "$ACTIVE_DIR/.qlib-release" || true)"
LEGACY_TAG="$(read_tag_file "$TAG_FILE" || true)"
if [ -n "$ACTIVE_TAG" ] && [ -n "$LEGACY_TAG" ] && [ "$ACTIVE_TAG" != "$LEGACY_TAG" ]; then
  die "current_tag_inconsistent active=$ACTIVE_TAG legacy=$LEGACY_TAG"
fi
CURRENT_TAG="${ACTIVE_TAG:-$LEGACY_TAG}"
prepare_download_cache
if [ "$CURRENT_TAG" = "$LATEST_TAG" ]; then
  if [ -e "$CACHE_META" ] || [ -e "$CACHE_TARBALL" ]; then
    reset_download_cache
    log "level=INFO download_cache=cleared reason=current_is_latest tag=$LATEST_TAG"
  fi
  log "level=INFO result=skipped_existing tag=$LATEST_TAG"
  exit 0
fi

if ! "$PYTHON_BIN" - "$CURRENT_TAG" "$LATEST_TAG" 2>>"$LOG" <<'PY'
import datetime as dt
import sys

current, latest = sys.argv[1:]
latest_date = dt.date.fromisoformat(latest)
if latest_date > dt.date.today():
    raise SystemExit("release tag is in the future")
if current:
    current_date = dt.date.fromisoformat(current)
    if latest_date < current_date:
        raise SystemExit("release tag regressed")
PY
then
  die "release_tag_invalid current=$CURRENT_TAG latest=$LATEST_TAG"
fi

TAG="$LATEST_TAG"
URL="$LATEST_URL"
EXPECTED_SIZE="$LATEST_EXPECTED_SIZE"
DIGEST="$LATEST_DIGEST"
CACHE_STATE="new"
CACHE_META_VALIDATED="$WORK_DIR/cache.meta.validated"

# Preserve a partial asset across process and day boundaries. If a newer
# upstream release appears while an older asset has real progress, finish the
# trusted cached release first; after it commits, the next natural run can jump
# to the newest release. This avoids throwing away every slow night's bytes.
if [ -s "$CACHE_META" ] && [ -e "$CACHE_TARBALL" ]; then
  if "$PYTHON_BIN" - "$CACHE_META" "$ASSET_NAME" "$REPO" "$CURRENT_TAG" "$LATEST_TAG" >"$CACHE_META_VALIDATED" 2>>"$LOG" <<'PY'
import datetime as dt
import pathlib
import re
import sys
from urllib.parse import urlsplit

path, asset_name, repo, current, latest = sys.argv[1:]
lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
if len(lines) != 4:
    raise SystemExit("cached metadata must contain exactly four lines")
tag, url, size, digest = lines
tag_date = dt.date.fromisoformat(tag)
latest_date = dt.date.fromisoformat(latest)
if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", tag):
    raise SystemExit("cached tag is not an ISO date")
if current and tag_date <= dt.date.fromisoformat(current):
    raise SystemExit("cached tag is not newer than current")
if tag_date > latest_date or tag_date > dt.date.today():
    raise SystemExit("cached tag is newer than latest or today")
if not size.isdigit() or int(size) <= 0:
    raise SystemExit("cached size is invalid")
if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    raise SystemExit("cached digest is invalid")
parts = urlsplit(url)
expected_path = f"/{repo}/releases/download/{tag}/{asset_name}"
if parts.scheme != "https" or parts.hostname != "github.com" or parts.path != expected_path:
    raise SystemExit("cached URL is outside the expected release path")
print(tag)
print(url)
print(size)
print(digest)
PY
  then
    cached_size="$(file_size "$CACHE_TARBALL")"
    cached_expected="$(sed -n '3p' "$CACHE_META_VALIDATED")"
    if [ "$cached_size" -gt 0 ] && [ "$cached_size" -le "$cached_expected" ]; then
      TAG="$(sed -n '1p' "$CACHE_META_VALIDATED")"
      URL="$(sed -n '2p' "$CACHE_META_VALIDATED")"
      EXPECTED_SIZE="$cached_expected"
      DIGEST="$(sed -n '4p' "$CACHE_META_VALIDATED")"
      CACHE_STATE="resume"
    else
      log "level=WARN download_cache=reset reason=invalid_partial_size actual=$cached_size expected=$cached_expected"
      reset_download_cache
    fi
  else
    log "level=WARN download_cache=reset reason=invalid_metadata"
    reset_download_cache
  fi
elif [ -e "$CACHE_META" ] || [ -e "$CACHE_TARBALL" ]; then
  log "level=WARN download_cache=reset reason=incomplete_pair"
  reset_download_cache
fi

if [ "$CACHE_STATE" = "new" ]; then
  : > "$CACHE_TARBALL"
  chmod 600 "$CACHE_TARBALL"
  cp -- "$META_FILE" "$WORK_DIR/cache.meta.new"
  chmod 600 "$WORK_DIR/cache.meta.new"
  mv -f -- "$WORK_DIR/cache.meta.new" "$CACHE_META"
fi
TARBALL="$CACHE_TARBALL"

log "level=INFO latest_tag=$LATEST_TAG selected_tag=$TAG current_tag=${CURRENT_TAG:-none} cache=$CACHE_STATE expected_size=$EXPECTED_SIZE digest=$DIGEST"

download_complete=0
attempt=1
transport=direct
while [ "$attempt" -le "$RETRY_ATTEMPTS" ]; do
  before="$(file_size "$TARBALL")"
  # The attempt budget is shared by both routes, not multiplied per route.
  # Explicit noproxy avoids inherited NO_PROXY silently bypassing the fallback.
  if [ "$transport" = "direct" ]; then
    curl_transport=(--noproxy '*')
  else
    curl_transport=(--proxy "$PROXY" --noproxy '')
  fi
  if "$CURL_BIN" \
      --fail --silent --show-error --location \
      --proto '=https' --proto-redir '=https' --tlsv1.2 \
      "${curl_transport[@]}" \
      --connect-timeout "$CONNECT_TIMEOUT" --max-time "$DOWNLOAD_MAX_TIME" \
      --continue-at - --output "$TARBALL" "$URL" >>"$LOG" 2>&1; then
    curl_rc=0
  else
    curl_rc=$?
  fi
  after="$(file_size "$TARBALL")"
  log "level=INFO event=download_attempt attempt=$attempt transport=$transport curl_rc=$curl_rc bytes_before=$before bytes_after=$after expected=$EXPECTED_SIZE"

  if [ "$after" -gt "$EXPECTED_SIZE" ]; then
    reset_download_cache
    die "download_oversize actual=$after expected=$EXPECTED_SIZE"
  fi
  if [ "$after" -eq "$EXPECTED_SIZE" ]; then
    download_complete=1
    break
  fi

  attempt=$((attempt + 1))
  if [ "$attempt" -le "$RETRY_ATTEMPTS" ]; then
    next_transport="$transport"
    if [ "$transport" = "direct" ]; then
      next_transport=proxy
    elif [ "$after" -le "$before" ]; then
      next_transport=direct
    fi
    if [ "$next_transport" != "$transport" ]; then
      log "level=WARN event=download_transport_fallback from=$transport to=$next_transport retained_bytes=$after"
      transport="$next_transport"
    fi
    sleep "$RETRY_DELAY"
  fi
done

[ "$download_complete" -eq 1 ] || die "download_incomplete actual=$(file_size "$TARBALL") expected=$EXPECTED_SIZE"

ACTUAL_SHA256="$($SHA256_BIN "$TARBALL" | awk '{print tolower($1)}')"
EXPECTED_SHA256="${DIGEST#sha256:}"
if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
  reset_download_cache
  die "digest_mismatch actual=$ACTUAL_SHA256 expected=$EXPECTED_SHA256"
fi
if ! "$GZIP_BIN" -t "$TARBALL" 2>>"$LOG"; then
  reset_download_cache
  die "gzip_validation_failed"
fi

# The cache is only for incomplete transport state. Once size, digest and gzip
# are trusted, move the archive back under the disposable transaction tree so
# later validation failures cannot pin a bad release forever.
WORK_TARBALL="$WORK_DIR/$ASSET_NAME"
mv -- "$TARBALL" "$WORK_TARBALL" || die "download_cache_consume_failed"
rm -f -- "$CACHE_META"
TARBALL="$WORK_TARBALL"
log "level=INFO download_cache=consumed tag=$TAG"

ARCHIVE_LIST="$WORK_DIR/archive.list"
"$TAR_BIN" -tzf "$TARBALL" >"$ARCHIVE_LIST" 2>>"$LOG" || die "tar_list_failed"
if ! "$PYTHON_BIN" - "$TARBALL" 2>>"$LOG" <<'PY'
import pathlib
import sys
import tarfile

with tarfile.open(sys.argv[1], mode="r:gz") as archive:
    members = archive.getmembers()
if not members:
    raise SystemExit("archive is empty")
roots = set()
seen = set()
for member in members:
    entry = member.name.rstrip("/")
    path = pathlib.PurePosixPath(entry)
    if not entry or entry == ".":
        raise SystemExit("archive contains an empty path")
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"unsafe archive path: {entry}")
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise SystemExit(f"unsafe archive member type: {entry}")
    if not (member.isfile() or member.isdir()):
        raise SystemExit(f"unsupported archive member type: {entry}")
    normalized = path.as_posix()
    if normalized in seen:
        raise SystemExit(f"duplicate archive path: {entry}")
    seen.add(normalized)
    roots.add(path.parts[0])
    if len(path.parts) == 1 and not member.isdir():
        raise SystemExit(f"top-level archive member is not a directory: {entry}")
if len(roots) != 1:
    raise SystemExit(f"archive must have one top-level directory, got {sorted(roots)}")
PY
then
  die "archive_member_validation_failed"
fi

mkdir "$NEW_DIR"
"$TAR_BIN" -xzf "$TARBALL" -C "$NEW_DIR" --strip-components 1 2>>"$LOG" || die "extraction_failed"

[ -s "$NEW_DIR/calendars/day.txt" ] || die "required_file_missing path=calendars/day.txt"
[ -d "$NEW_DIR/features" ] || die "required_dir_missing path=features"
[ -d "$NEW_DIR/instruments" ] || die "required_dir_missing path=instruments"
find "$NEW_DIR/features" -type f -print -quit | grep -q . || die "features_empty"
find "$NEW_DIR/instruments" -type f -print -quit | grep -q . || die "instruments_empty"

LAST_DAY="$(tail -n 1 "$NEW_DIR/calendars/day.txt" | tr -d '\r\n')"
CURRENT_LAST_DAY="$(tail -n 1 "$ACTIVE_DIR/calendars/day.txt" 2>/dev/null | tr -d '\r\n' || true)"
if ! "$PYTHON_BIN" - "$CURRENT_LAST_DAY" "$LAST_DAY" "$TAG" "$NEW_DIR/calendars/day.txt" 2>>"$LOG" <<'PY'
import datetime as dt
import sys

current, new, release, calendar_path = sys.argv[1:]
with open(calendar_path, encoding="utf-8") as fh:
    values = [line.strip() for line in fh if line.strip()]
if not values:
    raise SystemExit("calendar is empty")
parsed = [dt.date.fromisoformat(value) for value in values]
if parsed != sorted(parsed) or len(set(parsed)) != len(parsed):
    raise SystemExit("calendar is not strictly increasing")
if values[-1] != new:
    raise SystemExit("calendar tail changed while validating")
new_date = dt.date.fromisoformat(new)
release_date = dt.date.fromisoformat(release)
if new_date > release_date or new_date > dt.date.today():
    raise SystemExit("new trading day is in the future or after the release tag")
if current and new_date < dt.date.fromisoformat(current):
    raise SystemExit("new trading day regressed")
PY
then
  die "calendar_validation_failed current=$CURRENT_LAST_DAY new=$LAST_DAY tag=$TAG"
fi

printf '%s\n' "$TAG" > "$NEW_DIR/.qlib-release"
printf '%s  %s\n' "$EXPECTED_SHA256" "$ASSET_NAME" > "$NEW_DIR/.qlib-asset.sha256"
printf '%s\n' "$TAG" > "$TAG_TMP"

# Publish the fully validated staging tree to the fixed exchange path with a
# same-filesystem rename. No partially extracted tree ever reaches that path.
[ ! -e "$ROLLBACK_DIR" ] || die "rollback_path_busy path=$ROLLBACK_DIR"
mv -- "$NEW_DIR" "$ROLLBACK_DIR"

# Fail-before-swap: no production path has been touched before this point.
maybe_fail_for_test before_swap
maybe_pause_for_test before_swap

# renameat2 exchanges ROLLBACK_DIR with ACTIVE_DIR in one filesystem operation;
# afterwards ACTIVE_DIR is new and ROLLBACK_DIR is old.
atomic_exchange "$ACTIVE_DIR" "$ROLLBACK_DIR" || die "atomic_exchange_failed"
maybe_fail_for_test after_exchange
maybe_pause_for_test after_exchange

# The tag replacement is atomic on the same filesystem. If the process exits
# before this succeeds, the EXIT trap sees a tag mismatch and restores old data.
maybe_fail_for_test before_tag_commit
mv -f -- "$TAG_TMP" "$TAG_FILE"
COMMITTED=1

if ! safe_remove_tree "$ROLLBACK_DIR"; then
  log "level=WARN cleanup=rollback_retained tag=$TAG"
fi
log "level=INFO result=updated tag=$TAG last_day=$LAST_DAY size=$EXPECTED_SIZE sha256=$EXPECTED_SHA256"
