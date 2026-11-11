"""Shared source-tolerance policy for the collector and the read-only audit.

TongDaXin (mootdx) quote servers refuse this host's datacenter egress IP at the
protocol layer: TCP connect and metadata APIs answer, but quotes/bars return
empty on every HQ server (reproduced with tdxpy and pytdx since ~2026-09-10).
mootdx is therefore an optional source; tencent and baostock remain the
authoritative watch-code quote sources.

Every path that inspects source errors must apply this same policy:
  - daily_snapshot.validate_source_completeness (write admission)
  - daily_snapshot.existing_snapshot (idempotent re-read of a stored row)
  - data_health.has_source_error (nightly read-only audit)
Otherwise a row is valid at write time and invalid on re-read, which blocks
idempotent re-runs and reports false source errors as business incidents.

The recorded error is evidence, never something to delete or overwrite: a
tolerated error path stays in the stored payload exactly as collected.
"""

OPTIONAL_SOURCE_ERRORS = frozenset({"mootdx.error"})


def is_optional_source_error(path):
    """True only for an exact tolerated error path such as 'mootdx.error'."""
    return isinstance(path, str) and path in OPTIONAL_SOURCE_ERRORS


def required_source_errors(paths):
    """Keep every error path except the tolerated optional-source ones."""
    return [path for path in paths if not is_optional_source_error(path)]
