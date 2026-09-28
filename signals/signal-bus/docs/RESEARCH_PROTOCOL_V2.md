# Research summary and calendar-skip wire contracts

This revision changes neither HMAC, source identity, date validation, report
hash gates nor the weekday 08:00–08:13:59 Asia/Shanghai send window. It never
replays a historical report. Deploy the matching Signal Gate validator and
consumer **before** this sender; do not test deployment by sending a report.

## Structured inference summary, version 2

The research envelope and `research_contract.version=1` remain unchanged.
For a structured inference only, `research_summary` retains `conclusion`,
`evidence`, `counter_evidence`, `risks`, `sources`, and `limitations`, and adds:

```json
{
  "analysis_contract": {
    "version": 2,
    "claim_type": "inference",
    "inference_status": "unverified",
    "counter_evidence_status": "explicitly_missing",
    "evidence_roles": [{"source_id": "7", "role": "下跌占优"}],
    "period_note": null,
    "inference_limit": "未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。"
  }
}
```

* Role-bearing support text, including stale labels, is retained verbatim.
  Roles are limited to 上涨占优、下跌占优、成交额、单日行情、报告期利润同比.
  Every support reference must resolve in the original report's source list.
* Real counter-evidence yields `counter_evidence_status=provided` and a null
  inference limit. A limitation is never inserted into `counter_evidence`.
* When the current compiler explicitly emits the exact limit above, an empty
  counter list is permitted only with role-bearing support and that same
  limit preserved in `limitations` with the `推断限制：` prefix. The inference
  remains **unverified**; this is not a promotion into a fact or a buy signal.
* The legacy risk-only placeholder `未提供结构化反证来源，不升级为事实结论。`
  is likewise retained as an explicit limitation, not counter-evidence. It
  cannot be used as a generic exception for other legacy inference blocks.
* A compiler period note of the form `行情 YYYY-MM-DD；利润同比报告期 YYYY-MM-DD`
  is kept in `period_note` and in `limitations` as `期间：…。`. Both dates must
  precede the report date. Single-day prices and reporting-period financial
  figures are not treated as measurements over the same period.
* Unknown roles, malformed references, missing limits or contradictory
  counter/limit combinations hold rather than silently losing semantics.
* Base factual summaries keep the legacy shape; they do not acquire invented
  inference metadata. Other analysis blocks not selected are disclosed as
  omitted, as before.

## Signed calendar skip, version 1

A weekday holiday must be positively proven; no occurrence, stale research,
or a bare `status=skipped` is insufficient. The relay checks the current-day
completed occurrence (UUID claim, successful child exit), its associated
`last-run` (collect/skipped/non_trading_day/not_applicable and matching frozen
cutoff), and the exact bytes of its referenced frozen context. Calendar dates,
recognized source, timezone-aware observation time and stable file fingerprints
are checked before sending.

The separate envelope is:

```json
{
  "source": "nng8",
  "signal_type": "research_skip",
  "target_date": "<today>",
  "as_of": "<today>",
  "produced_at": "<occurrence.completedAt>",
  "research_skip": {
    "version": 1,
    "status": "not_scheduled",
    "reason": "non_trading_day",
    "market": "XSHG",
    "target_date": "<today>",
    "validated_at": "<occurrence.completedAt>",
    "calendar": {
      "source_id": "akshare:sina:trade-calendar",
      "target_date": "<today>",
      "status": "non_trading_day",
      "previous_trading_date": "<before today>",
      "next_trading_date": "<after today>",
      "data_as_of": "<original same-day timezone-aware dataAsOf>"
    },
    "proof": {
      "occurrence_sha256": "<exact occurrence bytes>",
      "last_run_sha256": "<exact last-run bytes>",
      "context_sha256": "<exact frozen context bytes>",
      "calendar_sha256": "<canonical calendar SHA-256>"
    }
  }
}
```

Canonical encoding is UTF-8 `json.dumps(value, ensure_ascii=False,
sort_keys=True, separators=(",", ":"))`. The six normalized calendar fields
above form `calendar_sha256`. The whole `research_skip` object forms the
`skip_sha256` identity recorded by the sender and receiver. No fake
`report_sha256`, `research_contract`, or research summary is added.

The receiver verifies the authenticated source, current date, schema and
proof before its atomic canonical storage. An ACK must prove stored bytes
match the body hash. A repeated date+kind+identity is idempotent; changed
proof or switching between a research and skip contract on the same date is
held. The receiver does not pretend it fetched the upstream calendar itself.

## Failure routing and recovery

The wrapper forwards held, rejected, not-sent, failed, accepted-without-proof,
ambiguous and malformed results to the same alert classifier; only bounded
status names are passed as arguments. The classifier still reads the real
journal/state reason and uses the wrapper hint when journald has not yet
published its result. The relay exit code is preserved. The digest unit has a
120-second ceiling to accommodate POST (20s), journal collection (20s), and
the alert channel (60s), instead of killing the child at 45 seconds.

Recovery requires current date + report/skip identity **and** receiver storage
evidence (or successful read-only query), valid equal payload/receiver hashes,
and `content_match=true`. Legacy HTTP-only confirmed records are unverified,
not silently promoted. Valid calendar proof with no confirmed delivery is
still a predeadline concern; an unproven skip does not silence alerts.

Historical relay failures cannot suppress a later evidence-based recovery.
Suppressed same-day incidents are also archived as resolved when recovery is
proven. Draft failures remain independent. `notified_once` survives recovery;
there is no automatic Telegram retry after an uncertain outcome.

## Offline verification

`tests/test_research_protocol_v2.py`, `tests/test_calendar_skip_relay.py`, and
`tests/test_alert_protocol_v2.py` cover compiler-shaped content, actual sibling
Signal Gate validation/rendering, absent versus invalid seals, all wrapper
outcomes, strict receiver evidence, suppressed recovery and signed skip
transport with a fake HTTP opener. All files/state are temporary; no real
network requests or messages are generated.

The existing CNINFO seal-chain test uses `ASTOCK_TEST_ROOT` for isolated clones
(otherwise a sibling `astock`, or the production checkout path) and `NODE`
for the runtime. No production report is regenerated by these tests.
