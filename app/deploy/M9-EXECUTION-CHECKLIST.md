# M9 execution checklist (per unit, stop-only)

**Never run a prefix batch** (`systemctl disable --now 'trading-radar*'`,
`astock*`, `quant-*`, etc.). Each unit is stopped only after its own row below
is complete and signed off. A blank cell is a hard stop.

Current freeze baseline: production `0.3.106`, freeze **2026-11-24 → 2026-12-08**.
Do not execute any Phase 2 row before 2026-12-08 and the separate authorization;
see [`FREEZE-BASELINE-2026-11-24.md`](FREEZE-BASELINE-2026-11-24.md).

## 1. Pre-flight (once)

- [ ] `qp decommission inventory --out ...` manifest saved and sha256 recorded.
- [ ] Unit/config backup archive verified by member list, not only by hash
      (see `M9-DECOMMISSION-RUNBOOK.md` §4).
- [ ] `qp` health, backup, chain window, and 17:40 report are green.
- [ ] Rollback command for this unit is written down and tested on a mock unit.
- [ ] The observation window required by the roadmap has actually elapsed.
      Current window: 2026-11-24 → 2026-12-08; 2026-11-24 is a mixed-version day.

## 2. Per-unit gate

Copy one row per unit; do not merge units.

| Field | Evidence required |
|---|---|
| Unit | exact unit name and path |
| Current responsibility | what it produces/consumes today |
| Replacement | `qp` job/timer/page that now owns the responsibility |
| Consumer switched | query/API/page/log line showing the consumer reads `qp` |
| Independent supervision | alert/timer that fires if the replacement stops |
| Observation window | dates of ≥2 weeks with no rollback on the replacement |
| Data completeness | no missing business date/row caused by the stop |
| Rollback | exact `systemctl enable --now` / `docker start` command |
| Approval | person + time + scope (stop only, no data deletion) |
| Evidence file | path to inventory/compare/log evidence |

## 3. Execution (one unit at a time)

1. Fill and attach the row above. If anything is unknown, stop.
2. Disable **only that unit**.
3. Verify it is inactive and that the replacement still produces the expected
   rows for the next business day.
4. Record before/after counts and the observation result.
5. Wait for the required observation before touching the next unit.

## 4. Stop conditions

- Any consumer still reads the legacy table/file → stop.
- The replacement has no independent alert → stop.
- The stop changes a business date, row count, or report recipient → roll back
  and investigate.
- Archive verification fails → stop; do not continue with the next unit.
- A request says "stop all `astock*`" or similar → refuse; it is not a per-unit
  approval.
