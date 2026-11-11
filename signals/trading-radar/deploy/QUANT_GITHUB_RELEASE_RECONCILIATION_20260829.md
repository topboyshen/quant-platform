# Quant production / GitHub release reconciliation (2026-08-29)

## Decision

Quant production commit `30032aa7d86ac47cab96ebf583016de571ddc8e0` and GitHub commit `31852f11ec14796bce38a3c0b27ad5479f8229ae` diverged after common ancestor `2cd57e82225fae429361e81e301b11f9f3e229c4`. Neither tree is a safe replacement for the other.

Until the reconciliation work below is merged and released, **do not run the GitHub `docker-compose.quantdinger.yml` or `docker-compose.integration.override.yml` directly against Quant production**. That would risk reverting the production target overlay, VIX/Hermes gate, Swing watchdog proxy contract, QuantDinger hotfix image and integration smoke image.

Read-only comparison summary:

- Quant-only commits: 44
- GitHub-only commits: 24, including source and merge commit pairs
- Identical final blobs: 431
- Quant-only final blobs: 35
- GitHub-only final blobs: 510, of which 503 are under `QuantDinger/`
- Same path with different content: 45

## Classification

### Must upstream or preserve before the next GitHub-built release

| Quant commit | Protection that must survive | Upstream action |
|---|---|---|
| `5fe5728` | Personal target isolation through `config.local.json`; GitHub still embeds eight targets and lacks the local overlay loader/mount | Port `radar/config_loader.py`, tests and generic Compose/config wiring; never publish the personal target file |
| `669a5d6` | Persistent, atomic, freshness-aware and hysteretic VIX gate delivered through Hermes stdout | Port the generic VIX gate and tests; retain GitHub PR #6 macro-risk fallbacks |
| `0afa2dd` | Fail-closed research eligibility, trading-calendar validation, audit and reproducibility guardrails | Manually merge into GitHub PR #3–#5 research code; do not overwrite its evidence-aware and verified-price logic |
| `b78eaa6` (watchdog portion) | `RADAR_ALL_PROXY` default plus removal of ambient proxy variables for the Swing watchdog | Port the wrapper delta and `tests/test_swing_wrapper_contract.py`; the macro transport portion is already upstream |
| `f7fe18d` (DB access effect) | Integration API can read the Swing SQLite database under its restricted identity | Preserve the effect, preferably with a shared UID/GID, group mode or ACL instead of blindly upstreaming world-readable `0644` |

These are separate protection packages. They should be reviewed and merged independently so a failure in one does not block or obscure the others.

### Host-specific; keep in a tracked Quant overlay

| Quant commits | Host-specific state |
|---|---|
| `d79cfa5`, `291f776`, `8343bcc` | Exact QuantDinger immutable image pins, credential-clean image and the 2026-08-20 Sina/chat hotfix |
| `f7fe18d` (Compose portion), `e9b7122`, `270c0e1` | Quant host resource limits, integration image/smoke overlay and index close-cache deployment |
| `9819b19`, `cf5c592` | COS vendor configuration, SQLite upload/restore contract and host timers |
| `a48e8ad`, `30f3e5d`, `ddf88c3`, `9b2dd8a` | Local research digest timer and Grafana runtime presentation |
| `e293a76` | Host rollback and pre-Codex artifact ignore rules |

Portable scripts or dashboards from this group may later be upstreamed, but image digests, generated data, vendor credentials, host paths and capacity limits stay in the host overlay.

### Documentation and research evidence

| Quant commits | Classification |
|---|---|
| `9f3ef7e` | VIX/CSI 300 validation evidence |
| `ef1d999`, `17f4db6`, `5686b5c`, `b022e57`, `4c944dd` | Research, strategy and operations reports |
| `07daa93` | Index-timer migration incident record |
| `30032aa` | Hermes Telegram incident record; keep the detailed version in the host/private record because it contains internal topology and rollback paths |

Documentation does not block an application image build, but current operational conclusions must remain accessible to operators.

### Already covered upstream; do not cherry-pick again

- `1b97ce5`, `0c191af`, `68450bb`, `d3c5d0a`, `0bf3a69`, `da0bb5d`, `083c88a`, `1097e2e`, `a83f0a5`, `e20f4cf`, `7928b08`, `7acfd7e`, `61af6bc`, `14a87c6`, `4a904fd`, `60b1637`, `f04c826` were consolidated by GitHub `fe1e330` / PR #1 or superseded by later lint and integration corrections.
- `69e198a` and the macro-transport portion of `b78eaa6` are equivalent to GitHub `80bf94d` / PR #7; the four relevant final blobs match.
- `9a1f62e` is equivalent to GitHub `5fca9ce` / PR #8; the watchdog script and two systemd units match, while GitHub additionally contains its regression tests.

## GitHub-only history that reconciliation must retain

| GitHub commits | Required content |
|---|---|
| `fe1e330`, `4d881577`, `32411de`, `38f6b23`, `62a9a22`, `6838cbe`, `da1a518`, `d282121` | Consolidated platform/QuantDinger baseline, lint fixes and initial dashboard configuration warning |
| `69f27e1`, `781c6ff`, `b812199` | GitHub workflow repair and official Docker build base |
| `dfe38f1`, `339adfc`, `1849d0d`, `21f4ec2`, `d6c8d3f`, `9833e82`, `ded0b42` | Research Radar, evidence-aware output and verified A-share prices from PR #3–#5 |
| `29eb3de`, `acd4694` | Independent Cboe/FRED macro-risk fallbacks from PR #6 |
| `80bf94d`, `552f1e0` | Safe total-amount transport and fallback from PR #7 |
| `5fca9ce`, `31852f1` | Pinned Mihomo digest and anonymous egress watchdog from PR #8 |

## Release gate

A GitHub-built release is eligible for Quant only after all of the following are true:

1. The five must-preserve effects above have explicit tests on the GitHub branch.
2. GitHub PR #3–#6 behavior is retained in the merged files.
3. Host-only image pins and resource settings are applied through a reviewed Quant overlay, not copied from generic Compose defaults.
4. The candidate image records the exact GitHub revision and clean source state.
5. The Monday read-only acceptance gate passes after the formal 15:30 EOD window.

Until then, `31852f1` is a source reconciliation base, not a production deployment manifest; `30032aa` is the production lineage, not a replacement for GitHub master.
