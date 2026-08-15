# Outcome review policy

Outcome review verifies the market fact, not whether the forecast was correct. Probability and model direction must not be used to decide approval.

An outcome may move from Phase2 `PENDING_REVIEW` to `APPROVED` only when the forecast is uniquely bound to its sealed batch, the target session is final, the source snapshot is intact, and an independent source confirms the prior and target-session unadjusted closes and trading status. The binary result is `1` only when target close is strictly greater than prior close; equality is `0`.

Do not approve an item when evidence is unavailable, hashes or symbols disagree, the target session is not final, the prior session is missing, the security is suspended or has zero volume, close values or direction disagree, timestamps/cutoffs are invalid, the forecast was not prospective, records are missing/duplicated, independent sources conflict, or the protocol binding differs.

Canonical reason codes:

- `SOURCE_UNAVAILABLE`
- `SOURCE_HASH_MISMATCH`
- `SYMBOL_BINDING_MISMATCH`
- `TARGET_SESSION_NOT_FINAL`
- `PRIOR_SESSION_MISSING`
- `SUSPENSION_OR_ZERO_VOLUME`
- `CLOSE_VALUE_MISMATCH`
- `DIRECTION_MISMATCH`
- `TIMESTAMP_OR_CUTOFF_INVALID`
- `FORECAST_NOT_PROSPECTIVE`
- `DUPLICATE_OR_MISSING_RECORD`
- `INDEPENDENT_SOURCE_CONFLICT`
- `PROTOCOL_MISMATCH`

An incorrect forecast is never an exclusion reason. Approved Phase2 outcomes become a new Ledger `REVIEWED` outcome revision; the original `PENDING_REVIEW` revision remains immutable, and the formal settlement must point to the new reviewed revision.
