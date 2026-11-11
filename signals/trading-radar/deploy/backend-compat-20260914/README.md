# Existing 09/14 backend compatibility release — tracked provenance

This directory records the already-running overlay. It is not a deploy runner
and creates no backup. Production was NOT rebuilt or restarted on 09/16.

- `release.json`: exact frozen base/result image IDs and old/new source hashes.
- `overlay.patch`: exact diff of three frozen-base files to deployed sources.
- `Dockerfile`: original 09/14 build definition, retained byte-for-byte.

The base is the pinned 08/20 image, not the divergent `~/QuantDinger` checkout.
To reconstruct a future isolated candidate, first verify the base image ID and
each base source hash, apply this patch to those sources only, then verify every
result hash. Do not assume the patch applies to current upstream. Building or
switching images requires its own authorization; this directory does neither.

The 35 compatibility tests in the historical release evidence were run on
09/14, not rerun against production on 09/16. This overlay changes chart
sampling, Tencent response cleanup/status checks and A-share fallback
reachability. It does not repair the A-share execution model or prove profit.

The ignored `releases/backend-compat-20260914.1/` contains the historical
validation and rollout evidence. Its old prepare/deploy scripts depend on a
database dump and must NOT be reused under `deploy/QUANT_BACKUP_POLICY.md`.
The existing dump stays untouched; its presence is not future backup consent.
