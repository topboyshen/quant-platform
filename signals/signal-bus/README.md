# Sealed research relay

This replaces the normal `build_research_digest.py` + `post_digest.py` service
entry, not the upstream research workflow. Historical scripts are left intact.

The production CLI has fixed roots and no date, destination, recipient, or
backfill override:

```sh
python3 -B /home/ubuntu/signal-bus/scripts/research_relay.py --dry-run --json
python3 -B /home/ubuntu/signal-bus/scripts/research_relay.py --json
```

Dry-run validates today's sealed formal report or signed calendar-skip proof,
even outside the send window.
It does not read `config/pending.env`, notification configuration or prior
delivery state, create directories/locks, write files, invoke models or connect
to the network. Output is only fixed reason codes and non-sensitive metadata;
`would_send` is always false. It is not a backfill authorization.

## Dependency and date contract

1. AStock completes the current Shanghai-day morning occurrence and run.
2. On a trading day, `astock-research-seal.service` produces the immutable
   research seal for the formal report and last-run bytes. XHS drafts run on
   demand and do not trigger automatic delivery or core-chain alerts.
3. The research seal's `OnSuccess` starts Telegram delivery and the relay.
   The independent relay timer also invokes `nng8-research-digest.service` at
   08:05/08:10, using the same wrapper, gates and per-day delivery state.
4. Signal Gate's merged digest runs at 08:15; its consumer independently requires
   today's structured research contract, not the old 72-hour acceptance rule.

Relay sends only Monday–Friday 08:00:00 through 08:13:59 Shanghai time, with a
completed run no older than two hours. Weekend/late/manual invocations cannot
POST. The upstream sealed report already passed trading-calendar validation;
the relay does not guess holidays or silently carry forward an old report.
The 08:14:00 cutoff leaves more than the 20-second HTTP timeout before the 08:15
merged digest. A proven weekday holiday closes the report/seal/Telegram daily
publication branch: no daily report or Telegram daily message is produced.
The independent 08:05/08:10 relay timer remains active and sends the existing
HMAC-signed `research_skip` contract through the same relay. It requires the
current day's completed occurrence and hash-bound calendar proof; a bare skip
status cannot authorize it. The relay unit must not inherit the publication
branch's `publish-branch.closed` condition. This retains one sender and one
deduplicated delivery record per day, without extending the send window.
See [the signed skip contract](docs/RESEARCH_PROTOCOL_V2.md#signed-calendar-skip-version-1).

`research-chain-alert.timer` checks at 08:12 with `--phase predeadline`;
`research-chain-alert-post.timer` invokes the existing post service at 08:15
with `--phase post`, using the same classifier and alert state. Both timers use
`Persistent=false` to avoid catch-up checks. The second check runs after the 08:14 relay
cutoff, so late success can resolve an earlier alert and a newly visible fault
can be reported once. It neither invokes the relay nor resends a daily report;
unchanged faults and repeated recovery observations stay deduplicated. A
confirmed signed skip records Telegram as not required, never as delivered.

All occurrence/run time ordering, successful child exit, fixed report paths,
publication/evidence gates, empty blockers, live/partial data, frozen cutoff,
human semantic-review label and three source hashes are validated. Symlink
parents/files, duplicate-key/invalid JSON, oversize files, changed artifacts and
missing seal proofs hold the relay. The mutable live context is deliberately
not read; its hash comes from the sealed run and matching manifest source.

## Summary contract

`prepare_research(project_root, now)` is a read-only Python API returning the
payload/proof in memory. `build_payload(project_root, now)` returns the payload.
These test seams are not CLI path/date overrides.

The market `as_of` comes from section 2 and must precede the report day. Section
6 supplies the first complete `[推断] 摘要` block (otherwise the first inference
block), including every support/counter-evidence continuation. All section 7
risks, cited source entries, partial-data limits and human-review limits remain.
If no structured inference exists, a labelled basic report carries original
facts; no analysis or counter-evidence is invented. Selection of one of several
blocks is disclosed. Overlong retained content fails instead of truncating away
risks or counter-evidence. Query/fragment credentials and unsafe source links are
removed with an explicit limitation note. No order or approval signal is emitted.

For a basic report's first nonempty headline line, a single unordered Markdown
bullet (`-`, `+`, or `*`, followed by spaces/tabs) is removed only when immediately
followed by an existing `[事实]` or `[推断]` label. Plain labelled headlines and
later lines keep the existing whitespace normalization only; unlabelled
continuations do not acquire labels.
Missing labels, unsupported wrappers and missing/invalid references still hold.

Conclusions retain `[事实]`/`[推断]` labels and require source references. If the
original headline has no inline reference, its retained support references are
copied explicitly into the conclusion, never invented. Inference blocks without
a cited support field or counter-evidence hold. Each continuation remains in its
original cited evidence item. The only uncited counter-evidence allowed is the
exact pre-existing risk-block placeholder `未提供结构化反证来源，不升级为事实结论。`;
it is preserved as a disclosed limitation, never inserted to make a report pass.
Compound references such as `[来源 2、3]` or `[来源 2 3]` resolve every
member; malformed or unknown references hold. Contract limits are 1,200 characters
per string, 24 items per list and 20,000 summary characters; exceeding a limit
holds rather than dropping counter-evidence or risk text.

The payload is `source=nng8`, `signal_type=research_summary`, with a version 1
`research_contract` and `research_summary` keys `conclusion`, `evidence`,
`counter_evidence`, `risks`, `sources`, `limitations`. `text/markdown` are backwards
compatible safe previews, not the primary consumer contract.

## Delivery safety

Normal mode reads only the existing POST keys `DEDIROCK_DIGEST_URL`,
`DEDIROCK_PENDING_SECRET`, `X_SOURCE`, `TLS_VERIFY`. HTTPS certificate checking is
mandatory; HTTP/redirects, userinfo and unverified TLS are not accepted.
Default `TLS_VERIFY=1` uses normal certificate-chain and hostname verification.
Process proxy environment variables are ignored in both modes.

For the existing IP endpoint whose self-signed certificate lacks a SAN, the
independent relay may use an explicitly provisioned certificate-identity pin.
The old `TLS_VERIFY=0` value alone **never** permits a connection: it additionally
requires both fixed files, installed through an already authenticated SSH path:

- `config/dedirock-relay-ca.crt`: one public PEM certificate, no private key.
- `config/relay-tls-pin.json`: exactly `version: 1`, `host` matching the existing
  endpoint hostname, and `certificate_sha256` matching that certificate's DER.

This explicit mode uses a dedicated CA with `CERT_REQUIRED`, keeping chain and
certificate-time validation. Since the existing certificate has no SAN,
hostname checking is replaced by **both** exact endpoint-host checking and the
exact peer-certificate DER SHA256. The custom connection performs these checks
after the TLS handshake and before any HTTP request bytes can be written.
Missing/malformed trust files, local/peer pin mismatch, chain failures, host
changes, CONNECT proxies and redirects fail closed. The relay does not create or
refresh trust pins automatically; certificate rotation needs an independently
authenticated trust-file update. No Nginx, private-key or pending-env changes are
made by this program. Dry-run never reads either trust file.

HMAC signs `timestamp + '.' + exact UTF-8 JSON bytes`. Acknowledgement must be a
2xx JSON object with `status=accepted`, `source=nng8`, `day=as_of`; its private
`path` or any response body is never logged or stored.

Per-day lock and atomic state live at `out/relay-delivery/YYYY-MM-DD.{lock,json}`.
State contains dates, hashes and fixed status/reason only. `sending` is fsynced
before POST; confirmed same-hash delivery is skipped. A changed same-day hash,
crash in `sending`, timeout or other ambiguous result requires manual review,
never an automatic resend. Explicit request rejection or proven non-connection
may retry at the next scheduled opportunity. No external result is guessed.

## Offline regression tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

Tests use temporary artifact trees, mock transport and a socket guard. They
cover sealed inputs, old/current report dates, mutation and symlink resistance,
gates/hashes/cutoff, basic-mode disclosure, retained counter-evidence/references,
source credential cleanup, dry-run isolation, window recheck, HMAC/receipts,
confirmed deduplication and ambiguous/rejected delivery recovery boundaries.
An optional integration test imports a sibling Signal Gate consumer when present
and validates six fixture payload shapes (including a basic list headline)
entirely in memory. A standalone
production relay checkout skips that one test; all other tests are independent.

## Shared delivery evidence contract

`scripts/delivery_evidence.py` is the single pure proof contract used by the
research alert and the dashboard. It has no filesystem, network, sender or retry
state access. The relay's resend-suppression state machine remains separate:
historical `confirmed`/sent records with incomplete proof do not authorize a
resend, nor are they silently rewritten as new proof.

POST confirmation and `confirmed_via_read_only_query` both need the same stored
receiver ID, actual payload hash, source identity and independently bound exact
`as_of`. HTTP accepted alone is insufficient. Telegram requires explicit
`outcome=success`, a valid zoned send receipt and matching receipt/event/file
hashes and exact integer byte counts. Historical raw-original attachments remain
supported only with the same proof and sealed-report identity; a missing outcome
or malformed presentation never becomes an implicit success.

Install the canonical module before either consumer. The dashboard's production
code path is fixed at `/home/ubuntu/signal-bus/scripts/delivery_evidence.py`,
independent of `/home/ubuntu/ops/dashboard` and `--source-root`. An explicitly
trusted `--delivery-contract-module` path supports isolated layouts. Missing,
incompatible or unimportable code produces explicit insufficient/unverified
delivery evidence, never a fallback validator or a pass; seal and other
dashboard panels continue to work. The alert's proof readers also degrade
without crashing. Existing send ownership, timers and deduplication are unchanged.

The cross-consumer fixture test requires the dashboard checkout. In the Linux
isolated layout, set `DASHBOARD_TEST_ROOT` to the dashboard code directory and
`DELIVERY_CONTRACT_TEST_MODULE` to the signal checkout's
`scripts/delivery_evidence.py`; neither variable selects evidence as code.
For the deployed layout these explicit values are
`DASHBOARD_TEST_ROOT=/home/ubuntu/ops/dashboard` and
`DELIVERY_CONTRACT_TEST_MODULE=/home/ubuntu/signal-bus/scripts/delivery_evidence.py`.
For the pinned Linux test bundle they are respectively
`/payload/lean-release-preparation/dashboard-qa` and
`/payload/lean-release-preparation/composed/signal-bus/scripts/delivery_evidence.py`.
The local isolated candidate fallback is the exact sibling `../dashboard`
relative to the signal checkout, not the production two-level layout. An absent
dashboard fixture is a test import error, never a silently skipped negative
matrix. Production proof readers instead report a missing canonical module as
explicit insufficient/unverified evidence; they never search `source_root` for
replacement code.
`tests/test_delivery_evidence_contract.py` runs identical positive/negative
fixtures through both consumers, including partial installation and legacy
resend suppression. These tests use temporary evidence and no real sends.

## CNINFO seal chain regression (2026-09-17)

Offline full-chain check that a report with CNINFO `披露日期` + `实际采集时间`
source lines can build an xhs-draft seal and pass `prepare_research` / dry-run
relay validation without HTTP send:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_cninfo_seal_relay_chain -v
```

See `CNINFO_SEAL_RELAY_CHAIN.md`. Current seal-failure alerts use
`research-chain-alert-post.service`; relay failures reach the same state machine
through `run_research_relay_with_alert.sh`. XHS draft failures stay local to the
on-demand command. The historical `morning_seal_alert.sh` is not an automatic
sender. See [current alert wiring](docs/RESEARCH_CHAIN_ALERT_WIRING.md).
