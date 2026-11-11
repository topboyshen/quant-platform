# DoltHub fixed-snapshot API contract — 2026-09-09

Later integration evidence: 500-row pages were subsequently validated on the
same fixed commit. The complete 09-02..09-08 range returned 27,767 unique rows,
with daily counts 5,552 / 5,554 / 5,553 / 5,554 / 5,554 for 09-02 / 03 / 04 / 07 / 08.
The original small-probe observations below remain historically scoped; the
current CLI uses page_size=500, max_requests=320 and max_seconds=1000.

The successful full dry-run at 16:52 CST pinned
`8u5mifihmr1an44clfcs8dgc8l604qmn`; each run freezes its own ref and never
mixes these historical observations across commits. Runtime transport now uses
40 seconds per attempt (connect 8 seconds), four alternating direct/existing
proxy attempts sharing at most 84 seconds, clipped by the original 1000-second
source deadline. The exact pinned final COUNT succeeded in a 23.35-second
direct probe after the former 20-second limit had cut it off. Class defaults
remain separate from this explicit CLI configuration. CSI and future-calendar
fetches now finish before expensive local feature conversion; the source clock
is not reset and the scheduled whole-process cap remains 25 minutes.

Read-only observations 14:08–14:11 CST. No bulk price download, upstream write,
login, production update, executable download or proxy change was performed.
Only three individual price rows were read; other results were small metadata,
schema or aggregate queries. The source client has no import/CLI side effects.

## Fixed version

Endpoint: `https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master`.

`SELECT DOLT_HASHOF('HEAD') AS head_hash` returned
`80q7jtqjrga15h61uufm60h6uqirk6iq` both before and after the investigation.
Do not put this hash in the endpoint path: the earlier direct commit path failed.
Instead every base table reference must explicitly use:

```sql
SELECT tradedate,symbol,high,low,open,close,adjclose,volume,amount
FROM final_a_stock_eod_price
AS OF '80q7jtqjrga15h61uufm60h6uqirk6iq'
WHERE tradedate='2026-09-08'
ORDER BY tradedate,symbol LIMIT 2
```

This returned HTTP 200 + `Success`, symbols BJ920000/BJ920001. The next fixed
query with `AND symbol>'BJ920001' LIMIT 1` returned BJ920002. Values, including
numbers, were strings. Response `commit_ref` remains `master`: it is not a
resolved hash. The exact SQL echo includes the fixed AS OF. An all-zero missing
commit produced **HTTP 200 + `query_execution_status='Error'`**, not an empty
successful dataset. A moving branch head must never silently repin later pages.

[Official historical-query documentation](https://www.dolthub.com/docs/sql-reference/version-control/querying-history/)
documents per-table AS OF commits and warns that historical views may use the
current view definition. The client therefore permits only the four base tables
below and does not rely on `max_index_date` (a view).

## Result acceptance and pagination

- Require HTTP 200, valid bounded UTF-8 JSON, `Success`, empty query message,
  expected repository identity, `commit_ref=master`, and exact `sql_query` echo.
- Reject `Error`, `RowLimit`, missing status, malformed/duplicate JSON keys,
  schema/row mismatch, non-string non-null cells, oversize data or transport
  failure. The [official SQL API](https://www.dolthub.com/docs/products/dolthub/api/v1alpha1/sql/)
  explicitly demonstrates `RowLimit`; it does not document a pagination cursor
  or a numeric row ceiling in the reviewed page.
- Client defaults to SQL LIMIT 200, configurable through 1000 as a **local safety
  cap**, not a verified service allowance. Only LIMIT 2/1 was tested live here.
  A requested 500-row page needs integration validation; never accept RowLimit.
- Keyset order includes a full unique key; every page and count uses the same
  fixed commit and predicate. Reject duplicate/non-increasing keys, excess rows,
  omitted projected keys, count mismatch and changed final count. Return rows
  only after complete validation, including a terminal short/empty page.
- HTTP, body bytes, per-query transport time, total client time, requests and
  rows are bounded. Explicit direct then existing SOCKS5h 127.0.0.1:7891 only;
  no curl config, persisted response, credentials, TLS weakening or proxy edits.
  HTTP 4xx (including quota errors) and oversize responses are not route-retried.
- The client is an application-owned SELECT reader, not a generic SQL sandbox.
  Date/market/price/unit/coverage checks remain the caller's duty. Metadata
  acceptance alone does not prove economically valid or complete prices.

## Schema at the fixed commit

| Base table | Unique key | Other fields / cautions |
|---|---|---|
| final_a_stock_eod_price | tradedate, symbol | tradedate DATE; symbol VARCHAR(100); high,low,open,close,adjclose,volume DOUBLE NOT NULL; amount DOUBLE nullable |
| ts_index_weight | index_code, stock_code, trade_date | index/stock VARCHAR(100), trade_date DATE, weight DOUBLE NOT NULL; index_date(index_code,trade_date) secondary index |
| ts_trade_day_calendar | id; also exchange,date | id INT; exchange VARCHAR(255); date DATE; is_open BINARY(1), observed open HEX=31 (ASCII '1'), not a JSON boolean |
| ts_a_stock_list | ts_code | ts_code,symbol,exchange VARCHAR(100); list_date DATE NOT NULL; delist_date DATE nullable |

Pinned SHOW CREATE TABLE verified all four. For the SSE calendar use SQL
`is_open=1`; never use Python truthiness of the returned string. Fixed-source
MAX(date) with exchange SSE, is_open=1, date<=2026-09-08 returned 2026-09-08.

## Reference indices and constituent limitations

No separate final index-price table appeared in the pinned 14-entry table list.
The final price table contains SH000300/SH000852/SH000905/SH000906/SZ399300 with
three rows each in 2026-09-04..2026-09-08 and max date 2026-09-08. **SH000985 has
zero rows in that interval**; do not fabricate or silently mark its coverage as
current. This does not establish whether an older historical SH000985 row exists.

The pinned aggregate of constituent max dates for six index codes exceeded its
15-second timeout. No retry was made; constituent freshness is unverified.
Use bounded index-specific ranges from the existing terminal membership start,
validate the base-date membership set, then update from actual observed sets.
Do not infer membership freshness from a successful price query or use the
unfixed definition of max_index_date.

## Interfaces

`DoltClient(transport=None, ref=None, page_size=200, max_rows=100000,
max_requests=256, max_bytes=2097152, max_seconds=900)`; keyword-only limits.

- `latest_ref()` obtains and freezes the first head; later calls return it.
- `get_head()` observes head without changing the frozen ref.
- `table(name)` returns the quoted base-table AS OF expression.
- `query(sql)` returns fully checked `list[dict]`, at most page_size rows.
- `paginated_rows(table, columns, where='1=1', order_fields=None)` returns a
  count-verified list. Columns must include a full unique order key.
- Injected transport signature: `callable(url) -> (HTTP_status, response_bytes)`.
- `requests` and `events` expose only request number, HTTP code, row count and
  status. `CurlTransport.events` contains transport/curl/HTTP/byte counts, never
  raw response bodies or URLs. Offline tests prohibit real subprocess/network.

## CSI interval module

`indices.update_indices(old_root, stage_root, client, base_date, target_date,
new_calendar)` validates every required CSI plan before writing any staging file.
The old terminal members must exactly match the latest fixed-source snapshot at
or before base_date within the bounded terminal-start..target range. Counts for
both old terminal and new snapshots must be 300/500/800/1000 for the named fixed
CSI indices; csiall may change count when the complete source snapshot proves it.
No absent snapshot is interpreted as an empty set. Snapshot dates are reported;
carried membership between snapshots is not called a fresh daily observation.

Historical intervals clipped at base_date must remain exactly equivalent.
Staging files are replaced through a verified directory descriptor, breaking
possible file hardlinks without writing the old tree and rejecting a substituted
stage ancestor. Production lock, full target-date feature coverage of all new
constituents, day_future, generation manifest, and atomic tree publication remain
the caller's separate duties. Any source timeout aborts the plan, not membership.

Offline tests: source 18 + indices 17 = 35 passed. These mock results do not
validate full live pages, constituent availability, market correctness or profit.

## Follow-up CSI compatibility probe, 14:50–14:54 CST

Using the same verified client and fixed commit, bounded date/count queries and
non-truncated bucketed SHA-256 summaries established that all five old terminal
membership sets match their fixed-source baseline exactly. Four fixed CSI sets
have 06-30/07-31/08-31 snapshots with nominal 300/500/800/1000 members; baseline
is 08-31. csiall has only the 05-29 snapshot in its terminal-start..09-08 range,
with 5061 members. There are no 09-07/08 new groups in those queried ranges.
Updating intervals carries these observed sets forward; it does not claim new
daily constituent observations. No member-detail batches were downloaded.

An important API caveat was observed: GROUP_CONCAT silently truncated whole-set
aggregation to 1024 characters despite HTTP 200 + Success. Such aggregate hashes
are **not** completeness proof. The probe instead grouped canonical codes by
six-character prefix (at most 100 ordinary codes per bucket), required actual
concatenated length to equal its computed expected length for every bucket, and
compared all bucket keys/counts/SHA-256 values against locally computed sets.
All 72/78/87/83/102 buckets respectively were complete and matched, with zero
mismatches. Production indices.py does not depend on GROUP_CONCAT hashes.
