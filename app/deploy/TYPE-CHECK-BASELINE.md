# Type-check baseline (2026-11-25)

Scope: the report-only mypy set from `.github/workflows/ci.yml`
(`qp/jobs qp/signals qp/models`), measured at `main` commit `a2d615a` with
**mypy 2.4.1** and `--ignore-missing-imports`.

## 1. Gate

`qp/models` is now **blocking** in CI (added to the critical-module mypy step).
The report-only step remains for `qp/jobs` and `qp/signals`.

```bash
# blocking
mypy --ignore-missing-imports --follow-imports=silent \
  qp/jobs/gates.py qp/signals/acceptance.py qp/signals/attribution.py \
  qp/api/overview.py qp/api/chain.py \
  qp/signals/chain_base.py qp/signals/chain_writer.py qp/signals/chain_snapshot.py \
  qp/market qp/reports qp/models

# report-only baseline
mypy --ignore-missing-imports qp/jobs qp/signals
```

## 2. Recorded baseline

| Module | Errors at `a2d615a` | Status |
|---|---:|---|
| `qp/models` | **0** (10 files) | blocking |
| `qp/jobs` | 152 | report-only |
| `qp/signals` | 152 | report-only |

The counts are informational snapshots, not a ratchet: the blocking gate is the
mypy command on the listed modules, and the report-only step stays
`continue-on-error` until a module reaches zero and is promoted.

## 3. Gate check

Temporarily appending a wrong assignment to `qp/models/signals.py`:

```python
_typecheck_probe: int = "not-an-int"
```

produced:

```
qp/models/signals.py:423: error: Incompatible types in assignment
  (expression has type "str", variable has type "int")  [assignment]
```

Reverting the probe returned `Success: no issues found in 10 source files`.
The existing critical modules and `qp/market`/`qp/reports` continue to pass.

## 4. Next candidates

`qp/jobs` and `qp/signals` each carry 152 report-only errors. Promote a
submodule (or fix a file to zero) rather than the whole package; repeat this
record with the new commit and error count. Do not add an error-count baseline
that silently accepts new errors.
