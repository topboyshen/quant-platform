# Deterministic dependency locks (2026-11-16)

There are two lock files, generated from the same resolver and kept in sync by
`tests/test_lockfiles.py`:

| File | Used by | Contents |
|---|---|---|
| `app/backend/requirements.lock` | CI backend job, local test env | runtime pins + test extra (`pytest`, `httpx`, `pgserver`) |
| `app/backend/requirements-runtime.lock` | release venv / `/opt/qp/current/requirements.lock` | runtime pins only |

The runtime pins are a strict subset of the test lock. The current runtime lock
hash is `3a22e1b353b9f83bafd0632ac47f09be2c7a77e14d868690064c64b46c234434`
(26 pins), matching the deployed `/opt/qp/current/requirements.lock`.

## Release install

The release bundle contains both files. Build the venv with:

```bash
sudo app/deploy/bin/qp-release-venv /opt/qp/releases/<version>
```

The script:

1. refuses a lock containing an editable `qp @` entry;
2. creates the release venv;
3. `pip install -r backend/requirements-runtime.lock`;
4. `pip install --no-deps backend`;
5. copies the runtime lock to `$REL/requirements.lock`, which is the path used by
   `/opt/qp/current/requirements.lock` and checked by the ETF evidence scan.

Do **not** copy the previous release's lock into a new release. That was the old
behaviour and allowed the CI and production pin sets to drift.

## Regeneration

```bash
cd app/backend
uv pip compile pyproject.toml --extra test --python-version 3.12 \
  --constraint requirements-runtime.lock \
  --no-header --no-annotate -o /tmp/requirements.lock.new
uv pip compile pyproject.toml --python-version 3.12 \
  --constraint requirements-runtime.lock \
  --no-header --no-annotate -o /tmp/requirements-runtime.lock.new
# review the diff, then replace the committed files
```

Notes:

- The constraint file preserves the version currently verified in production.
- `greenlet==3.5.6` is present in the deployed runtime lock; keep it pinned so the
  runtime/test subset check stays meaningful.
- After an intentional dependency change, update the observation freeze
  (`requirements_lock_sha256`) as a maintenance change and record the old/new
  hash. A version change that can alter scoring is a new observation version.
- Never place secrets, `qp @ file:` entries, or unpinned lines in either lock.
