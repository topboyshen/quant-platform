# quant-platform

Monorepo merging the nnG8 trading-radar / QuantDinger / research stacks (phase 2, 2026-11-11).
Runtime paths on nnG8 are **unchanged** by this repo (P1); services still run from their original locations.

| Module | Path | Source on nnG8 | Import mode |
|---|---|---|---|
| market data | `market-data/research-data` | `~/research-data` (git fabed60) | history + deployed-tree overlay |
| market data | `market-data/qlib-scripts` | `~/qlib/scripts` (non-git) | snapshot |
| research | `research/astock` | `~/AStock_AI_System` (topboyshen/AStock_AI_System 14e8216) | snapshot (history had gitleaks findings) |
| research | `research/research-ledger` | `/opt/research-ledger` (git 7255251) | history + overlay |
| research | `research/research-system-phase2` | `/opt/research-system-phase2` (git 8837c0a) | history |
| signals | `signals/trading-radar` | `~/trading-radar` (topboyshen/trading-radar 679fd53) | snapshot (history had gitleaks findings) |
| signals | `signals/signal-bus` | `~/signal-bus` (topboyshen/signal-bus 2479057) | history |
| execution | `execution/quantdinger` | `~/QuantDinger` (vendor: brokermr810/QuantDinger 3551b8e + local changes) | snapshot (vendor; history had findings) |
| reports | `reports/daily-report-tracking` | `~/releases/daily-report-tracking-release-20261109.2/candidate` | snapshot |
| ops | `ops/dashboard` | `~/ops/dashboard` (git 03a2c17) | history + overlay |
| ops | `ops/opt/*` | `/opt/{nng8-operations,quant-nng8-sync-writer,nng8-sync,qd-*}` | snapshot |
| ops | `ops/nginx` | `/etc/nginx` (sanitized templates) | new |
| web | `web/shell` | new unified entry shell | new |
| web | `web/qd-frontend` | QD Vue source (Quant-Server release etf-named-triggers-20261106.1 build tree) | snapshot |
| web | `web/workbench` | `/opt/nng8-dashboard` static site | snapshot |

Excluded on import: secrets, `.env*`, keys/certs, databases, logs, data/, runtime/, models, archives, node_modules, venvs, release evidence, files > 5 MB.
Original repos remain untouched (read-only; never pushed from here). Third-party: QuantDinger (see its LICENSE/TRADEMARKS), qlib and hermes-agent stay external.
