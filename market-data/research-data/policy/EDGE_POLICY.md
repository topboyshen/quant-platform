# EDGE_POLICY — freeze active strategy work pending a demonstrated edge

**Decided:** 2026-08-18, by human. **Status:** in force. **Reactivation:** only via a new human-approved commit (see criteria below).

## The decision

Without a demonstrated edge source, active strategy development is **stopped**. The system is parked in `EDGE_DISCOVERY` mode. This is not a pause to think — it is a formal rule: **no backtest or code may manufacture trading impulse without a prior edge source.**

## Basis (what is proven vs asserted)

- **Sharpe, William F. (1991).** *The Arithmetic of Active Management.* Financial Analysts Journal, Jan/Feb 1991.
  https://web.stanford.edu/~wfsharpe/art/active/active.htm
  - Before costs, the average actively-managed dollar equals the average passively-managed dollar (arithmetic identity).
  - After costs, the average actively-managed dollar **underperforms** passive.
  - This applies to *active excess return* (near zero-sum after costs), not to total market return (positive-sum from enterprise value, dividends, growth).

- **Legacy forecast chain at the 2026-08-18 freeze:** 120 sealed forecasts and 90 captured outcomes; all 90 outcomes are PENDING_REVIEW. The first 60 outcomes were analyzed as a tombstone cohort, not formally settled — Brier 0.2807 > 0.25 random baseline; threshold win-rate 0.65 < 0.683 majority-class baseline; mean forecast probability 0.165 vs mean outcome 0.317, bias −0.152. A further 30 outcomes were imported before the timers were disabled and are not included in those tombstone statistics. The final 30 forecasts target the next session and have no outcome. No revision/settlement/evaluation is added.

- **First candidate-family test (rf-momentum-overnight-v1):**
  ```yaml
  status: PROVISIONAL_REJECT
  capital_eligible: false
  conclusion: DO_NOT_TRADE
  reason: negative_point_estimate_with_incomplete_small_sample
  n_stock_sessions_scanned: 360
  n_silence_rule_not_triggered: 350
  n_generated: 10
  n_settled: 7
  n_unsettled_data_gap: 3
  n_no_prior_bars: 0
  win_rate: 0.286
  mean_net_return: -0.00735   # net of 20bps round-trip cost
  mean_net_R: -0.178
  sum_net_return: -0.0514
  ci_95_net_return: [-0.0181, 0.0034]   # CI includes 0
  ci_includes_zero: true
  ```
  **Not** recorded as `ELIMINATED`. The CI includes zero, n is small (7 settled, 3 generated-but-unsettled due to missing bars on 08-13/14/17), so the honest status is `PROVISIONAL_REJECT — DO_NOT_TRADE`. (Note: an earlier run display header said "first 8" while only 7 settled rows exist; that was a display artifact, not a data inconsistency — counts above reconcile: 360 = 0 no-prior + 350 silence + 10 generated; 10 = 7 settled + 3 unsettled.)

- **Edge source assessment:** current assets — public OHLC/news, public Qlib/AkShare/LLM, single-machine daily processing, plus a sealing/settlement apparatus — are **research tools, not a unique advantage**. Discipline, patience, and audit capacity reduce errors but do not generate alpha. LLM's only plausible value is accelerating event processing **in a domain you already understand**; absent your domain knowledge / unique data / unique execution, LLM classification is a public capability.

## Machine-readable policy

See `edge-policy.json`. Enforced at startup by `src/policy.py` + `tests/test_policy_gate.py`:
any candidate-generation code path must call `policy.require_generation_allowed()` and will raise `PermissionError` while frozen.

## What is frozen (machine-enforced or scheduler-frozen)

- `candidate_generation_enabled: false`
- `random_rule_search_enabled: false`
- `historical_parameter_search_enabled: false`
- `execution_enabled: false` / `active_capital_fraction: 0`
- **Old forecast chain schedulers disabled** (see `scheduler_freeze_record.json`):
  `research-system-phase2-producer.timer`, `research-system-phase2-settler.timer`,
  `research-ledger-phase2-import.timer`, `research-ledger-worker.timer`.
  Without this, the frozen `120 forecasts / 90 PENDING outcomes` state would keep changing. The timers are now `disabled` (reversible via `systemctl enable --now <timer>`).
- Candidate schema + DDL remain only on the VPS-local legacy branch; they were not pushed to GitHub and are not deployed to the production database (`candidate_schema_in_production_db: false`). They were validated only on disposable test DBs.

## What is left untouched and continues

- Long-running services `research-system-phase2.service` and `research-ledger.service` (dashboards/APIs, not forecast generators) — left running.
- AStock data collection (08:00 morning cron, 22:00 qlib cron) — continues, but **must not auto-promote public data to trade candidates**.
- PostgreSQL `research_ledger` DB, audit tables, backups, and the sealing/settlement/calibration validation code — preserved on standby.

## Legacy chain: 120 forecasts / 90 outcomes

All 90 outcomes remain `PENDING_REVIEW` permanently. The remaining 30 sealed forecasts have no outcome. No revision, settlement, or evaluation is added. The records are kept as an anti-example archive. Only the first 60 outcomes belong to the tombstone metric cohort above; the next 30 outcomes arrived before the 16:06 CST scheduler freeze and remain unanalyzed. The legacy chain is frozen — no new writes, no extension.

## Reactivation criterion

The only way out of `EDGE_DISCOVERY` is a **new human-approved commit** that states, explicitly and falsifiably:
1. **Who ignores what** — the specific thing mispriced.
2. **Why it persists** — why the market has not arbitraged it (and won't).
3. **How to falsify it** — the prospective test that would kill it.

Then: seal the rule before the predicted window and validate prospectively via the preserved apparatus. No reactivation without all three.

## What this is not

Not giving up on making money. It is refusing to spend real money and time buying the illusion of "edge might exist." Until a genuine edge source is brought in, the highest-EV capital policy is low-cost, broadly-diversified, passive — proportion set by your horizon and risk tolerance, outside this system.
